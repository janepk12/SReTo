"""
_lazy_orbits.py — the one place SReTo reaches into the pipeline's propagator.

skyview.py needs azimuth, and the science repo's 01_CODE/orbits.py computes it
against the same TLEs the planner recorded. Reusing it means the GUI and the
figures cannot disagree about where a satellite was.

The cost is that importing orbits pulls in numpy (and skyfield on first
propagate), which is the opposite of what the GUI is meant to be. So the import
is deferred to the first call and isolated here — one module, one import, easy
to see. A session that never opens the sky-view filter never loads any of it.

Read-only: nothing here calls back into the pipeline's file-writing paths.
"""

import os
import sys
import threading

from . import config, paths

_lock = threading.Lock()
_orbits = None


def _load():
    """Import the science repo's orbits.py, wherever SReTo was launched from."""
    global _orbits
    if _orbits is not None:
        return _orbits
    with _lock:
        if _orbits is not None:
            return _orbits
        if not paths.have_science_repo():
            # Without this, the import below fails with a bare ImportError that
            # says 'orbits' and nothing about the actual cause.
            raise ImportError(config.missing_repo_message())
        # Loaded BY LOCATION, not by name. orbits.py moved into the science
        # repo's application package (01_CODE/gui/analysis/), so a sys.path
        # insert plus `import orbits` silently found nothing — every azimuth
        # went unresolved and the sky-view filter reported ZERO visible passes
        # instead of an error. paths.ANALYSIS_SRC_DIR knows both layouts.
        #
        # By location also means a module named `orbits` that happens to be
        # importable on this machine can never be picked up in its place.
        import importlib.util  # noqa: PLC0415
        src = os.path.join(paths.ANALYSIS_SRC_DIR, "orbits.py")
        if not os.path.isfile(src):
            raise ImportError(
                f"the science repo has no orbits.py at {src} — SReTo cannot "
                f"compute azimuth without it.")
        spec = importlib.util.spec_from_file_location("_science_orbits", src)
        orbits = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("_science_orbits", orbits)
        spec.loader.exec_module(orbits)
        _orbits = orbits
        return _orbits


def available():
    """(ok, reason) — can azimuth be computed on this machine?"""
    try:
        _load()
    except ImportError as e:
        if not paths.have_science_repo():
            return False, config.missing_repo_message()
        return False, (f"the science repo's 01_CODE/orbits.py could not be "
                       f"imported ({e}). Its environment needs numpy and "
                       f"skyfield — see SRETO_PYTHON in the README.")
    if not os.path.isfile(paths.GEOMETRY_JSON):
        # Not fatal: the plan header usually supplies the receiver.
        pass
    return True, ""


def propagate(tle_line1, tle_line2, times, rec_lat, rec_lon, rec_alt):
    """(elevation_deg, azimuth_deg, range_m). Azimuth is compass, 0°=N, 90°=E."""
    return _load().propagate_satellite(tle_line1, tle_line2, times,
                                       rec_lat, rec_lon, rec_alt)


def is_loaded():
    """True once the heavy import has actually happened."""
    return _orbits is not None
