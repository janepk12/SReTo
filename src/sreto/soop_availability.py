"""
soop_availability.py — what is overhead, and when.

MODULAR BY DESIGN. The panel does not know where passes come from; it asks a
PROVIDER. Two ship here:

  * CapturePlanProvider — reads the plan soop_planner.py already writes
    (03_FIGURES/SOOP_AVAILABILITY/latest_capture_plan.tsv). Real passes, real
    TLEs, zero extra computation, and it is the SAME file soop_capture.sh
    executes from — so the panel shows exactly what the automation will do.
  * PlaceholderProvider — synthetic passes, loudly labelled as such, so the UI
    is usable and testable when no plan exists yet.

TO INJECT YOUR OWN TARGET ALGORITHM
-----------------------------------
Subclass Provider, implement passes(), and register it:

    from sreto import soop_availability as sa

    class MyTargets(sa.Provider):
        name = "my-targets"
        label = "My target selection"
        def passes(self, horizon_h=24.0, min_elev_deg=10.0):
            return [sa.SatellitePass(name="…", catnr=…, rise_unix=…, …)]

    sa.register_provider(MyTargets())

The panel picks it up from its dropdown on the next refresh — no GUI edits.

Stdlib only and no propagation of its own: computing passes is soop_planner.py's
job, and duplicating SGP4 here would be a second source of truth for the one
number the whole experiment depends on.
"""

import math
import os
import time
from dataclasses import dataclass, field

from . import paths

PLAN_COLUMNS = [
    "rise_unix", "set_unix", "peak_unix", "duration_s", "peak_el_deg",
    "freq_mhz", "samplerate_mhz", "bandwidth_mhz", "geo", "catnr", "name",
    "rise_utc", "set_utc", "tle_line1", "tle_line2",
]


@dataclass
class SatellitePass:
    """One horizon-to-horizon opportunity. Times are unix seconds (UTC)."""
    name: str
    catnr: str = ""
    rise_unix: float = 0.0
    set_unix: float = 0.0
    peak_unix: float = 0.0
    peak_el_deg: float = 0.0
    freq_mhz: float = 0.0
    samplerate_mhz: float = 0.0
    bandwidth_mhz: float = 0.0
    geo: bool = False
    source: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def duration_s(self):
        return max(0.0, self.set_unix - self.rise_unix)

    def state(self, now=None):
        """'now' | 'upcoming' | 'past' — drives the row colour."""
        now = now if now is not None else time.time()
        if self.geo:
            return "now"          # geostationary: always up, never rises
        if self.rise_unix <= now <= self.set_unix:
            return "now"
        return "upcoming" if self.rise_unix > now else "past"

    def seconds_until_rise(self, now=None):
        now = now if now is not None else time.time()
        return self.rise_unix - now

    def estimated_gb(self, seconds=None):
        """Capture size at this pass's samplerate — 2 ch, 16-bit, 4 B/sample.

        The same arithmetic soop_capture.sh:587 does before committing to a
        pass, so the panel's number and the runner's disk guard agree.
        """
        dur = self.duration_s if seconds is None else seconds
        return dur * self.samplerate_mhz * 1e6 * 8 / 1e9


class Provider:
    """Source of SatellitePass objects."""

    name = "base"
    label = "Provider"
    is_placeholder = False

    def available(self):
        """Can this provider produce anything right now? (str reason if not)"""
        return True, ""

    def passes(self, horizon_h=24.0, min_elev_deg=10.0):
        raise NotImplementedError

    def describe(self):
        return self.label


class CapturePlanProvider(Provider):
    """The plan soop_planner.py writes and soop_capture.sh executes."""

    name = "plan"
    label = "Capture plan (soop_planner.py)"

    def __init__(self, plan_path=None):
        self.plan_path = plan_path or paths.PLAN_TSV

    def available(self):
        if not os.path.isfile(self.plan_path):
            return False, ("no plan file yet — run the SoOp planner "
                           "(Automation tab) to generate one")
        return True, ""

    def age_hours(self):
        try:
            return (time.time() - os.path.getmtime(self.plan_path)) / 3600.0
        except OSError:
            return None

    def header_lines(self):
        """The '#' preamble soop_planner writes (window, receiver, mask)."""
        out = []
        try:
            with open(self.plan_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith("#"):
                        break
                    out.append(line.lstrip("# ").rstrip())
        except OSError:
            pass
        return out

    def passes(self, horizon_h=24.0, min_elev_deg=10.0):
        ok, _ = self.available()
        if not ok:
            return []
        cutoff = time.time() + horizon_h * 3600.0
        out = []
        try:
            with open(self.plan_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line.strip() or line.startswith("#"):
                        continue
                    p = _parse_plan_row(line)
                    if p is None:
                        continue
                    if p.peak_el_deg < min_elev_deg:
                        continue
                    if p.rise_unix > cutoff:
                        continue
                    out.append(p)
        except OSError:
            return []
        out.sort(key=lambda p: (not p.geo, p.rise_unix))
        return out


def _parse_plan_row(line):
    fields = line.split("\t")
    if len(fields) < len(PLAN_COLUMNS) - 2:      # TLE lines may be absent
        return None
    row = dict(zip(PLAN_COLUMNS, fields))

    def num(key, default=0.0):
        try:
            return float(row.get(key, "") or default)
        except ValueError:
            return default

    return SatellitePass(
        name=row.get("name", "?").strip(),
        catnr=row.get("catnr", "").strip(),
        rise_unix=num("rise_unix"),
        set_unix=num("set_unix"),
        peak_unix=num("peak_unix"),
        peak_el_deg=num("peak_el_deg"),
        freq_mhz=num("freq_mhz"),
        samplerate_mhz=num("samplerate_mhz"),
        bandwidth_mhz=num("bandwidth_mhz"),
        geo=str(row.get("geo", "0")).strip() == "1",
        source="latest_capture_plan.tsv",
        extra={"tle_line1": row.get("tle_line1", ""),
               "tle_line2": row.get("tle_line2", ""),
               "rise_utc": row.get("rise_utc", "")},
    )


class PlaceholderProvider(Provider):
    """Synthetic passes so the panel is usable before real targets exist.

    NOT a propagator and not pretending to be one: evenly spaced windows with a
    plausible elevation shape, derived from a fixed seed so the view does not
    jitter between refreshes. Every row it emits is tagged 'PLACEHOLDER' and the
    panel says so above the table — this must never be mistaken for a schedule.
    """

    name = "placeholder"
    label = "Placeholder (synthetic — inject your own algorithm)"
    is_placeholder = True

    TEMPLATES = [
        # (name, catnr, freq MHz, samplerate MHz, period minutes)
        ("IRIDIUM 1xx", "43000", 1621.25, 10.0, 97),
        ("GLOBALSTAR M0xx", "39075", 2491.0, 4.0, 114),
        ("NOAA-19", "33591", 1698.0, 3.0, 102),
        ("METOP-C", "43689", 1701.3, 4.0, 101),
        ("GPS BIIF-x", "26407", 1575.42, 4.0, 718),
    ]

    def passes(self, horizon_h=24.0, min_elev_deg=10.0):
        now = time.time()
        base = now - (now % 600)          # stable across refreshes
        out = []
        for i, (name, catnr, freq, sr, period_min) in enumerate(self.TEMPLATES):
            step = period_min * 60.0
            t = base + (i + 1) * 480.0 - step
            while t < now + horizon_h * 3600.0:
                if t + 600 > now - 3600:
                    # A smooth 12°..82° spread, deterministic per pass.
                    elev = 12.0 + 70.0 * abs(math.sin(0.7 * i + 0.9 * (t / step)))
                    dur = 300.0 + 400.0 * (elev / 90.0)
                    if elev >= min_elev_deg:
                        out.append(SatellitePass(
                            name=f"{name} [PLACEHOLDER]",
                            catnr=catnr,
                            rise_unix=t,
                            set_unix=t + dur,
                            peak_unix=t + dur / 2,
                            peak_el_deg=elev,
                            freq_mhz=freq,
                            samplerate_mhz=sr,
                            bandwidth_mhz=sr,
                            geo=False,
                            source="placeholder",
                        ))
                t += step
        out.sort(key=lambda p: p.rise_unix)
        return out


# ── registry ──────────────────────────────────────────────────────────────
_PROVIDERS = {}


def register_provider(provider, make_default=False):
    """Add a provider to the panel's dropdown. Safe to call at import time."""
    _PROVIDERS[provider.name] = provider
    if make_default:
        _PROVIDERS["__default__"] = provider
    return provider


def get_provider(name):
    return _PROVIDERS.get(name)


def providers():
    return [p for k, p in _PROVIDERS.items() if k != "__default__"]


def default_provider():
    """The plan when it exists, the placeholder otherwise."""
    explicit = _PROVIDERS.get("__default__")
    if explicit is not None:
        return explicit
    plan = _PROVIDERS.get("plan")
    if plan is not None and plan.available()[0]:
        return plan
    return _PROVIDERS.get("placeholder")


register_provider(CapturePlanProvider())
register_provider(PlaceholderProvider())


# ── derived views for the panel ───────────────────────────────────────────
def summarise(passes, now=None):
    now = now if now is not None else time.time()
    up_now = [p for p in passes if p.state(now) == "now"]
    upcoming = [p for p in passes if p.state(now) == "upcoming"]
    next_pass = min(upcoming, key=lambda p: p.rise_unix) if upcoming else None
    return {
        "total": len(passes),
        "visible_now": len(up_now),
        "upcoming": len(upcoming),
        "next": next_pass,
        "best": max(passes, key=lambda p: p.peak_el_deg) if passes else None,
        "total_gb": sum(p.estimated_gb() for p in passes),
    }


def fmt_countdown(seconds):
    """'in 4m 12s' / 'now' / '2h 05m ago' — the panel's when column."""
    s = float(seconds)
    if -1 < s < 1:
        return "now"
    sign = "in " if s > 0 else ""
    suffix = "" if s > 0 else " ago"
    s = abs(s)
    if s < 60:
        body = f"{s:.0f}s"
    elif s < 3600:
        body = f"{int(s // 60)}m {int(s % 60):02d}s"
    elif s < 86400:
        body = f"{int(s // 3600)}h {int((s % 3600) // 60):02d}m"
    else:
        body = f"{int(s // 86400)}d {int((s % 86400) // 3600):02d}h"
    return f"{sign}{body}{suffix}"
