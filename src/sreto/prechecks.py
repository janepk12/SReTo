"""
prechecks.py — is this machine actually ready to capture and analyse?

Every check here answers a question that has cost a real run before: a radio
that was not plugged in, a disk with 3 GB left in front of a 10-minute session,
a plan file from yesterday, a geometry.json that stopped being valid JSON.

Checks are pure Python (no subprocess except the radio probe), each one is
independent, and each returns a status plus a HINT — the fix, not just the
complaint. They run on a worker thread because the bladeRF probe talks to USB
and can block for a second or two.

Thresholds are borrowed from the tools themselves so the GUI and the scripts
agree about what "fine" means:
    DISK_HEADROOM_GB = 5   soop_capture.sh:150 (its own disk guard)
    PLAN_MAX_AGE_H   = 6   soop_capture.sh:147 (when it re-plans automatically)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from . import config, paths

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"

DISK_HEADROOM_GB = 5.0
PLAN_MAX_AGE_H = 6.0
TLE_STALE_DAYS = 3.0

ANALYSIS_MODULES = ("numpy", "scipy", "matplotlib", "skyfield")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""
    group: str = "general"


def _gb(n):
    return n / 1e9


# ── individual checks ─────────────────────────────────────────────────────
def check_science_repo():
    """Is a science repository configured, and does it hold the tools?

    First check in the list on purpose: when this fails, most of the checks
    below fail too, and a panel full of red rows hides the one that matters.
    """
    root, source = config.resolve()
    if root is None:
        return [Check("science repo", FAIL, "not configured",
                      f"set ${config.ENV_REPO_ROOT}, or run "
                      f"`sreto --set-repo /path/to/sdr_r`", "repo")]
    if not config.looks_like_science_repo(root):
        return [Check("science repo", FAIL, f"not a science repo: {root}",
                      f"resolved from {source}; expected to contain "
                      f"{', '.join(config.REPO_MARKERS)}", "repo")]
    return [
        Check("science repo", OK, root, f"resolved from {source}", "repo"),
        Check("state dir", INFO, config.state_dir(),
              "the only place SReTo writes", "repo"),
    ]


def check_scripts():
    """The tools the GUI shells out to must exist and be readable."""
    out = []
    for label, path, needs_exec in (
        ("capture.sh", paths.CAPTURE_SH, True),
        ("soop_capture.sh", paths.SOOP_CAPTURE_SH, True),
        ("soop_planner.py", paths.SOOP_PLANNER_PY, False),
        ("MAIN.py", paths.MAIN_PY, False),
    ):
        if not os.path.isfile(path):
            out.append(Check(label, FAIL, "not found", f"expected at {path}",
                             "tools"))
        elif not os.access(path, os.R_OK):
            out.append(Check(label, FAIL, "not readable", f"chmod +r {path}", "tools"))
        elif needs_exec and not os.access(path, os.X_OK):
            # Not fatal: the GUI invokes it as `bash <script>`, which does not
            # need the execute bit. Worth saying, because the CLI does.
            out.append(Check(label, WARN, "present, not executable",
                             f"chmod +x {path} so ./{os.path.basename(path)} works "
                             f"from a terminal too", "tools"))
        else:
            out.append(Check(label, OK, "present", "", "tools"))
    return out


def check_bladerf(probe=True, timeout=8):
    """bladeRF-cli on PATH, and a device actually attached."""
    exe = paths.bladerf_cli()
    if not exe:
        return [Check("bladeRF-cli", FAIL, "not on PATH",
                      "install the bladeRF host tools, or activate the "
                      "science env (conda activate sdrr) — capture.sh calls "
                      "bladeRF-cli by name. Using a different radio? "
                      "`sreto --radios` lists what is supported and what is "
                      "coming", "radio")]
    checks = [Check("bladeRF-cli", OK, exe, "", "radio")]
    if not probe:
        return checks

    try:
        proc = subprocess.run([exe, "-p"], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        checks.append(Check("bladeRF device", WARN, f"probe timed out after {timeout}s",
                            "the USB link may be wedged — unplug and replug the "
                            "board", "radio"))
        return checks
    except OSError as e:
        checks.append(Check("bladeRF device", FAIL, str(e), "", "radio"))
        return checks

    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and ("Serial" in text or "backend" in text.lower()):
        serial = ""
        for line in text.splitlines():
            if "serial" in line.lower():
                serial = line.strip()
                break
        checks.append(Check("bladeRF device", OK, serial or "device found", "", "radio"))
    else:
        checks.append(Check("bladeRF device", FAIL,
                            (text.strip().splitlines() or ["no device reported"])[0][:120],
                            "plug the board in and check the USB 3 cable — "
                            "captures will fail without it", "radio"))
    return checks


def check_disk():
    """Free space where captures land, against soop_capture.sh's own headroom."""
    out = []
    for label, path in (("capture disk", paths.DATA_DIR),
                        ("figures disk", paths.FIGURES_DIR)):
        target = path if os.path.isdir(path) else paths.REPO_ROOT
        try:
            usage = shutil.disk_usage(target)
        except OSError as e:
            out.append(Check(label, FAIL, str(e), "", "disk"))
            continue
        free = _gb(usage.free)
        detail = f"{free:.1f} GB free of {_gb(usage.total):.0f} GB"
        if free < DISK_HEADROOM_GB:
            out.append(Check(label, FAIL, detail,
                             f"soop_capture.sh stops below {DISK_HEADROOM_GB:.0f} GB "
                             f"headroom — free some space before capturing", "disk"))
        elif free < 4 * DISK_HEADROOM_GB:
            out.append(Check(label, WARN, detail,
                             "10 MS/s dual-channel 16-bit is ~0.8 GB per 10 s — "
                             "this will not last a long session", "disk"))
        else:
            out.append(Check(label, OK, detail, "", "disk"))
    return out


def check_directories():
    out = []
    for label, path, must_write in (
        ("02_DATA (captures)", paths.DATA_DIR, True),
        ("10_ANALYSIS", paths.ANALYSIS_DIR, True),
        ("20_SOOP_AVAILABILITY", paths.SOOP_DIR, True),
    ):
        if not os.path.isdir(path):
            out.append(Check(label, WARN, "does not exist yet",
                             "the tools create it on first write", "paths"))
        elif must_write and not os.access(path, os.W_OK):
            out.append(Check(label, FAIL, "not writable", f"chmod u+w {path}", "paths"))
        else:
            n = len([f for f in os.listdir(path) if not f.startswith(".")])
            out.append(Check(label, OK, f"{n} item(s)", "", "paths"))
    return out


def _importable_here(names):
    """{name: bool} in THIS interpreter, without importing anything heavy.

    importlib.util.find_spec only locates the module, so the GUI process never
    pays for numpy/matplotlib just to say they exist.
    """
    import importlib.util
    found = {}
    for mod in names:
        try:
            found[mod] = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found[mod] = False
    return found


def _importable_there(python, names, timeout=20):
    """{name: bool} in ANOTHER interpreter, or None when it cannot be asked.

    Needed because SReTo may be installed in its own venv while the analysis
    chain runs under the science environment: checking numpy in the GUI's
    interpreter would then answer a question nobody asked.
    """
    code = ("import importlib.util,json,sys;"
            "print(json.dumps({m: importlib.util.find_spec(m) is not None "
            "for m in sys.argv[1:]}))")
    try:
        proc = subprocess.run([python, "-c", code, *names],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def check_python_env():
    """The analysis chain's imports, checked in the interpreter that runs it.

    NOT FAIL — WARN. These are the ANALYSIS chain's dependencies (waterfalls,
    physics, the IQ dashboard). capture.sh and soop_capture.sh need none of
    them — they are bash calling bladeRF-cli — and neither does this package's
    own TUI, which is stdlib-only (see tui.py, jobs.py). A field Raspberry Pi
    set up only to record has no reason to carry numpy, and a red board there
    would report a rig that captures perfectly well as broken, which teaches
    the operator to stop reading the board.
    """
    python = paths.python_executable()
    same = os.path.realpath(python) == os.path.realpath(sys.executable)

    out = [Check("interpreter", OK, python,
                 "" if same else f"SReTo itself runs on {sys.executable}", "env")]

    found = (_importable_here(ANALYSIS_MODULES) if same
             else _importable_there(python, ANALYSIS_MODULES))
    if found is None:
        out.append(Check("analysis modules", WARN, "could not be probed",
                         f"{python} did not answer — check $SRETO_PYTHON", "env"))
        return out

    missing = []
    for mod in ANALYSIS_MODULES:
        if found.get(mod):
            out.append(Check(mod, OK, "importable", "", "env"))
        else:
            missing.append(mod)
            out.append(Check(mod, WARN, "not importable",
                             f"needed to ANALYSE, not to capture — install "
                             f"{mod} into {python} (the science repo's "
                             f"environment.yml lists it)", "env"))
    if missing:
        out.append(Check("capture without them", OK,
                         "capture.sh and soop_capture.sh need none of these",
                         "the planner needs numpy/matplotlib/skyfield; a manual "
                         "capture needs only bladeRF-cli", "env"))
    return out


def check_geometry():
    path = paths.GEOMETRY_JSON
    if not os.path.isfile(path):
        return [Check("geometry.json", WARN, "missing",
                      "MAIN.py falls back to its legacy ELEVATION_DEG constant",
                      "geometry")]
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return [Check("geometry.json", FAIL, f"unreadable: {e}",
                      "fix the JSON — every geometry-dependent stage reads it",
                      "geometry")]
    rx = cfg.get("receiver", {})
    if "latitude" in rx and "longitude" in rx:
        detail = (f"rx {rx.get('latitude'):.4f}, {rx.get('longitude'):.4f}, "
                  f"{rx.get('altitude_m', '?')} m")
        return [Check("geometry.json", OK, detail, "", "geometry")]
    return [Check("geometry.json", WARN, "no receiver lat/lon",
                  "soop_planner.py needs receiver coordinates", "geometry")]


def check_tle_cache():
    path = paths.TLE_CACHE
    if not os.path.isfile(path):
        return [Check("TLE cache", INFO, "empty",
                      "the first planner run fetches from CelesTrak", "geometry")]
    age_days = (time.time() - os.path.getmtime(path)) / 86400.0
    detail = f"{age_days:.1f} days old"
    if age_days > TLE_STALE_DAYS:
        return [Check("TLE cache", WARN, detail,
                      "TLEs age out fast — a refresh needs network access to "
                      "CelesTrak", "geometry")]
    return [Check("TLE cache", OK, detail, "", "geometry")]


def check_plan():
    path = paths.PLAN_TSV
    if not os.path.isfile(path):
        return [Check("capture plan", WARN, "not generated yet",
                      "run the SoOp planner — soop_capture.sh will otherwise "
                      "generate one itself on start", "soop")]
    age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    try:
        with open(path) as f:
            rows = sum(1 for line in f if line.strip() and not line.startswith("#"))
    except OSError as e:
        return [Check("capture plan", FAIL, str(e), "", "soop")]
    detail = f"{rows} pass(es), {age_h:.1f} h old"
    if age_h > PLAN_MAX_AGE_H:
        return [Check("capture plan", WARN, detail,
                      f"older than soop_capture.sh's {PLAN_MAX_AGE_H:.0f} h limit — "
                      f"it will re-plan automatically on start", "soop")]
    return [Check("capture plan", OK, detail, "", "soop")]


def check_master_log():
    path = paths.MASTER_CSV
    if not os.path.isfile(path):
        return [Check("master log", INFO, "no captures logged yet", "", "history")]
    try:
        with open(path) as f:
            rows = max(0, sum(1 for _ in f) - 1)
    except OSError as e:
        return [Check("master log", WARN, str(e), "", "history")]
    return [Check("master log", OK, f"{rows} capture(s)", "", "history")]


# bladeRF 2.0 micro. Used to find the board in sysfs without a USB library.
BLADERF_VENDOR_IDS = ("2cf0",)

# USB 2.0 tops out at 480 Mb/s of signalling, ~35-40 MB/s of real payload —
# below a single channel at 10 MS/s. A board that enumerated at high-speed
# instead of SuperSpeed will drop samples at any rate the experiment uses.
USB_SUPERSPEED_MIN_MBPS = 5000.0


USB_SYSFS_ROOT = "/sys/bus/usb/devices"
_USB_LINK = "USB link speed"


def check_usb_link(sysfs_root=None):
    """Did the bladeRF actually enumerate on USB 3?

    The classic silent field failure. A worn cable, a USB 2 hub, or the wrong
    port on the Pi gets you a link that negotiates at 480 Mb/s: the radio is
    found, `bladeRF-cli -p` is happy, the capture starts, and the only symptom
    is a SHORT file after the pass has ended. Nothing else in this board would
    catch it.

    Read from sysfs rather than probed, so it costs nothing and does not touch
    the device. Linux only — that is where the Pi is, and macOS exposes the
    same fact only through a slow system_profiler call.

    `sysfs_root` exists so the Linux branch is testable from a machine that has
    no sysfs, which is the machine this is developed on.
    """
    import glob
    root = sysfs_root or USB_SYSFS_ROOT
    if not os.path.isdir(root):
        return [Check(_USB_LINK, INFO, "not checkable on this OS",
                      f"read on Linux from {USB_SYSFS_ROOT}", "radio")]

    for vendor_file in sorted(glob.glob(os.path.join(root, "*", "idVendor"))):
        try:
            with open(vendor_file) as f:
                vendor = f.read().strip().lower()
        except OSError:
            continue
        if vendor not in BLADERF_VENDOR_IDS:
            continue
        device = os.path.dirname(vendor_file)
        try:
            with open(os.path.join(device, "speed")) as f:
                mbps = float(f.read().strip())
        except (OSError, ValueError):
            return [Check(_USB_LINK, WARN, "device found, speed unreadable",
                          "", "radio")]
        detail = f"{mbps:.0f} Mb/s"
        if mbps >= USB_SUPERSPEED_MIN_MBPS:
            return [Check(_USB_LINK, OK, f"{detail} (SuperSpeed)", "", "radio")]
        return [Check(_USB_LINK, FAIL, f"{detail} — this is USB 2",
                      "the board enumerated below USB 3: use a USB 3 cable and a "
                      "blue USB 3 port, with no USB 2 hub in between. Captures "
                      "above ~2 MS/s will drop samples and land SHORT.", "radio")]

    return [Check(_USB_LINK, INFO, "no bladeRF found on the USB bus",
                  "plug the board in to check the link speed", "radio")]


def check_geometry():
    path = paths.GEOMETRY_JSON
    if not os.path.isfile(path):
        return [Check("geometry.json", WARN, "missing",
                      "MAIN.py falls back to its legacy ELEVATION_DEG constant",
                      "geometry")]
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return [Check("geometry.json", FAIL, f"unreadable: {e}",
                      "fix the JSON — every geometry-dependent stage reads it",
                      "geometry")]
    rx = cfg.get("receiver", {})
    if "latitude" in rx and "longitude" in rx:
        detail = (f"rx {rx.get('latitude'):.4f}, {rx.get('longitude'):.4f}, "
                  f"{rx.get('altitude_m', '?')} m")
        return [Check("geometry.json", OK, detail, "", "geometry")]
    return [Check("geometry.json", WARN, "no receiver lat/lon",
                  "soop_planner.py needs receiver coordinates", "geometry")]


def check_tle_cache():
    path = paths.TLE_CACHE
    if not os.path.isfile(path):
        return [Check("TLE cache", INFO, "empty",
                      "the first planner run fetches from CelesTrak", "geometry")]
    age_days = (time.time() - os.path.getmtime(path)) / 86400.0
    detail = f"{age_days:.1f} days old"
    if age_days > TLE_STALE_DAYS:
        return [Check("TLE cache", WARN, detail,
                      "TLEs age out fast — a refresh needs network access to "
                      "CelesTrak", "geometry")]
    return [Check("TLE cache", OK, detail, "", "geometry")]


def check_plan():
    path = paths.PLAN_TSV
    if not os.path.isfile(path):
        return [Check("capture plan", WARN, "not generated yet",
                      "run the SoOp planner — soop_capture.sh will otherwise "
                      "generate one itself on start", "soop")]
    age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    try:
        with open(path) as f:
            rows = sum(1 for line in f if line.strip() and not line.startswith("#"))
    except OSError as e:
        return [Check("capture plan", FAIL, str(e), "", "soop")]
    detail = f"{rows} pass(es), {age_h:.1f} h old"
    if age_h > PLAN_MAX_AGE_H:
        return [Check("capture plan", WARN, detail,
                      f"older than soop_capture.sh's {PLAN_MAX_AGE_H:.0f} h limit — "
                      f"it will re-plan automatically on start", "soop")]
    return [Check("capture plan", OK, detail, "", "soop")]


def check_master_log():
    path = paths.MASTER_CSV
    if not os.path.isfile(path):
        return [Check("master log", INFO, "no captures logged yet", "", "history")]
    try:
        with open(path) as f:
            rows = max(0, sum(1 for _ in f) - 1)
    except OSError as e:
        return [Check("master log", WARN, str(e), "", "history")]
    return [Check("master log", OK, f"{rows} capture(s)", "", "history")]

ALL_CHECKS = (
    ("Science repository", check_science_repo),
    ("Tools", check_scripts),
    ("Python environment", check_python_env),
    ("Radio", check_bladerf),
    ("USB link", check_usb_link),
    ("Disk", check_disk),
    ("Directories", check_directories),
    ("Geometry", check_geometry),
    ("TLE cache", check_tle_cache),
    ("Capture plan", check_plan),
    ("History", check_master_log),
)


def run_all(probe_radio=True, on_result=None):
    """Run every check. on_result(section, [Check]) is called as each finishes.

    Never raises: a check that blows up becomes a FAIL row, because a preflight
    panel that crashes is worse than one that reports bad news.
    """
    results = []
    for section, fn in ALL_CHECKS:
        try:
            if fn is check_bladerf:
                checks = fn(probe=probe_radio)
            else:
                checks = fn()
        except Exception as e:                      # noqa: BLE001 - see docstring
            checks = [Check(section, FAIL, f"check crashed: {e}", "", "general")]
        results.extend(checks)
        if on_result:
            on_result(section, checks)
    return results


def summarise(checks):
    """(worst_status, counts) — what the header chip should say."""
    counts = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    if counts.get(FAIL):
        return FAIL, counts
    if counts.get(WARN):
        return WARN, counts
    return OK, counts
