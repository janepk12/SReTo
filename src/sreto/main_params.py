"""
main_params.py — parameterise MAIN.py without editing MAIN.py.

MAIN.py has no CLI: its settings are plain module-level assignments inside the
'USER PARAMETERS' block (MAIN.py:30-242), and the file is meant to be edited by
hand. The GUI must not do that — the moment it rewrites MAIN.py, running MAIN.py
from a terminal stops meaning what the user last typed there, and the dual-mode
guarantee is gone.

So instead: read MAIN.py, rewrite the chosen assignment LINES in memory, and
write the result to a throwaway file under SReTo's own state directory
(``paths.TMP_DIR``). That copy is what runs — MAIN.py itself is never opened
for writing.

Three properties make this safe rather than clever:

  * Only lines INSIDE the USER PARAMETERS block are touched. Several of these
    names are legitimately reassigned by the engine further down (MAIN.py:275
    MAX_SAMPLES, MAIN.py:453 WINDOW_START_SEC); blind whole-file substitution
    would clobber that logic.
  * Every override must match exactly one assignment. A missing or duplicated
    name raises instead of silently running with the old value — if someone
    renames a parameter in MAIN.py, the GUI says so loudly.
  * The output is parsed with ast before it is allowed to run, so a bad value
    is a dialog, not a traceback three minutes into a capture read.

The temp copy runs with cwd=01_CODE and PYTHONPATH=01_CODE so its bare
`import console` / `from waterfalls import …` resolve exactly as they do for
`python MAIN.py`.
"""

import ast
import os
import re
import time

from . import config, paths

BLOCK_START_MARK = "USER PARAMETERS"
BLOCK_END_MARK = "end of user parameters"

# Provenance comment lines prepended to every generated copy. Exported so the
# tests can strip it and compare the body line-for-line against MAIN.py.
HEADER_LINE_COUNT = 6

# Every name the GUI is allowed to drive, with the widget it deserves.
# kind: float | int | bool | str | choice | path
PARAM_SPECS = [
    # (name, kind, label, group, help)
    ("file_name", "str", "Capture file", "capture",
     "The .bin (or .json) to analyse. Must live in one of MAIN.py's DATA_DIRS."),

    ("PROCESS_PERCENTAGE", "float", "Process fraction (0–1)", "speed",
     "The speed knob. Runtime is dominated by reading the .bin, so 0.1 runs in "
     "about a tenth of the time — and analyses only the FIRST 10% of the record."),
    ("GLOBAL_DECIMATION_FACTOR", "int", "Decimation factor", "speed",
     "Waterfalls: FFT frames averaged per displayed row (drives MEMORY almost "
     "linearly). IQ dashboard: true sample decimation, so bandwidth becomes "
     "samplerate/N — keep it small when the dashboard is on."),
    ("MAX_SAMPLES", "int", "Max samples", "speed",
     "Hard ceiling on samples loaded. MAIN.py lowers it further to match "
     "PROCESS_PERCENTAGE so the IQ dashboard reuses the waterfalls' cached read."),

    ("ANALYSIS_BANDWIDTH_MHZ", "float", "Analysis bandwidth (MHz)", "band",
     "Width of the 1D extraction window. The 2D panels always show the FULL "
     "captured bandwidth and only shade this slice."),
    ("CENTER_FREQ_OFFSET_MHZ", "float", "Centre offset (MHz)", "band",
     "Offset of the extraction window from the tuned centre frequency."),

    ("RUN_IQ_DASHBOARD_CALCULATIONS", "bool", "IQ dashboard", "stages",
     "05_xx figures: IQ scatter, envelopes, phase difference, cross-correlation."),
    ("RUN_SIGNAL_XCORR", "bool", "Band cross-correlation", "stages",
     "08_xx figures. Side-channel: nothing else in the pipeline reads its results."),
    ("RUN_FRESNEL_FOOTPRINT", "bool", "Fresnel footprint", "stages",
     "07_00. Needs a resolved satellite, however it was resolved."),
    ("RUN_CAPTURE_SKYMAP", "bool", "Capture skymap", "stages",
     "07_02. Needs a resolved satellite and a capture with a known window."),
    ("RUN_PHYSICS_EXTRACTION", "bool", "Physics extraction", "stages",
     "Soil moisture + phase altimetry from the coherence. Uncalibrated output "
     "is labelled relative until REFERENCE_JSON points at a metal-plate run."),

    ("SATELLITE_QUERY", "str", "Satellite (name / CATNR)", "geometry",
     "Manual override, wins over every automatic route. Empty = fall back to "
     "the capture sidecar or geometry.json."),
    ("GEOMETRY_AUTO_FROM_CAPTURE", "bool", "Auto geometry from capture", "geometry",
     "Read the transmitter from the capture's .soop.json sidecar / catnr tag."),

    ("WINDOW_START_SEC", "float", "IQ window start (s)", "window",
     "Overridden by geometry.json 'timing' when that file supplies it."),
    ("WINDOW_END_SEC", "float", "IQ window end (s)", "window", ""),
    ("WINDOW_NUM_SAMPLES", "int", "IQ window samples", "window",
     "Points drawn in the IQ scatter. Large values freeze the plot."),

    ("XCORR_THRESHOLD_PCT", "float", "X-corr amplitude gate (pct)", "xcorr",
     "Keep samples above this envelope percentile, per channel."),
    ("XCORR_DC_NOTCH_KHZ", "float", "X-corr DC notch (kHz)", "xcorr",
     "Not cosmetic: the shared LO's wandering DC offset is ~99% common-mode "
     "and buries the correlation under a pedestal. 0 disables."),
    ("XCORR_MAX_LAG_US", "float", "X-corr max lag (µs)", "xcorr",
     "Half-width of the lag axis. 1000 µs = ±300 km of path difference."),
    ("XCORR_BLOCK_SEC", "float", "X-corr block (s)", "xcorr",
     "Coherent block length; keep inter-chain drift well under a radian across it."),
    ("XCORR_COMPARE_UNGATED", "bool", "Also correlate ungated", "xcorr",
     "Control run. Thresholding is nonlinear — this says whether it helped. "
     "Roughly doubles this stage's runtime."),

    ("COHERENCE_THRESHOLD", "float", "Coherence threshold |γ|", "physics",
     "Blocks below this are masked (NaN)."),
    ("BLOCK_SEC", "float", "Coherent block (s)", "physics",
     "Averaging window per retrieval point."),
    ("ELEVATION_DEG", "float", "Elevation fallback (deg)", "physics",
     "Legacy fallback, used only when geometry.json is absent."),
    ("POLARIZATION", "choice", "Polarization", "physics", "H or V.",),

    ("SAVE_PLOTS", "bool", "Save plots", "output", ""),
    # SAVE_DIR is deliberately absent. It is no longer a user-editable literal
    # in MAIN.py: figures go to 03_FIGURES/10_ANALYSIS/<capture stem>/, derived
    # from file_name so two captures cannot overwrite each other's output.
    # Exposing it as a free-text path would let a user re-create exactly the
    # collision the per-capture directory was introduced to prevent. See
    # jobs.analysis_job(), which derives the run's actual output directory
    # from file_name via paths.analysis_dir() instead of reading this.
]

CHOICES = {"POLARIZATION": ["V", "H"]}

SPEC_BY_NAME = {s[0]: s for s in PARAM_SPECS}

GROUP_TITLES = {
    "capture": "Capture file",
    "speed": "Speed & memory",
    "band": "Analysis band",
    "stages": "Pipeline stages",
    "geometry": "Transmitter geometry",
    "window": "IQ window",
    "xcorr": "Band cross-correlation",
    "physics": "Physics extraction",
    "output": "Output",
}


class MainParamError(RuntimeError):
    """MAIN.py does not look the way the GUI expects."""


def _read_source():
    """MAIN.py's text, or MainParamError naming the fix.

    An unreadable MAIN.py is turned into MainParamError rather than allowed to
    escape as OSError, because every caller already handles the former and the
    two situations are the same one to a user. Without this, a machine with no
    science repository configured could not even BUILD the Analysis tab: the
    panel calls read_defaults() from its constructor, the FileNotFoundError
    propagated through App._build, and the window never appeared — while the
    rest of the app is specifically designed to open and explain itself with no
    repository present.
    """
    try:
        with open(paths.MAIN_PY, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        if not config.is_configured():
            raise MainParamError(
                "No science repository is configured, so MAIN.py's parameters "
                "cannot be read. Set one with `sreto --set-repo /path/to/sdr_r` "
                "(or $SRETO_REPO_ROOT) and press 'Reload MAIN.py defaults'."
            ) from e
        raise MainParamError(
            f"{paths.MAIN_PY} could not be read ({e.strerror}). The configured "
            f"science repository is {paths.REPO_ROOT} — check that it is the "
            f"right one with `sreto --where`."
        ) from e


def _block_bounds(lines):
    """(start, end) line indices of the USER PARAMETERS block, end exclusive."""
    start = end = None
    for i, line in enumerate(lines):
        if start is None and BLOCK_START_MARK in line:
            start = i
        elif start is not None and BLOCK_END_MARK in line:
            end = i
            break
    if start is None or end is None:
        raise MainParamError(
            f"{paths.MAIN_PY} has no '{BLOCK_START_MARK}' … "
            f"'{BLOCK_END_MARK}' block — the GUI cannot locate its parameters. "
            f"Either restore those marker lines or edit MAIN.py directly.")
    return start, end


def read_defaults():
    """Current values of the USER PARAMETERS block, straight from MAIN.py.

    Parsed, never executed: ast.parse + literal_eval, so importing MAIN.py's
    heavyweight dependencies (or running its pipeline) is not involved.
    """
    src = _read_source()
    lines = src.splitlines()
    start, end = _block_bounds(lines)

    values = {}
    tree = ast.parse(src, filename=paths.MAIN_PY)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (start < node.lineno <= end + 1):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass          # an expression, not a literal — not GUI-drivable
    return values


# Fields where "left blank" is meaningful and must become None, not "".
OPTIONAL_NAMES = {"SATELLITE_QUERY", "ELEVATION_DEG", "WINDOW_END_SEC"}


def format_value(name, value):
    """Python source literal for one override, validated against its spec."""
    spec = SPEC_BY_NAME.get(name)
    kind = spec[1] if spec else None

    if isinstance(value, str) and not value.strip() and name in OPTIONAL_NAMES:
        # MAIN.py branches on `if SATELLITE_QUERY:` / `is not None` — an empty
        # string would technically work but None is what those branches document.
        return "None"
    if value is None:
        return "None"
    if name == "SATELLITE_QUERY":
        # MAIN.py accepts a catalog NAME, a CATNR, or any CelesTrak name. A
        # numeric answer is emitted as an int so it reads like the CATNR it is.
        text = str(value).strip()
        return repr(int(text)) if text.isdigit() else repr(text)
    if kind == "bool":
        return "True" if bool(value) else "False"
    if kind == "int":
        return repr(int(value))
    if kind == "float":
        return repr(float(value))
    if kind in ("str", "path", "choice"):
        return repr(str(value))
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def build_overridden_source(overrides):
    """MAIN.py's source with the given names reassigned. MAIN.py is untouched."""
    src = _read_source()
    lines = src.splitlines()
    start, end = _block_bounds(lines)

    remaining = dict(overrides)
    seen = {}

    for i in range(start, end):
        line = lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not m:
            continue
        name = m.group(1)
        if name not in remaining:
            continue
        if name in seen:
            raise MainParamError(
                f"'{name}' is assigned twice inside MAIN.py's USER PARAMETERS "
                f"block (lines {seen[name] + 1} and {i + 1}) — the GUI refuses "
                f"to guess which one drives the run.")
        seen[name] = i
        lines[i] = f"{name} = {format_value(name, remaining[name])}"

    missing = [n for n in remaining if n not in seen]
    if missing:
        raise MainParamError(
            "MAIN.py's USER PARAMETERS block has no assignment for: "
            + ", ".join(sorted(missing))
            + ".\nThe GUI will not run with values it cannot verify took effect. "
              "Update sreto/main_params.py:PARAM_SPECS to match MAIN.py.")

    header = [
        "# ─────────────────────────────────────────────────────────────────────",
        "# GENERATED by 01_CODE/gui — a throwaway copy of MAIN.py with the GUI's",
        f"# parameters substituted ({time.strftime('%Y-%m-%d %H:%M:%S')}).",
        "# MAIN.py itself is unmodified; edit and run THAT for CLI work.",
        f"# overridden: {', '.join(sorted(remaining)) or '(nothing)'}",
        "# ─────────────────────────────────────────────────────────────────────",
    ]
    assert len(header) == HEADER_LINE_COUNT      # kept in step with the tests
    return "\n".join(header + lines) + "\n"


def write_run_copy(overrides, tmp_dir=None):
    """Materialise the parameterised MAIN copy. Returns its path.

    Validates by parsing: a malformed value fails here, before any subprocess
    starts and long before the several minutes it takes to read a .bin.
    """
    source = build_overridden_source(overrides)
    try:
        ast.parse(source, filename="<MAIN.py override>")
    except SyntaxError as e:
        raise MainParamError(
            f"the substituted parameters do not parse as Python "
            f"(line {e.lineno}: {e.msg}). Check the value of the field "
            f"nearest that line.") from e

    tmp_dir = tmp_dir or paths.TMP_DIR
    os.makedirs(tmp_dir, exist_ok=True)
    _prune_old_copies(tmp_dir)

    path = os.path.join(tmp_dir, f"MAIN_gui_{time.strftime('%Y%m%d_%H%M%S')}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    return path


def _prune_old_copies(tmp_dir, keep=10):
    """Keep the last few generated copies — they are the record of what ran."""
    try:
        copies = sorted(
            (os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
             if f.startswith("MAIN_gui_") and f.endswith(".py")),
            key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for old in copies[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
