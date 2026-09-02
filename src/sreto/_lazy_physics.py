"""
_lazy_physics.py — the one place the GUI reaches into the retrieval chain.

sreto.analysis.physics already owns the entire IQ → soil moisture argument: the
dielectric models, the ground-truth schema, and the stage-by-stage explanation
the physics menu displays. The GUI must not carry a second copy of any of it,
because a menu that explains a pipeline different from the one that runs is
worse than no menu.

The cost is that importing it pulls in numpy, scipy and matplotlib — about
1.5 s, which is the whole startup budget for a window that must feel instant.
So the import is deferred to the first call and isolated here, exactly as
_lazy_orbits.py does for the propagator. A session that never opens the Physics
tab never loads any of it.

Unlike _lazy_orbits, this reaches into the VENDORED sreto.analysis package
rather than a configured science repo, so the physics menu works on a bare
`pip install sreto[analysis]` with no sdr_r clone present.

WRITES: ground truth, and only ground truth. That file is field data living in
02_DATA, not code — see paths.GROUND_TRUTH_JSON. Nothing here writes 01_CODE.
"""

import threading

from . import paths

_lock = threading.Lock()
_physics = None


def _load():
    """Import 01_CODE/physics.py, whatever directory the GUI was launched from."""
    global _physics
    if _physics is not None:
        return _physics
    with _lock:
        if _physics is not None:
            return _physics
        from .analysis import physics     # noqa: PLC0415 — deferred on purpose
        _physics = physics
        return _physics


def available():
    """(ok, reason) — can the retrieval chain be described on this machine?"""
    try:
        _load()
    except ImportError as e:
        return False, (f"sreto.analysis.physics could not be imported ({e}). "
                       f"Install the analysis extra:  pip install "
                       f"'sreto[analysis]'  (numpy, scipy, matplotlib).")
    return True, ""


def is_loaded():
    """True once the heavy import has actually happened."""
    return _physics is not None


# ── the retrieval chain ────────────────────────────────────────────────────
def build_pipeline(summary=None, truth=None):
    """The ordered, explained stages. Works with summary=None (no run yet)."""
    return _load().build_pipeline(summary, truth)


def status_meaning():
    """{status: one-line explanation} — what MEASURED/ASSUMED/... actually mean."""
    return dict(_load().STATUS_MEANING)


def status_order():
    """Statuses worst-to-best, so a UI can rank without a second table."""
    return list(_load().STATUS_ORDER)


def score(theta_v, sigma, truth, capture=None):
    """Retrieved θ_v against the nearest in-situ reading, or None."""
    return _load().score_against_truth(theta_v, sigma, truth, capture)


# ── ground truth ───────────────────────────────────────────────────────────
def load_truth(path=None):
    """Read the ground-truth file. None when absent; raises on malformed JSON."""
    return _load().load_ground_truth(path or paths.GROUND_TRUTH_JSON)


def save_truth(data, path=None):
    """Write the ground-truth file. The only write this module performs."""
    return _load().save_ground_truth(path or paths.GROUND_TRUTH_JSON, data)


def default_truth():
    """The seeded config: one reading, sandy soil, grass cover."""
    return _load().default_ground_truth()


def texture_classes():
    """{class name: {'sand_pct', 'clay_pct'}} from the USDA texture triangle."""
    return dict(_load().SOIL_TEXTURE_CLASSES)


def texture_of(truth):
    """(sand_pct, clay_pct) for a ground-truth dict, or None."""
    return _load().texture_from_ground_truth(truth)
