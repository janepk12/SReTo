"""
skyview.py — which passes can you ACTUALLY see from where the antenna sits.

A pass being above the horizon is not the same as it being observable. A roof,
a building line, or an antenna pointed at one quadrant means most of the sky is
unusable, and the planner has no idea — it masks on elevation alone
(soop_planner writes 'mask 10 deg' into the plan header and nothing about
azimuth). So the plan lists passes you cannot record.

This module adds the missing half: an azimuth mask. You declare which compass
sectors you have a view of, and each pass is evaluated against it over its whole
arc — not just at its peak, because a pass that peaks behind your building can
still be perfectly usable on the way up.

WHAT IT REPORTS, per pass:
    fraction of the pass inside your view
    the visible time window (start/end unix) — this is the capture window
    best elevation reached WHILE VISIBLE (not the pass's overall peak)

That last distinction is the point. A 78° pass that is only visible below 15°
is a worse target than a 40° pass you can see all of, and the raw plan ranks
them the other way round.

NO NEW ORBITAL MECHANICS. Azimuth comes from orbits.propagate_satellite() —
the pipeline's own propagator, fed the exact TLE the planner recorded in the
plan file. skyfield/numpy are imported lazily, on first use, so a session that
never opens this feature never pays for them.
"""

import json
import os
import re
import threading

from . import paths

# 45°-wide compass sectors, centred on their bearing.
SECTOR_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SECTOR_WIDTH = 45.0
SECTOR_CENTER = {name: i * SECTOR_WIDTH for i, name in enumerate(SECTOR_NAMES)}

ALL_SECTORS = frozenset(SECTOR_NAMES)

# Named views. The cardinal ones are three sectors wide (135°), which is what a
# window, a balcony or a roof edge facing that way actually gives you — a single
# 45° wedge is narrower than any real sky view.
PRESETS = {
    "Full sky": ALL_SECTORS,
    "North view (NW·N·NE)": frozenset({"NW", "N", "NE"}),
    "East view (NE·E·SE)": frozenset({"NE", "E", "SE"}),
    "South view (SE·S·SW)": frozenset({"SE", "S", "SW"}),
    "West view (SW·W·NW)": frozenset({"SW", "W", "NW"}),
    "Southern half": frozenset({"E", "SE", "S", "SW", "W"}),
    "Northern half": frozenset({"W", "NW", "N", "NE", "E"}),
    "Eastern half": frozenset({"N", "NE", "E", "SE", "S"}),
    "Western half": frozenset({"S", "SW", "W", "NW", "N"}),
}

DEFAULT_SAMPLES = 48
MIN_SAMPLE_STEP_S = 2.0

_CACHE_PATH = os.path.join(paths.GUI_STATE_DIR, "skyview_cache.json")
_CACHE_VERSION = 2


def sector_of(az_deg):
    """Compass sector containing this azimuth (0°=N, 90°=E)."""
    az = float(az_deg) % 360.0
    index = int((az + SECTOR_WIDTH / 2.0) % 360.0 // SECTOR_WIDTH)
    return SECTOR_NAMES[index]


def sector_bounds(name):
    """(start, end) azimuth of a sector; start may be > end when it wraps N."""
    center = SECTOR_CENTER[name]
    return (center - SECTOR_WIDTH / 2.0) % 360.0, (center + SECTOR_WIDTH / 2.0) % 360.0


class HorizonMask:
    """Open compass sectors plus the elevation floor inside them.

    per_sector_min lets a single obstructed direction carry its own floor (the
    tree line to the south-west, say) without raising the floor everywhere.
    """

    def __init__(self, open_sectors=None, min_elev_deg=10.0, per_sector_min=None):
        self.open_sectors = frozenset(open_sectors if open_sectors is not None
                                      else ALL_SECTORS)
        self.min_elev_deg = float(min_elev_deg)
        self.per_sector_min = dict(per_sector_min or {})

    # ── queries ───────────────────────────────────────────────────────────
    def is_full_sky(self):
        return (self.open_sectors == ALL_SECTORS
                and not self.per_sector_min)

    def is_blind(self):
        return not self.open_sectors

    def floor_for(self, sector):
        return float(self.per_sector_min.get(sector, self.min_elev_deg))

    def contains(self, az_deg, el_deg):
        sector = sector_of(az_deg)
        if sector not in self.open_sectors:
            return False
        return float(el_deg) >= self.floor_for(sector)

    def describe(self):
        if self.is_blind():
            return "no sectors open — nothing is observable"
        if self.open_sectors == ALL_SECTORS:
            return f"full sky above {self.min_elev_deg:g}°"
        ordered = [s for s in SECTOR_NAMES if s in self.open_sectors]
        for label, sectors in PRESETS.items():
            if sectors == self.open_sectors and label != "Full sky":
                return f"{label} above {self.min_elev_deg:g}°"
        return f"{'·'.join(ordered)} above {self.min_elev_deg:g}°"

    # ── persistence ───────────────────────────────────────────────────────
    def to_dict(self):
        return {"open_sectors": sorted(self.open_sectors),
                "min_elev_deg": self.min_elev_deg,
                "per_sector_min": self.per_sector_min}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        sectors = data.get("open_sectors")
        valid = [s for s in (sectors or []) if s in ALL_SECTORS]
        return cls(open_sectors=frozenset(valid) if sectors is not None else ALL_SECTORS,
                   min_elev_deg=data.get("min_elev_deg", 10.0),
                   per_sector_min=data.get("per_sector_min") or {})


class Visibility:
    """How much of one pass falls inside the mask."""

    __slots__ = ("fraction", "start_unix", "end_unix", "best_el_deg",
                 "best_az_deg", "best_unix", "sectors_crossed", "error")

    def __init__(self, fraction=0.0, start_unix=0.0, end_unix=0.0,
                 best_el_deg=0.0, best_az_deg=0.0, best_unix=0.0,
                 sectors_crossed=(), error=""):
        self.fraction = fraction
        self.start_unix = start_unix
        self.end_unix = end_unix
        self.best_el_deg = best_el_deg
        self.best_az_deg = best_az_deg
        self.best_unix = best_unix
        self.sectors_crossed = tuple(sectors_crossed)
        self.error = error

    @property
    def visible(self):
        return self.fraction > 0.0 and not self.error

    @property
    def duration_s(self):
        return max(0.0, self.end_unix - self.start_unix)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k, cls.__slots__ and 0) for k in cls.__slots__
                      if k in d})


# ── receiver location ─────────────────────────────────────────────────────
_RECEIVER_RE = re.compile(
    r"receiver:\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)")


def receiver_location(plan_path=None):
    """(lat, lon, alt_m, source) for the antenna, or None.

    The plan file's own header wins: it records the coordinates the passes in
    THAT file were computed for, so a mask evaluated against them is consistent
    with the plan by construction. geometry.json is the fallback.
    """
    plan_path = plan_path or paths.PLAN_TSV
    try:
        with open(plan_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                m = _RECEIVER_RE.search(line)
                if m:
                    return (float(m.group(1)), float(m.group(2)),
                            float(m.group(3)), "plan header")
    except OSError:
        pass

    try:
        with open(paths.GEOMETRY_JSON, encoding="utf-8") as f:
            rx = json.load(f).get("receiver", {})
        if "latitude" in rx and "longitude" in rx:
            return (float(rx["latitude"]), float(rx["longitude"]),
                    float(rx.get("altitude_m", 0.0)), "geometry.json")
    except (OSError, ValueError, TypeError):
        pass
    return None


# ── evaluation ────────────────────────────────────────────────────────────
def _pass_key(p):
    return f"{p.catnr}|{int(p.rise_unix)}|{int(p.set_unix)}"


class VisibilityEngine:
    """Evaluates passes against a mask, with an on-disk cache.

    The expensive part is per-pass TLE propagation, and the results depend only
    on (pass, receiver) — NOT on the mask. So az/el tracks are cached and the
    mask is applied to the cached track, which makes changing the mask instant
    after the first pass over the data.
    """

    def __init__(self, receiver=None, samples=DEFAULT_SAMPLES):
        self.receiver = receiver or receiver_location()
        self.samples = samples
        self._tracks = {}          # pass key -> {"t": [...], "el": [...], "az": [...]}
        self._lock = threading.Lock()
        self._loaded = False

    # ── cache ─────────────────────────────────────────────────────────────
    def load_cache(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("version") != _CACHE_VERSION:
            return
        if data.get("receiver") != list(self.receiver or []):
            return          # antenna moved -> every cached track is wrong
        with self._lock:
            self._tracks.update(data.get("tracks") or {})

    def save_cache(self):
        paths.ensure_state_dirs()
        with self._lock:
            # Bound the file: keep the most recent tracks, drop the rest.
            items = sorted(self._tracks.items(),
                           key=lambda kv: kv[1].get("t", [0])[0], reverse=True)
            tracks = dict(items[:4000])
        try:
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"version": _CACHE_VERSION,
                           "receiver": list(self.receiver or []),
                           "tracks": tracks}, f)
        except OSError:
            pass

    # ── the propagation ───────────────────────────────────────────────────
    def _track_for(self, p):
        """Cached (times, el, az) for one pass. Propagates on a miss."""
        key = _pass_key(p)
        with self._lock:
            hit = self._tracks.get(key)
        if hit is not None:
            return hit["t"], hit["el"], hit["az"]

        tle1 = (p.extra or {}).get("tle_line1", "")
        tle2 = (p.extra or {}).get("tle_line2", "")
        if not tle1.strip() or not tle2.strip():
            raise ValueError("this pass carries no TLE, so its azimuth is unknown")
        if not self.receiver:
            raise ValueError("no receiver coordinates (plan header / geometry.json)")

        span = max(1.0, p.set_unix - p.rise_unix)
        n = max(4, min(self.samples, int(span / MIN_SAMPLE_STEP_S) + 1))
        times = [p.rise_unix + span * i / (n - 1) for i in range(n)]

        from . import _lazy_orbits  # heavy import, first use only
        el, az, _rng = _lazy_orbits.propagate(tle1, tle2, times, *self.receiver[:3])

        el = [float(v) for v in el]
        az = [float(v) for v in az]
        with self._lock:
            self._tracks[key] = {"t": times, "el": el, "az": az}
        return times, el, az

    def evaluate(self, p, mask):
        """Visibility of one pass under `mask`."""
        if mask.is_blind():
            return Visibility(error="no sectors open")
        try:
            times, el, az = self._track_for(p)
        except Exception as e:                          # noqa: BLE001
            return Visibility(error=str(e))

        inside = [mask.contains(a, e) for a, e in zip(az, el)]
        n_in = sum(inside)
        if not n_in:
            return Visibility(fraction=0.0,
                              sectors_crossed=_sectors_crossed(az))

        idx = [i for i, ok in enumerate(inside) if ok]
        best_i = max(idx, key=lambda i: el[i])
        return Visibility(
            fraction=n_in / len(inside),
            start_unix=times[idx[0]],
            end_unix=times[idx[-1]],
            best_el_deg=el[best_i],
            best_az_deg=az[best_i],
            best_unix=times[best_i],
            sectors_crossed=_sectors_crossed(az),
        )

    def evaluate_many(self, passes, mask, on_progress=None, should_stop=None):
        """{pass key: Visibility}. Cooperative — call from a worker thread."""
        out = {}
        total = len(passes)
        for i, p in enumerate(passes):
            if should_stop is not None and should_stop():
                break
            out[_pass_key(p)] = self.evaluate(p, mask)
            if on_progress and (i % 10 == 0 or i == total - 1):
                on_progress(i + 1, total)
        self.save_cache()
        return out

    def cached_count(self, passes):
        with self._lock:
            return sum(1 for p in passes if _pass_key(p) in self._tracks)

    def prefetch(self, passes, limit=12, should_stop=None):
        """Propagate up to `limit` uncached passes. WORKER THREAD ONLY.

        The skymap needs tracks for the handful of satellites that are up at
        one instant — a few, not the 580-row plan. This fills exactly those,
        bounded, so the map works without waiting for a full mask evaluation
        and without ever becoming one.
        """
        done = 0
        for p in passes:
            if should_stop is not None and should_stop():
                break
            if self.cached_track(p) is not None:
                continue
            try:
                self._track_for(p)
            except Exception:                              # noqa: BLE001
                continue      # no TLE on this row, or no receiver — skip it
            done += 1
            if done >= limit:
                break
        if done:
            self.save_cache()
        return done

    def cached_track(self, p):
        """(times, el, az) if this pass is already propagated, else None.

        NEVER propagates. The skymap draws on the Tk thread and calls this per
        satellite per redraw; a cache miss there must cost nothing, not an SGP4
        run that freezes the window.
        """
        with self._lock:
            hit = self._tracks.get(_pass_key(p))
        return (hit["t"], hit["el"], hit["az"]) if hit else None


def _sectors_crossed(az_values):
    seen = []
    for a in az_values:
        s = sector_of(a)
        if not seen or seen[-1] != s:
            seen.append(s)
    # Collapse a repeated sector picked up again after leaving it.
    ordered = []
    for s in seen:
        if s not in ordered:
            ordered.append(s)
    return ordered


def fmt_fraction(v):
    if v.error:
        return "?"
    if v.fraction <= 0:
        return "0%"
    return f"{v.fraction * 100:.0f}%"


def great_circle_label(az_deg):
    """'146° SE' — bearing with its sector, the way you would read a compass."""
    return f"{az_deg:.0f}° {sector_of(az_deg)}"


def suggest_capture_window(p, v, lead_s=10.0, max_s=None):
    """(start, duration) for the VISIBLE part of a pass, ready for --max-sec.

    soop_capture.sh centres a capped capture on the pass PEAK. When the peak is
    behind an obstruction that is the wrong centre, so this returns the window
    the mask actually allows, centred on the best visible moment.
    """
    if not v.visible:
        return None, 0.0
    start = max(p.rise_unix, v.start_unix - lead_s)
    duration = max(0.0, v.end_unix - start)
    if max_s and duration > max_s:
        half = max_s / 2.0
        start = max(start, v.best_unix - half)
        duration = max_s
    return start, duration
