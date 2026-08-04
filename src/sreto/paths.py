"""
paths.py — every filesystem location SReTo touches, resolved from this file.

Nothing here is hardcoded to a user's home directory. There are two roots and
they are deliberately kept apart:

    REPO_ROOT   the SCIENCE repository, resolved by config.py. SReTo only ever
                READS it. It may be absent, in which case every path derived
                from it is still a string but points at nothing, and
                ``config.is_configured()`` is False.

    STATE_DIR   the only place SReTo writes: journal, presets, logs and the
                transpiled MAIN.py copies. Outside both the science repo and
                the installed package, so an installed wheel never writes into
                site-packages and the science repo stays byte-identical.

The original in-tree version derived the repo root from ``__file__`` because
the GUI lived inside the science repo. It no longer does, so the derivation
moved to config.py and the constants below became *functions of that*.

Because the constants are read at import time by a lot of modules, they are
recomputed by :func:`refresh` whenever the configured root changes — call it
after ``config.set_repo_root``.
"""

import os
import shutil
import sys

from . import config

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PACKAGE_DIR, "assets")

# Kept as a name because a lot of code and several tests refer to "the GUI's
# own directory". For an installed package that is the package directory.
GUI_DIR = PACKAGE_DIR


def _init():
    """(Re)derive every repo-relative constant from the configured root."""
    global REPO_ROOT, CODE_DIR
    global CAPTURE_SH, SOOP_CAPTURE_SH, SOOP_PLANNER_PY, MAIN_PY, SATELLITES_PY
    global GEOMETRY_JSON, TLE_CACHE
    global DATA_DIR, FIGURES_DIR, ANALYSIS_DIR, SOOP_DIR, PLAN_TSV
    global MASTER_CSV, MASTER_JSONL, MASTER_FAILURES_CSV, ALT_DATA_DIR
    global GUI_STATE_DIR, RUN_JOURNAL, PRESETS_JSON, TMP_DIR, GUI_LOG_DIR

    root = config.repo_root()
    # A missing repo must not turn every path constant into None — hundreds of
    # os.path.join calls would start raising TypeError far from the cause. An
    # unresolvable root becomes a path that simply does not exist, which every
    # os.path.isfile guard in this package already handles.
    REPO_ROOT = root or os.path.join(config.state_dir(), "no-science-repo")
    CODE_DIR = os.path.join(REPO_ROOT, config.CODE_SUBDIR)

    # ── the CLI tools SReTo wraps (never modified) ─────────────────────────
    CAPTURE_SH = os.path.join(CODE_DIR, "capture.sh")
    SOOP_CAPTURE_SH = os.path.join(CODE_DIR, "soop_capture.sh")
    SOOP_PLANNER_PY = os.path.join(CODE_DIR, "soop_planner.py")
    MAIN_PY = os.path.join(CODE_DIR, "MAIN.py")
    SATELLITES_PY = os.path.join(CODE_DIR, "satellites.py")
    GEOMETRY_JSON = os.path.join(CODE_DIR, "geometry.json")
    TLE_CACHE = os.path.join(CODE_DIR, ".tle_cache.json")

    # ── data / output locations (mirror the constants inside the CLI tools) ─
    DATA_DIR = os.path.join(REPO_ROOT, "02_DATA")            # capture.sh BASE_DIR
    FIGURES_DIR = os.path.join(REPO_ROOT, "03_FIGURES")
    ANALYSIS_DIR = os.path.join(FIGURES_DIR, "ANALYSIS PLOTS")   # MAIN.py SAVE_DIR
    SOOP_DIR = os.path.join(FIGURES_DIR, "SOOP_AVAILABILITY")    # soop_planner SAVE_DIR
    PLAN_TSV = os.path.join(SOOP_DIR, "latest_capture_plan.tsv")
    MASTER_CSV = os.path.join(DATA_DIR, "master_experiment_log.csv")
    MASTER_JSONL = os.path.join(DATA_DIR, "master_experiment_log.jsonl")
    MASTER_FAILURES_CSV = os.path.join(DATA_DIR, "master_failures.csv")

    # Secondary capture location MAIN.py also searches (DATA_DIRS[1]).
    ALT_DATA_DIR = os.path.join(REPO_ROOT, "data_stream", "bladerf_stream")

    # ── SReTo-owned state (the only places SReTo writes) ───────────────────
    GUI_STATE_DIR = config.state_dir()
    RUN_JOURNAL = os.path.join(GUI_STATE_DIR, "runs.jsonl")     # history dashboard
    PRESETS_JSON = os.path.join(GUI_STATE_DIR, "presets.json")  # last-used values
    TMP_DIR = os.path.join(GUI_STATE_DIR, "tmp")                # MAIN.py copies
    GUI_LOG_DIR = os.path.join(GUI_STATE_DIR, "logs")           # console captures


_init()


def refresh():
    """Recompute every constant after the configured repo root changed."""
    _init()


def ensure_state_dirs():
    """Create SReTo's own directories. Never creates anything in the repo."""
    for d in (GUI_STATE_DIR, TMP_DIR, GUI_LOG_DIR):
        os.makedirs(d, exist_ok=True)


def have_science_repo():
    """True when the CLI tools SReTo drives are actually present."""
    return config.is_configured()


def python_executable():
    """The interpreter to run the repo's python tools with.

    SReTo is normally launched from the science env ('sdrr'), in which case
    sys.executable already IS that env's python and the analysis tools get the
    same numpy/matplotlib/bladerf the CLI gets. ``$SRETO_PYTHON`` overrides it,
    which is what an installed SReTo in its own venv needs: the GUI's
    interpreter and the pipeline's no longer have to be the same one.
    """
    return os.environ.get("SRETO_PYTHON") or sys.executable


def bladerf_cli():
    """Path to bladeRF-cli, or None when it is not on PATH."""
    return shutil.which("bladeRF-cli")


def data_dirs():
    """Capture directories, in the order MAIN.py searches them."""
    return [d for d in (DATA_DIR, ALT_DATA_DIR) if os.path.isdir(d)]


def rel(path):
    """Repo-relative path for display, absolute passthrough when outside."""
    try:
        r = os.path.relpath(path, REPO_ROOT)
        return path if r.startswith("..") else r
    except ValueError:
        return path
