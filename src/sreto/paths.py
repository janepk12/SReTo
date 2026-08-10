"""
paths.py — every filesystem location SReTo touches, resolved from this file.

Nothing here is hardcoded to a user's home directory. There are three roots and
they are deliberately kept apart:

    REPO_ROOT   the SCIENCE repository, resolved by config.py. SReTo only ever
                READS it. It may be absent, in which case every path derived
                from it is still a string but points at nothing, and
                ``config.is_configured()`` is False.

    OUTPUT_ROOT where captures and figures live. The SAME as REPO_ROOT when one
                is configured — those are the pipeline's own 02_DATA and
                03_FIGURES, and SReTo still only reads them. Without a repo it
                is a tree SReTo owns and creates (config.output_root()), so a
                fresh clone has somewhere to put output instead of naming a
                placeholder directory nothing ever made.

    STATE_DIR   the only place SReTo writes *unconditionally*: journal, presets,
                logs and the transpiled MAIN.py copies. Outside both the science
                repo and the installed package, so an installed wheel never
                writes into site-packages and the science repo stays
                byte-identical.

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
    global REPO_ROOT, CODE_DIR, OUTPUT_ROOT
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
    # With a science repo these ARE the pipeline's own directories and SReTo
    # only reads them. Without one they used to point into a "no-science-repo"
    # placeholder that nothing ever created, so every output path named a
    # directory that did not exist. They now fall back to a root SReTo owns and
    # creates — see config.output_root() and ensure_output_dirs().
    OUTPUT_ROOT = REPO_ROOT if config.is_configured() else config.output_root()

    DATA_DIR = os.path.join(OUTPUT_ROOT, "02_DATA")          # capture.sh BASE_DIR
    FIGURES_DIR = os.path.join(OUTPUT_ROOT, "03_FIGURES")
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


OUTPUT_README = """\
SReTo output
============

Captures and figures land here because no science repository is configured,
so SReTo is using a tree of its own instead of the pipeline's 02_DATA and
03_FIGURES. The layout deliberately mirrors the science repo, so pointing
SReTo at a real one later (`sreto --set-repo /path/to/sdr_r`) changes only
which root these names hang off.

    02_DATA/                          IQ captures and the master logs
    03_FIGURES/ANALYSIS PLOTS/        what MAIN.py writes
    03_FIGURES/SOOP_AVAILABILITY/     pass plans and sky maps

Created automatically, and ignored by git (the `output/` line in .gitignore)
so multi-gigabyte captures can never be committed. Safe to delete: it is
rebuilt on the next start. Override the location with $SRETO_OUTPUT_DIR.
"""


def output_dirs():
    """The capture/figure directories, parents before children."""
    return (OUTPUT_ROOT, DATA_DIR, FIGURES_DIR, ANALYSIS_DIR, SOOP_DIR)


def ensure_output_dirs():
    """Create the capture/figure tree, but only when SReTo owns it.

    Returns the directories it had to create, so the console can say where the
    output went the first time — a tree appearing silently next to the source
    is worse than one that announces itself.

    Does nothing when a science repository is configured: those directories
    belong to the pipeline, capture.sh and MAIN.py create them on first write,
    and SReTo creating anything inside that repo would break the promise the
    whole of tests/test_nondestructive.py exists to enforce.
    """
    if have_science_repo():
        return []

    made = []
    for d in output_dirs():
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
                made.append(d)
            except OSError:
                return made   # unwritable parent — the pre-checks will say so

    readme = os.path.join(OUTPUT_ROOT, "README.txt")
    if made and not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(OUTPUT_README)
        except OSError:
            pass          # the directories are what matter; the note is a nicety
    return made


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
