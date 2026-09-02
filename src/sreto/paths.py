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

# ── per-capture figure kinds — mirrors 01_CODE/paths.env's FIG_* / the
# post-restructure ANALYSIS_DIR/<stem>/{waterfalls,dashboard,physics}/ layout.
FIG_WATERFALLS = "waterfalls"
FIG_DASHBOARD = "dashboard"
FIG_PHYSICS = "physics"
FIG_KINDS = (FIG_WATERFALLS, FIG_DASHBOARD, FIG_PHYSICS)


def _init():
    """(Re)derive every repo-relative constant from the configured root.

    Mirrors 01_CODE/paths.env's CURRENT layout (captures under 02_DATA/
    captures/<stem>/, figures under 03_FIGURES/10_ANALYSIS/<stem>/<kind>/ —
    the per-capture restructure). Kept in step by hand, the same way the rest
    of this port is: there is no shared source of truth to parse across two
    separate git repositories the way 01_CODE/repo_paths.py parses paths.env
    for capture.sh within ONE repo.
    """
    global REPO_ROOT, CODE_DIR
    global CAPTURE_SH, SOOP_CAPTURE_SH, SOOP_PLANNER_PY, MAIN_PY, SATELLITES_PY
    global GEOMETRY_JSON, TLE_CACHE
    global APP_DIR, SCIENCE_LAYOUT, SOOP_PLANNER_MODULE
    global ANALYSIS_SRC_DIR, CONFIG_SRC_DIR, SCRIPTS_SRC_DIR
    global DATA_DIR, CAPTURES_DIR, REGISTRY_DIR, SOOP_AUTO_DIR
    global FIGURES_DIR, LATEX_FIGURES_DIR, RESULT_FIGURES_DIR
    global ANALYSIS_DIR, SOOP_DIR, PLAN_TSV, SOOP_LOG_DIR
    global MASTER_CSV, MASTER_JSONL, MASTER_FAILURES_CSV, GROUND_TRUTH_JSON
    global LEGACY_DATA_DIR, LEGACY_ANALYSIS_DIR, ALT_DATA_DIR
    global GUI_STATE_DIR, RUN_JOURNAL, PRESETS_JSON, TMP_DIR, GUI_LOG_DIR

    root = config.repo_root()
    # A missing repo must not turn every path constant into None — hundreds of
    # os.path.join calls would start raising TypeError far from the cause. An
    # unresolvable root becomes a path that simply does not exist, which every
    # os.path.isfile guard in this package already handles.
    REPO_ROOT = root or os.path.join(config.state_dir(), "no-science-repo")
    CODE_DIR = os.path.join(REPO_ROOT, config.CODE_SUBDIR)

    # ── the CLI tools SReTo wraps (never modified) ─────────────────────────
    #
    # TWO SCIENCE-REPO LAYOUTS ARE SUPPORTED, and which one is in front of us
    # is decided by looking, not assumed:
    #
    #   "package" (current)  01_CODE/MAIN.py + 01_CODE/gui/{scripts,analysis,
    #                        config}/ — everything the app runs lives inside
    #                        gui/, so that directory can be copied as a unit.
    #   "flat" (legacy)      every tool loose in 01_CODE/.
    #
    # SReTo ships independently of the science repo and is pinned to neither,
    # so a user with an older clone must keep working. Each path falls back
    # individually rather than switching wholesale on one probe: a half-migrated
    # repo then still resolves what it has, instead of failing on all of it.
    APP_DIR = os.path.join(CODE_DIR, "gui")

    def _either(*candidates):
        for path in candidates:
            if os.path.exists(path):
                return path
        return candidates[-1]      # legacy location, so errors name it

    CAPTURE_SH = _either(os.path.join(APP_DIR, "scripts", "capture.sh"),
                         os.path.join(CODE_DIR, "capture.sh"))
    SOOP_CAPTURE_SH = _either(os.path.join(APP_DIR, "scripts", "soop_capture.sh"),
                              os.path.join(CODE_DIR, "soop_capture.sh"))
    SOOP_PLANNER_PY = _either(os.path.join(APP_DIR, "analysis", "soop_planner.py"),
                              os.path.join(CODE_DIR, "soop_planner.py"))
    SATELLITES_PY = _either(os.path.join(APP_DIR, "analysis", "satellites.py"),
                            os.path.join(CODE_DIR, "satellites.py"))
    GEOMETRY_JSON = _either(os.path.join(APP_DIR, "config", "geometry.json"),
                            os.path.join(CODE_DIR, "geometry.json"))

    # MAIN.py stays at the top of 01_CODE in both layouts — it is the terminal
    # entry point and deliberately not buried in the package.
    MAIN_PY = os.path.join(CODE_DIR, "MAIN.py")
    TLE_CACHE = os.path.join(CODE_DIR, ".tle_cache.json")

    # In the package layout the planner is a MODULE with relative imports, so
    # it must be launched `-m` from 01_CODE; by path it would fail with
    # "attempted relative import with no known parent package".
    SCIENCE_LAYOUT = ("package"
                      if os.path.isdir(os.path.join(APP_DIR, "analysis"))
                      else "flat")
    SOOP_PLANNER_MODULE = ("gui.analysis.soop_planner"
                           if SCIENCE_LAYOUT == "package" else None)

    # Where the pipeline SOURCE sits, for the parity checks that read the
    # science repo's plotting modules and shell config off disk. One name here
    # beats the same two-layout conditional repeated in every test.
    ANALYSIS_SRC_DIR = (os.path.join(APP_DIR, "analysis")
                        if SCIENCE_LAYOUT == "package" else CODE_DIR)
    CONFIG_SRC_DIR = (os.path.join(APP_DIR, "config")
                      if SCIENCE_LAYOUT == "package" else CODE_DIR)
    SCRIPTS_SRC_DIR = (os.path.join(APP_DIR, "scripts")
                       if SCIENCE_LAYOUT == "package" else CODE_DIR)

    # ── 02_DATA — raw acquisition only (mirrors paths.env exactly) ─────────
    DATA_DIR = os.path.join(REPO_ROOT, "02_DATA")
    CAPTURES_DIR = os.path.join(DATA_DIR, "captures")   # one dir per capture stem
    REGISTRY_DIR = os.path.join(DATA_DIR, "registry")   # the master logs
    SOOP_AUTO_DIR = os.path.join(DATA_DIR, "soop_auto")

    MASTER_CSV = os.path.join(REGISTRY_DIR, "master_experiment_log.csv")
    MASTER_JSONL = os.path.join(REGISTRY_DIR, "master_experiment_log.jsonl")
    MASTER_FAILURES_CSV = os.path.join(REGISTRY_DIR, "master_failures.csv")

    # In-situ ground truth — what a probe in the ground actually read, and the
    # only thing that can falsify the physics retrieval. This is the ONE
    # science-repo path SReTo writes: it is field data the physics menu
    # collects, not a tool it drives. With no science repo configured it lands
    # in SReTo's own state directory instead, so the menu works standalone.
    GROUND_TRUTH_JSON = (os.path.join(DATA_DIR, "ground_truth.json")
                         if root else
                         os.path.join(config.state_dir(), "ground_truth.json"))

    # ── 03_FIGURES — everything derived (mirrors paths.env exactly) ────────
    FIGURES_DIR = os.path.join(REPO_ROOT, "03_FIGURES")
    LATEX_FIGURES_DIR = os.path.join(FIGURES_DIR, "01_LATEX_FIGURES")
    RESULT_FIGURES_DIR = os.path.join(FIGURES_DIR, "02_RESULT_FIGURES")
    ANALYSIS_DIR = os.path.join(FIGURES_DIR, "10_ANALYSIS")      # + /<stem>/<kind>/
    SOOP_DIR = os.path.join(FIGURES_DIR, "20_SOOP_AVAILABILITY")
    PLAN_TSV = os.path.join(SOOP_DIR, "latest_capture_plan.tsv")
    SOOP_LOG_DIR = os.path.join(SOOP_DIR, "logs")

    # Pre-restructure locations. Readers fall back to these so a capture or a
    # figure that predates (or never received) the per-capture restructure
    # still resolves; nothing here is ever written to.
    LEGACY_DATA_DIR = DATA_DIR
    LEGACY_ANALYSIS_DIR = os.path.join(FIGURES_DIR, "ANALYSIS PLOTS")

    # Secondary capture location MAIN.py also searches (DATA_DIRS[-1]).
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
    """Capture directories, in the order MAIN.py's DATA_DIRS searches them:
    the new per-capture layout first, then the flat legacy root, then the
    streaming location. Mirrors 01_CODE/gui/paths.py's data_dirs()."""
    ordered = (CAPTURES_DIR, LEGACY_DATA_DIR, ALT_DATA_DIR)
    seen, out = set(), []
    for d in ordered:
        if d not in seen and os.path.isdir(d):
            out.append(d)
            seen.add(d)
    return out


# ── stems / per-capture figure directory (mirrors 01_CODE/repo_paths.py) ───
def capture_stem(path) -> str:
    """'…/20260615_152839_..._ch1_2.bin' -> the stem. Accepts the .bin, the
    .json, the .log, or the capture directory itself."""
    p = os.path.abspath(str(path)) if os.path.isdir(str(path)) else str(path)
    base = os.path.basename(p.rstrip(os.sep))
    if os.path.isdir(p):
        return base
    return base.split(".")[0]


def capture_dir(stem_or_path) -> str:
    """The directory holding one capture's raw .bin/.json/.log (new layout)."""
    return os.path.join(CAPTURES_DIR, capture_stem(stem_or_path))


def capture_file(stem_or_path, suffix: str) -> str:
    stem = capture_stem(stem_or_path)
    return os.path.join(capture_dir(stem), f"{stem}{suffix}")


def find_capture(stem_or_path, suffix: str = ".bin"):
    """Locate a capture file: new per-capture layout, then the legacy flat
    one. Returns a path or None. Mirrors repo_paths.find_capture exactly, so
    a capture this package can see is a capture sdr_r's own tools can see."""
    stem = capture_stem(stem_or_path)
    candidate = capture_file(stem, suffix)
    if os.path.isfile(candidate):
        return candidate
    legacy = os.path.join(LEGACY_DATA_DIR, f"{stem}{suffix}")
    return legacy if os.path.isfile(legacy) else None


def analysis_dir(stem_or_path, kind: str = "") -> str:
    """Where one capture's figures go: ANALYSIS_DIR/<stem>/<kind>/, `kind`
    one of FIG_KINDS or ''. Identical contract to repo_paths.analysis_dir(),
    so a capture analysed by MAIN.py and by this package's ported pipeline
    land in the SAME directory when both point at the same science repo."""
    if kind and kind not in FIG_KINDS:
        raise ValueError(f"unknown figure kind {kind!r}; expected one of {FIG_KINDS}")
    base = os.path.join(ANALYSIS_DIR, capture_stem(stem_or_path))
    return os.path.join(base, kind) if kind else base


def ensure_analysis_dir(stem_or_path, kind: str = "") -> str:
    d = analysis_dir(stem_or_path, kind)
    os.makedirs(d, exist_ok=True)
    return d


def rel(path):
    """Repo-relative path for display, absolute passthrough when outside."""
    try:
        r = os.path.relpath(path, REPO_ROOT)
        return path if r.startswith("..") else r
    except ValueError:
        return path
