"""
jobs.py — turn GUI form values into an invocation of the EXISTING tools.

Every function here returns a Job describing a command line (and, for
capture.sh, the stdin answers). Nothing is reimplemented: the GUI's whole
contribution is deciding what to type.

THE CAPTURE.SH PROMPT CONTRACT
------------------------------
capture.sh is interactive (`read -p`, capture.sh:11-110). Its questions arrive
in a fixed order, and two of them are conditional:

    1  frequency MHz            [433]
    2  samplerate MHz           [2]
    3  bandwidth MHz            [1]
    4  channels                 [1,2]
    5  antenna info             [none]
    6  experiment info          [none]
    7  bitmode                  [16bit]
    8  bias-tee                 [off]
    9  AGC rx1                  [off]
   10  rx1 gain                 [20]      ← only asked when AGC rx1 is not on
   11  AGC rx2                  [off]
   12  rx2 gain                 [20]      ← only asked when AGC rx2 is not on
   13  capture mode 1|2         [2]
   14  samples in M / timeout s [10]

soop_capture.sh:605-614 already answers exactly this sequence, and this module
answers the same one — which is why tests/test_capture_contract.py re-derives
the order from capture.sh itself and fails if the script ever changes.

BLANK MEANS DEFAULT. capture.sh does `freq=${freq:-433}` for every prompt, so
sending an empty line is how you take its built-in default. The GUI leans on
that deliberately: a form the user never touched produces a plain default
capture, identical to pressing Enter through the CLI.
"""

import os
import sys
from dataclasses import dataclass, field

from . import main_params, paths

# The prompt order above, as data. Index = the order the answers are sent in.
CAPTURE_PROMPTS = [
    ("freq", "Frequency (MHz)", "433"),
    ("samplerate", "Sample rate (MHz)", "2"),
    ("bandwidth", "Bandwidth (MHz)", "1"),
    ("channels", "Channels", "1,2"),
    ("antenna_info", "Antenna info", "none"),
    ("experiment_info", "Experiment info", "none"),
    ("bitmode", "Bit mode", "16bit"),
    ("biastee", "Bias-tee", "off"),
    ("agc_rx1", "AGC rx1", "off"),
    ("rx1_gain", "Gain rx1", "20"),          # conditional on agc_rx1
    ("agc_rx2", "AGC rx2", "off"),
    ("rx2_gain", "Gain rx2", "20"),          # conditional on agc_rx2
    ("capture_mode", "Capture mode", "2"),
    ("amount", "Duration (s) / samples (M)", "10"),
]

# capture.sh:51 / :62 — the gain prompt is skipped only for a literal on/ON.
AGC_ON_VALUES = ("on", "ON")


@dataclass
class Job:
    """One invocation of one existing tool."""
    name: str
    kind: str                       # capture | autocapture | analysis | planner
    argv: list
    cwd: str
    env: dict = field(default_factory=dict)
    stdin_lines: list = field(default_factory=list)
    nice: int = 0                   # 0 = normal priority
    output_dir: str = ""            # revealed in the file explorer when done
    artifact_dir: str = ""          # scanned for figures to open when done
    summary: str = ""               # one-line description shown in the console
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.argv = [str(a) for a in self.argv]
        self.stdin_lines = [str(s) for s in self.stdin_lines]


# ── capture.sh ─────────────────────────────────────────────────────────────
def capture_answers(values):
    """Answers for capture.sh's prompts, in its exact ask order.

    `values` maps the keys in CAPTURE_PROMPTS to strings; anything absent or
    blank stays blank, which makes capture.sh use its own default.
    """
    def get(key):
        return str(values.get(key, "") or "").strip()

    answers = [get(k) for k in ("freq", "samplerate", "bandwidth", "channels",
                                "antenna_info", "experiment_info", "bitmode",
                                "biastee", "agc_rx1")]
    # Blank AGC answers default to 'off' inside capture.sh, so the gain prompt
    # IS asked — mirroring the script's own `${agc_rx1:-off}` before the test.
    if (get("agc_rx1") or "off") not in AGC_ON_VALUES:
        answers.append(get("rx1_gain"))
    answers.append(get("agc_rx2"))
    if (get("agc_rx2") or "off") not in AGC_ON_VALUES:
        answers.append(get("rx2_gain"))
    answers.append(get("capture_mode"))
    answers.append(get("amount"))
    return answers


def estimate_capture(values):
    """Mirror capture.sh's own size arithmetic (capture.sh:93-122).

    Reproduced rather than guessed so the number the GUI shows before you press
    Start is the number capture.sh prints after you do. Returns
    (bytes, duration_sec, readable) or (None, None, reason).
    """
    def get(key, default):
        raw = str(values.get(key, "") or "").strip()
        return raw or default

    try:
        samplerate = float(get("samplerate", "2"))
        channels = get("channels", "1,2")
        bitmode = get("bitmode", "16bit")
        mode = get("capture_mode", "2")
        amount = float(get("amount", "10"))
    except ValueError:
        return None, None, "non-numeric value"

    if samplerate <= 0 or amount <= 0:
        return None, None, "sample rate and amount must be positive"

    num_channels = 2 if channels.replace(" ", "") in ("1,2", "2,1") else 1
    bytes_per_sample = 2 if bitmode == "8bit" else 4

    if mode == "1":                       # n million samples per channel
        n_hardware = amount * 1e6 * num_channels
        duration = max(1, round(amount / samplerate))
    else:                                 # timeout in seconds
        n_hardware = amount * samplerate * 1e6 * num_channels
        duration = amount

    size = n_hardware * bytes_per_sample
    return size, duration, human_bytes(size)


def capture_rate_bytes_per_sec(values):
    """Sustained bytes/second the capture must be written to disk at.

    This is the number that decides whether a capture SUCCEEDS or lands as a
    SHORT file, and until now nothing in the app said it out loud. bladeRF
    streaming is real-time: the samples arrive whether or not the storage
    underneath can absorb them, and when it cannot, the radio drops them and
    capture.sh reports a size mismatch after the pass is over and gone.

        rate = samplerate x channels x bytes_per_sample

    Worth knowing before pressing Start on a machine that is not the
    workstation. 10 MS/s dual-channel 16-bit is 80 MB/s, which a laptop SSD
    swallows and a Raspberry Pi microSD card (~40-90 MB/s, and that is the
    burst figure, not the sustained one) does not. Same command, same radio,
    different outcome.

    Returns bytes/sec, or None when the form does not describe a capture yet.
    """
    try:
        samplerate = float(str(values.get("samplerate", "") or "2").strip())
    except ValueError:
        return None
    if samplerate <= 0:
        return None
    channels = str(values.get("channels", "") or "1,2").strip()
    num_channels = 2 if channels.replace(" ", "") in ("1,2", "2,1") else 1
    bytes_per_sample = 2 if str(values.get("bitmode", "") or "").strip() == "8bit" else 4
    return samplerate * 1e6 * num_channels * bytes_per_sample


def human_bytes(n):
    """capture.sh's format_bytes(), in Python (capture.sh:79)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def capture_job(values):
    freq = values.get("freq") or "433"
    mode = (values.get("capture_mode") or "2").strip()
    amount = values.get("amount") or "10"
    unit = "M samples/ch" if mode == "1" else "s"
    return Job(
        name="capture.sh",
        kind="capture",
        argv=["bash", paths.CAPTURE_SH],
        cwd=paths.CODE_DIR,
        stdin_lines=capture_answers(values),
        # capture.sh writes into CAPTURES_DIR/<stem>/ (one directory per
        # capture, generated at runtime from the timestamp) — the GUI cannot
        # know the exact stem ahead of time, so it reveals the parent that
        # actually receives new captures now, not the flat 02_DATA root that
        # holds only registry/soop_auto/captures subdirectories today.
        output_dir=paths.CAPTURES_DIR,
        summary=f"manual capture — {freq} MHz, {amount} {unit}",
        meta={"frequency_mhz": freq, "experiment": values.get("experiment_info", "")},
    )


# ── soop_capture.sh ────────────────────────────────────────────────────────
def autocapture_job(values):
    """Build soop_capture.sh's argv. Only non-empty fields become flags, so an
    untouched form runs the script's own defaults (24 h, all targets >=15 deg)."""
    argv = ["bash", paths.SOOP_CAPTURE_SH]

    def flag(name, key, transform=str):
        raw = values.get(key)
        if raw is None:
            return
        text = str(raw).strip()
        if text:
            argv.extend([name, transform(text)])

    if str(values.get("session_mode", "for")) == "until" and values.get("until"):
        flag("--until", "until")
    else:
        flag("--for", "for")

    flag("--sat", "sat")
    flag("--min-elev", "min_elev")
    flag("--max-sec", "max_sec")
    flag("--budget-gb", "budget_gb")
    flag("--gain", "gain")
    flag("--bw", "bw")
    flag("--sr", "sr")
    flag("--lead", "lead")
    flag("--plan", "plan")

    if values.get("next_only"):
        argv.append("--next")
    else:
        flag("--count", "count")

    if values.get("include_geo"):
        argv.append("--include-geo")
    if values.get("refresh"):
        argv.append("--refresh")
    elif values.get("no_refresh"):
        argv.append("--no-refresh")

    mode = values.get("run_mode", "run")
    if mode == "list":
        argv.append("--list")
    elif mode == "dry-run":
        argv.append("--dry-run")

    label = {"list": "queue listing", "dry-run": "dry run"}.get(mode, "session")
    return Job(
        name="soop_capture.sh",
        kind="autocapture",
        argv=argv,
        cwd=paths.CODE_DIR,
        output_dir=paths.DATA_DIR,
        summary=f"automated SoOp {label}" + (
            f" — {values.get('sat')}" if values.get("sat") else ""),
        meta={"mode": mode, "sat": values.get("sat", "")},
    )


# ── soop_planner.py ────────────────────────────────────────────────────────
def planner_job(values):
    # In the current science-repo layout the planner is a package module with
    # relative imports and MUST be launched with -m from 01_CODE; in the legacy
    # flat layout it is a standalone file launched by path. paths decides which
    # by looking at the repo, so this stays correct for either clone.
    if paths.SOOP_PLANNER_MODULE:
        argv = [paths.python_executable(), "-m", paths.SOOP_PLANNER_MODULE]
    else:
        argv = [paths.python_executable(), paths.SOOP_PLANNER_PY]
    if values.get("selftest"):
        argv.append("--selftest")
    else:
        if values.get("mode"):
            argv.extend(["--mode", str(values["mode"])])
        if values.get("hours"):
            argv.extend(["--hours", str(values["hours"])])
    return Job(
        name="soop_planner.py",
        kind="planner",
        argv=argv,
        cwd=paths.CODE_DIR,
        env={"PYTHONPATH": paths.CODE_DIR},
        nice=int(values.get("nice", 0) or 0),
        output_dir=paths.SOOP_DIR,
        artifact_dir=paths.SOOP_DIR,
        summary="SoOp pass planner",
        meta={"mode": values.get("mode", "")},
    )


# ── MAIN.py (waterfalls / IQ dashboard / physics) ──────────────────────────
def analysis_job(overrides, nice=10):
    """Run MAIN.py's pipeline with GUI parameters, via a throwaway copy.

    See main_params.py for why the copy exists. PYTHONPATH is set because a
    script run from outside 01_CODE gets ITS OWN directory on sys.path, not the
    cwd — without this, MAIN.py's `import console` would fail.
    """
    run_copy = main_params.write_run_copy(overrides)
    # SAVE_DIR is no longer a free-text override (see main_params.PARAM_SPECS'
    # comment) — MAIN.py computes it itself as ANALYSIS_DIR/<capture stem>/,
    # keyed by file_name, so two captures cannot collide. Reveal/open must
    # follow the SAME per-capture directory MAIN.py actually wrote to, not
    # the flat ANALYSIS_DIR root (which now holds one subfolder per capture
    # ever analysed, not this run's figures).
    file_name = overrides.get("file_name") or ""
    save_dir = paths.analysis_dir(file_name) if file_name else paths.ANALYSIS_DIR
    return Job(
        name="MAIN.py",
        kind="analysis",
        argv=[paths.python_executable(), run_copy],
        cwd=paths.CODE_DIR,
        env={"PYTHONPATH": paths.CODE_DIR},
        nice=int(nice or 0),
        output_dir=save_dir,
        artifact_dir=save_dir,
        summary=f"analysis — {os.path.basename(str(overrides.get('file_name', '?')))}",
        meta={"file_name": overrides.get("file_name", ""),
              "run_copy": run_copy,
              "process_percentage": overrides.get("PROCESS_PERCENTAGE"),
              "satellite": overrides.get("SATELLITE_QUERY")},
    )


# ── physics (the retrieval on its own) ─────────────────────────────────────
def physics_job(values, nice=10):
    """Run the retrieval on one capture — soil moisture, without the rest.

    Unlike analysis_job, this runs the VENDORED sreto.analysis.physics rather
    than a file in the science repo's 01_CODE. That is the whole point of
    vendoring it: the physics menu works on a bare `pip install sreto[analysis]`
    with no sdr_r clone present, needing only the capture .bin/.json pair.

    `-m` rather than a path because the module lives inside an installed
    package, wherever pip put it; `sys.executable` is used directly for the
    same reason — the package must be importable by the interpreter that runs
    it, which is this one, not whichever python the science repo prefers.

    Blank fields are omitted rather than sent as empty flags, so an untouched
    form runs physics.py's own PhysicsConfig defaults.
    """
    json_path = str(values.get("json_path") or "")
    save_dir = str(values.get("save_dir") or paths.ANALYSIS_DIR)
    argv = [sys.executable, "-m", "sreto.analysis.physics", json_path,
            "--save-dir", save_dir]
    for flag, key in (("--elevation", "elevation"),
                      ("--pol", "polarization"),
                      ("--block-sec", "block_sec"),
                      ("--bw-khz", "bw_khz"),
                      ("--offset-mhz", "offset_mhz"),
                      ("--soil-model", "soil_model"),
                      ("--reference", "reference_json"),
                      ("--ground-truth", "ground_truth")):
        text = str(values.get(key, "") or "").strip()
        if text:
            argv.extend([flag, text])

    return Job(
        name="physics",
        kind="physics",
        argv=argv,
        cwd=paths.CODE_DIR if os.path.isdir(paths.CODE_DIR) else os.getcwd(),
        nice=int(nice or 0),
        output_dir=save_dir,
        artifact_dir=save_dir,
        summary=f"physics retrieval — {os.path.basename(json_path) or '?'}",
        meta={"file_name": os.path.basename(json_path),
              "soil_model": values.get("soil_model", ""),
              "ground_truth": values.get("ground_truth", "")},
    )


def describe(job):
    """The command line, quoted the way a shell would need it."""
    import shlex
    line = " ".join(shlex.quote(a) for a in job.argv)
    if job.nice:
        line = f"nice -n {job.nice} {line}"
    return line
