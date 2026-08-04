"""
location.py — where the receiver is, and how stale that answer is.

THREE SOURCES, one interface:

    plan header    the coordinates the CURRENT plan was propagated for
    geometry.json  the configured receiver MAIN.py uses
    laptop         a live CoreLocation fix from this machine

The first two are already what skyview.receiver_location() reads; this module
adds the third and, more importantly, adds TIME to all of them. A coordinate on
screen with no timestamp is the thing that lets you analyse a capture against
the wrong site.

WHY THE LAST-KNOWN FIX IS CACHED
--------------------------------
Continuous location on a laptop costs battery, and a fixed antenna does not
move. So a laptop fix is taken ONCE, on request, written to
SReTo's state dir as location.json, and reused from there forever after — the GUI keeps
displaying it with "last updated HH:MM, dd.mm.yyyy" rather than going dark when
location services are switched off. A stale coordinate that says how stale it is
beats a live one that costs a battery, and it beats a blank field outright.

ABOUT CoreLocation, AND WHY A BARE `sreto` CANNOT USE IT
-------------------------------------------------------
pyobjc-core may be present without the CoreLocation *bindings*, so the
framework is loaded by path with objc.loadBundle rather than imported.

macOS's Location Services permission is not a per-process toggle — it is keyed
to a BUNDLE IDENTIFIER (TCC, the same mechanism behind every "X wants to use
your..." system prompt). `NSBundle.mainBundle().bundleIdentifier()` for a bare
interpreter process is None, and a None identifier has nothing for System
Settings to list or grant. This is not a missing checkbox to find — there is
no setting that fixes it, for any bare CLI process, ever. requestWhenInUseAuth-
orization() silently no-ops and the fix just times out.

WHAT "RUN IT FROM THE .APP" ACTUALLY REQUIRES
---------------------------------------------
Wrapping a .app around the launch command is NOT enough, and this cost a long
debugging session: macOS derives the asking app from the running process's
executable path, walking up to the enclosing .app. A launcher script that
shells out to the conda env's interpreter yields a process whose executable is
`.../envs/<env>/bin/python` — outside any bundle — so mainBundle() resolves to
that bin directory, bundleIdentifier() is still None, and the Info.plist next
door is never consulted. From CoreLocation's side the .app may as well not
exist.

So scripts/make_desktop_app.sh copies the interpreter INTO
`Contents/MacOS/python` and launches that, with PYTHONHOME pointing back at
the real environment for the stdlib and site-packages. Now the process is
inside the bundle, mainBundle() is the .app, and the Info.plist's
CFBundleIdentifier + NSLocationWhenInUseUsageDescription apply to it. The
bundle is also ad-hoc code-signed, because locationd will not durably record a
client it cannot identify by signature.

Launched that way, macOS shows the usual permission prompt once and the app
then appears in System Settings > Privacy & Security > Location Services like
any other. is_bundled_app() below is the check that distinguishes the two
cases at runtime.

Everything here is best-effort by design regardless: nothing in the pipeline
depends on a live fix, and a bare-process failure degrades straight to the
cached fix and then to the plan header.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import paths

CACHE_PATH = os.path.join(paths.GUI_STATE_DIR, "location.json")

SOURCE_LABELS = {
    "laptop": "this laptop (CoreLocation)",
    "plan header": "plan header",
    "geometry.json": "geometry.json",
    "cache": "last known fix",
}

# A fix older than this is still shown — with its age — but the UI marks it.
STALE_AFTER_S = 24 * 3600


class Fix:
    """One position, and when it was obtained."""

    __slots__ = ("lat", "lon", "alt_m", "source", "unix", "accuracy_m")

    def __init__(self, lat, lon, alt_m=0.0, source="", unix=0.0, accuracy_m=None):
        self.lat = float(lat)
        self.lon = float(lon)
        self.alt_m = float(alt_m or 0.0)
        self.source = source
        self.unix = float(unix or 0.0)
        self.accuracy_m = accuracy_m

    # ── formatting ────────────────────────────────────────────────────────
    def short(self):
        """'52.3792°N 13.0661°E' — the header tile version."""
        ns = "N" if self.lat >= 0 else "S"
        ew = "E" if self.lon >= 0 else "W"
        return f"{abs(self.lat):.4f}°{ns} {abs(self.lon):.4f}°{ew}"

    def precise(self):
        """Full precision, for the clipboard. Plain decimal degrees, lat,lon —
        the form soop_planner.py and geometry.json both use, so a paste lands
        straight into either without reformatting."""
        return f"{self.lat!r}, {self.lon!r}"

    def detailed(self):
        parts = [self.precise(), f"{self.alt_m:.1f} m"]
        if self.accuracy_m:
            parts.append(f"±{self.accuracy_m:.0f} m")
        parts.append(SOURCE_LABELS.get(self.source, self.source))
        if self.unix:
            parts.append(self.age_label())
        return "   ·   ".join(parts)

    def age_s(self):
        return max(0.0, time.time() - self.unix) if self.unix else None

    def is_stale(self):
        age = self.age_s()
        return age is not None and age > STALE_AFTER_S

    def age_label(self):
        """'last updated 14:32, 04.08.2026' — the wording the tile shows.

        Berlin wall-clock like every other displayed time in this repo; the
        stored value stays unix/UTC.
        """
        if not self.unix:
            return "time unknown"
        from .history import BERLIN
        stamp = datetime.fromtimestamp(self.unix, BERLIN)
        return f"last updated {stamp.strftime('%H:%M')}, {stamp.strftime('%d.%m.%Y')}"

    def to_dict(self):
        return {"lat": self.lat, "lon": self.lon, "alt_m": self.alt_m,
                "source": self.source, "unix": self.unix,
                "accuracy_m": self.accuracy_m}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or "lat" not in data or "lon" not in data:
            return None
        try:
            return cls(data["lat"], data["lon"], data.get("alt_m", 0.0),
                       data.get("source", "cache"), data.get("unix", 0.0),
                       data.get("accuracy_m"))
        except (TypeError, ValueError):
            return None

    def __eq__(self, other):                               # pragma: no cover
        return (isinstance(other, Fix)
                and abs(self.lat - other.lat) < 1e-9
                and abs(self.lon - other.lon) < 1e-9)


# ── the cache ─────────────────────────────────────────────────────────────
def load_cached():
    """The last laptop fix, or None. Survives restarts and a disabled GPS."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return Fix.from_dict(json.load(f))
    except (OSError, ValueError):
        return None


def save_cached(fix):
    paths.ensure_state_dirs()
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(fix.to_dict(), f, indent=2)
        return True
    except OSError:
        return False


# ── the repo's own coordinates ────────────────────────────────────────────
def from_plan_or_geometry():
    """Whatever skyview already resolves, wrapped in a timestamped Fix.

    The timestamp is the FILE's mtime, which is honest: it is when those
    coordinates were last written, not when a satellite was seen.
    """
    from . import skyview
    rx = skyview.receiver_location()
    if not rx:
        return None
    lat, lon, alt, source = rx
    path = paths.PLAN_TSV if source == "plan header" else paths.GEOMETRY_JSON
    try:
        unix = os.path.getmtime(path)
    except OSError:
        unix = 0.0
    return Fix(lat, lon, alt, source, unix)


# ── the laptop ────────────────────────────────────────────────────────────
def is_bundled_app():
    """Does this process have a real bundle identifier?

    macOS's Location Services permission is keyed to this identifier, not to
    the process. A bare interpreter has none — see the module docstring — so
    this is the one check that predicts whether a fix can EVER work here,
    before spending 8 seconds finding out the slow way.
    """
    try:
        from Foundation import NSBundle
        return NSBundle.mainBundle().bundleIdentifier() is not None
    except ImportError:
        return False


def corelocation_available():
    """Can we even try? (bool, reason)

    Every rejection names its fix, or says plainly that there is not one. A
    bare "pyobjc is not installed" sends the reader to a search engine for a
    package name this project already knows — and on Linux it would send them
    to install something that cannot help.
    """
    on_macos = os.path.isdir("/System/Library/Frameworks/CoreLocation.framework")
    try:
        import objc  # noqa: F401
    except ImportError:
        if not on_macos:
            return False, ("CoreLocation is a macOS framework — not available "
                           "here. Use the plan header or geometry.json "
                           "coordinates instead")
        return False, (
            "pyobjc is not installed in this environment — "
            'install it with: pip install "sreto[macos]". Note that it only '
            "helps when the GUI is launched from the .app bundle "
            "scripts/make_desktop_app.sh builds; a bare python process cannot "
            "be granted Location Services at all")
    if not on_macos:
        return False, ("CoreLocation is a macOS framework — not available here. "
                       "Use the plan header or geometry.json coordinates instead")
    if not is_bundled_app():
        return False, (
            "running as a bare python process, which macOS cannot grant "
            "Location Services to — there is no setting for this, for any "
            "bare process. Build scripts/make_desktop_app.sh's .app bundle and "
            "launch the GUI from that instead")
    return True, ""


# CLAuthorizationStatus, from CLLocationManager.h. Getting these wrong is
# silent and confusing — 3 and 4 are the two SUCCESS values, not failures.
AUTH_NOT_DETERMINED = 0
AUTH_RESTRICTED = 1
AUTH_DENIED = 2
AUTH_ALWAYS = 3
AUTH_WHEN_IN_USE = 4
AUTH_GRANTED = (AUTH_ALWAYS, AUTH_WHEN_IN_USE)

# CLError, from CLError.h. Code 0 is TRANSIENT — Apple's own guidance is to
# keep waiting rather than treat it as failure, and CoreLocation repeats it
# every second or so while it is still trying.
CL_ERROR_LOCATION_UNKNOWN = 0
CL_ERROR_DENIED = 1
CL_ERROR_NETWORK = 2

_DELEGATE_CLASS = None


def wifi_powered():
    """Is the Wi-Fi radio on? True / False / None if it cannot be determined.

    This matters more than it looks. A Mac HAS NO GPS — no Mac ever shipped
    with a GNSS receiver. CoreLocation positions it by scanning nearby Wi-Fi
    base stations and asking Apple's location service where that pattern of
    BSSIDs is. With the radio off there is nothing to scan, so a fix is
    impossible no matter what permissions say — and the only symptom is a
    repeated kCLErrorLocationUnknown, which names none of this. Ethernet does
    not substitute: it carries the query to Apple but supplies no BSSIDs to
    ask about.
    """
    try:
        import objc
        bundle = {}
        objc.loadBundle("CoreWLAN", bundle,
                        bundle_path="/System/Library/Frameworks/CoreWLAN.framework")
        client = bundle["CWWiFiClient"].sharedWiFiClient()
        interface = client.interface()
        return bool(interface.powerOn()) if interface is not None else None
    except Exception:                                      # noqa: BLE001
        return None


def _delegate_class():
    """A minimal CLLocationManagerDelegate, created once.

    Defining an ObjC class twice under the same name raises, so this is built
    lazily and cached. CoreLocation will not reliably start delivering updates
    to a manager with no delegate, even when the fix is read by polling
    manager.location() rather than from the callback.
    """
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is None:
        from Foundation import NSObject

        class _SoOpLocationDelegate(NSObject):
            def locationManager_didUpdateLocations_(self, manager, locations):
                # Deliberately empty: the fix is read by polling
                # manager.location(), which is simpler than marshalling an
                # ObjC callback back to the waiting thread. This exists only
                # because CoreLocation wants somewhere to deliver updates.
                pass

            def locationManager_didFailWithError_(self, manager, error):
                self.failure = str(error)
                try:
                    self.failure_code = int(error.code())
                except Exception:                          # noqa: BLE001
                    self.failure_code = None

            def locationManagerDidChangeAuthorization_(self, manager):
                # Also empty: laptop_fix() polls authorizationStatus() in the
                # same loop, so the change is observed there.
                pass

        _DELEGATE_CLASS = _SoOpLocationDelegate
    return _DELEGATE_CLASS


def _auth_status(manager, CLLocationManager):
    """Current authorization, preferring the instance method.

    The class method is deprecated from macOS 11 and reports stale values on
    some releases; the instance property is the one to trust when present.
    """
    try:
        return int(manager.authorizationStatus())
    except Exception:                                      # noqa: BLE001
        pass
    try:
        return int(CLLocationManager.authorizationStatus())
    except Exception:                                      # noqa: BLE001
        return None


def _denied_message():
    return ("location access was denied for this app — turn it back on under "
            "System Settings > Privacy & Security > Location Services")


def _no_fix_message(timeout_s):
    """Say WHY nothing arrived, not just that nothing arrived.

    Wi-Fi off is by far the most common cause on a desk machine and produces
    no distinguishing error of its own, so it is checked explicitly rather
    than left for the user to guess at from 'no fix'.
    """
    if wifi_powered() is False:
        return ("Wi-Fi is switched off, and a Mac has no GPS — it works out "
                "where it is by scanning nearby Wi-Fi networks. Turn Wi-Fi on "
                "(you do not have to join a network; Ethernet alone is not "
                "enough) and try again")
    return (f"no fix within {timeout_s:.0f} s — a machine indoors can take "
            f"longer to place itself, so try again")


def laptop_fix(timeout_s=8.0, auth_timeout_s=45.0):
    """A live fix from this machine, or (None, reason).

    Blocking, and slow enough to matter — call it from a worker thread. The
    CoreLocation delegate needs a run loop, so this spins one on the calling
    thread and gives up rather than parking the caller forever.

    Two separate waits, because they fail for different reasons and on wildly
    different timescales:

        auth_timeout_s   only on the very first run, while the macOS prompt is
                         on screen waiting for a human to click Allow. Eight
                         seconds is not enough time to read a dialog, and the
                         old single timeout meant the first request always
                         reported failure even when the user then allowed it.
        timeout_s        the actual satellite/wifi fix, once permission exists.
    """
    ok, reason = corelocation_available()
    if not ok:
        return None, reason

    manager = None
    try:
        import objc
        from Foundation import NSDate, NSRunLoop

        bundle = {}
        objc.loadBundle(
            "CoreLocation", bundle,
            bundle_path="/System/Library/Frameworks/CoreLocation.framework")
        CLLocationManager = bundle.get("CLLocationManager")
        if CLLocationManager is None:
            return None, "CoreLocation loaded but CLLocationManager is missing"

        if not CLLocationManager.locationServicesEnabled():
            return None, ("Location Services are switched off for the whole "
                          "machine — System Settings > Privacy & Security > "
                          "Location Services")

        # Checked BEFORE asking for permission, not after timing out: with the
        # radio off there is no positioning path at all on a Mac, so the only
        # outcomes are a 45 s wait on a permission prompt the user cannot make
        # use of, followed by a bare 'no fix'. Say the actual reason at once.
        if wifi_powered() is False:
            return None, _no_fix_message(timeout_s)

        manager = CLLocationManager.alloc().init()
        delegate = _delegate_class().alloc().init()
        delegate.failure = None
        delegate.failure_code = None
        manager.setDelegate_(delegate)

        loop = NSRunLoop.currentRunLoop()

        def pump(seconds):
            loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))

        # ── permission ────────────────────────────────────────────────────
        status = _auth_status(manager, CLLocationManager)
        if status == AUTH_NOT_DETERMINED:
            if hasattr(manager, "requestWhenInUseAuthorization"):
                manager.requestWhenInUseAuthorization()
            deadline = time.time() + auth_timeout_s
            while time.time() < deadline and status == AUTH_NOT_DETERMINED:
                pump(0.25)
                status = _auth_status(manager, CLLocationManager)
            if status == AUTH_NOT_DETERMINED:
                return None, ("no answer to the location permission prompt "
                              f"within {auth_timeout_s:.0f} s — try again and "
                              "click Allow")

        if status == AUTH_DENIED:
            return None, _denied_message()
        if status == AUTH_RESTRICTED:
            return None, ("location access is restricted on this machine "
                          "(parental controls or an MDM profile)")

        # ── the fix ───────────────────────────────────────────────────────
        manager.startUpdatingLocation()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            pump(0.25)
            location = manager.location()
            if location is not None:
                coord = location.coordinate()
                # (0, 0) is CoreLocation's "no fix yet", not the Gulf of Guinea.
                if coord.latitude or coord.longitude:
                    return Fix(
                        coord.latitude, coord.longitude,
                        location.altitude(), "laptop", time.time(),
                        accuracy_m=location.horizontalAccuracy()), ""
            # Only DENIED is worth giving up on. LOCATION_UNKNOWN arrives once
            # a second for the whole time CoreLocation is still searching, so
            # returning on it — as this used to — turned "still working on it"
            # into an instant, permanent-looking failure.
            if delegate.failure_code == CL_ERROR_DENIED:
                return None, _denied_message()
            delegate.failure = None
        return None, _no_fix_message(timeout_s)
    except Exception as e:                                 # noqa: BLE001
        return None, f"CoreLocation failed: {e}"
    finally:
        if manager is not None:
            try:
                manager.stopUpdatingLocation()
                manager.setDelegate_(None)
            except Exception:                              # noqa: BLE001
                pass


def request_laptop_fix_async(on_done, timeout_s=8.0):
    """Fetch a laptop fix on a worker thread; on_done(fix, reason) off it.

    on_done is called FROM THE WORKER. Marshal to Tk yourself — app.py does it
    through the same queue the job runner uses.
    """
    def worker():
        fix, reason = laptop_fix(timeout_s=timeout_s)
        if fix is not None:
            save_cached(fix)
        on_done(fix, reason)

    thread = threading.Thread(target=worker, daemon=True, name="location")
    thread.start()
    return thread


# ── resolution ────────────────────────────────────────────────────────────
def resolve(prefer="plan"):
    """The Fix to display. `prefer` is 'laptop' or 'plan'.

    'laptop' never triggers a live lookup — it uses the cached fix, which is
    the whole point: the radio does not move, so one fix lasts. Ask for a fresh
    one explicitly with request_laptop_fix_async().
    """
    if prefer == "laptop":
        cached = load_cached()
        if cached is not None:
            return cached
    return from_plan_or_geometry()


def utc_now_label(fmt="%H:%M:%S"):
    return datetime.now(timezone.utc).strftime(fmt)


def local_now_label(fmt="%H:%M:%S"):
    """Berlin wall-clock — the repo's display convention (see history.py)."""
    from .history import BERLIN
    return datetime.now(BERLIN).strftime(fmt)


def local_tz_label():
    from .history import BERLIN
    return datetime.now(BERLIN).strftime("%Z")
