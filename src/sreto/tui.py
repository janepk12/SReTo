"""
tui.py — the terminal front-end, for when there is no screen to put a window on.

WHY THIS EXISTS
---------------
The windowed app is the wrong tool over SSH, and SSH is how this instrument
gets used in the field: the laptop runs an unattended overnight session and you
want to know, from somewhere else, whether it is still capturing, what the last
run did, and whether the analysis chain still works. Forwarding X11 to answer
that is absurd. So this is the same application driven from a menu.

IT IS A SECOND FRONT-END, NOT A SECOND IMPLEMENTATION
-----------------------------------------------------
Everything below the widgets was already free of Tk — main_params, jobs,
history, prechecks, radio_settings, soop_availability, system_open all import
nothing from tkinter. That separation was built for testability and it pays for
itself here: this module adds a menu and reuses that entire layer verbatim.

In particular, running the pipeline goes through the SAME mechanism the window
uses — main_params writes a parameterised COPY of MAIN.py into the state
directory and that copy is what runs. The science repo's MAIN.py is never
edited. A run started here and a run started from the Analysis tab are the same
run, logged the same way, with the same generated source on disk to prove what
executed.

IT NEEDS THE SCIENCE REPOSITORY, AND SAYS SO WHEN IT IS MISSING
---------------------------------------------------------------
SReTo is a front-end: capture.sh, soop_capture.sh and MAIN.py live in the
science repository (see config.py), not in this package. Every action here that
drives one of them is gated on `config.is_configured()` and refuses with the
same message the window shows, naming SRETO_REPO_ROOT. The menu still OPENS
without a repo — a tool that cannot explain why it is useless is worse than one
that does nothing.

Output goes through sreto.analysis.console — the same banner/kv/note vocabulary
the pipeline itself prints with, so a session reads as one continuous report
and drops its colour automatically when stdout is not a TTY (a log file, a
pipe, `ssh host 'sreto --status'`).
"""

import os
import subprocess
import sys
import time

from . import (config, history, jobs, main_params, paths, prechecks,
               radio_settings, system_open)
from .analysis import console as con

# Sustained write rates the capture must not outrun, in MB/s. Measured
# sequential figures, not interface maximums:
#
#   Pi 4 microSD   ~40   (DDR50-capped; 36-43 across cards)
#   Pi 5 microSD   ~50-70 (SDR104)
#   USB 3 SSD/UASP ~250-330 on either board
#
# Past SAFE, a Pi 4's card is already the limit. Past CEILING, no microSD of
# any kind keeps up and the capture will land SHORT. Both are deliberately
# conservative: benchmarks are run on an empty card, and an hours-long session
# gets roughly 70% of them once the card's SLC cache is full.
MICROSD_SAFE_MB_S = 40.0
MICROSD_CEILING_MB_S = 90.0

# Stage toggles, by the name they carry in MAIN.py's USER PARAMETERS block.
# Waterfalls are absent on purpose: MAIN.py runs them unconditionally, which is
# also why a single-channel capture cannot be analysed at all.
STAGE_TOGGLES = ("RUN_IQ_DASHBOARD_CALCULATIONS", "RUN_SIGNAL_XCORR",
                 "RUN_FRESNEL_FOOTPRINT", "RUN_CAPTURE_SKYMAP",
                 "RUN_PHYSICS_EXTRACTION")

# Each menu entry is a preset over those toggles. "Waterfalls only" is not a
# separate code path — it is every optional stage turned off, which is exactly
# what you would do by hand in the parameters block.
PRESETS = {
    "full":       {},                               # whatever MAIN.py says
    "waterfalls": {k: False for k in STAGE_TOGGLES},
    "physics":    {**{k: False for k in STAGE_TOGGLES},
                   "RUN_PHYSICS_EXTRACTION": True},
    "dashboard":  {**{k: False for k in STAGE_TOGGLES},
                   "RUN_IQ_DASHBOARD_CALCULATIONS": True},
    "xcorr":      {**{k: False for k in STAGE_TOGGLES},
                   "RUN_SIGNAL_XCORR": True},
}

# The self-checks reachable from here. Every one is a module the app actually
# runs, so a green board means the analysis chain is intact — which is the
# question you are asking when you check on a session from a train.
SELFTESTS = [
    ("physics retrieval", [sys.executable, "-m", "sreto.analysis.physics",
                           "--selftest"]),
    ("IQ dashboard", [sys.executable, "-m", "sreto.analysis.iq_dashboard",
                      "--selftest"]),
    ("band correlator", [sys.executable, "-m", "sreto.analysis.band_correlator",
                         "--selftest"]),
    ("SoOp planner", [sys.executable, "-m", paths.SOOP_PLANNER_MODULE,
                      "--selftest"]),
]


# ══════════════════════════════════════════════════════════════════════════
#  prompting
# ══════════════════════════════════════════════════════════════════════════
def _ask(prompt, default=""):
    """One line of input. EOF and Ctrl-C both mean 'go back', never 'proceed'.

    A closed stdin — `ssh host 'python MAIN.py --menu' < /dev/null`, or a job
    that lost its terminal — must not fall through into a capture or a
    multi-minute analysis with default answers nobody typed.
    """
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(con.c(f"   {prompt}{suffix}: ", "cyan")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer or default


def _ask_float(prompt, default):
    """A number, or None to go back. Re-asks rather than accepting nonsense."""
    while True:
        raw = _ask(prompt, str(default))
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            con.warn(f"{raw!r} is not a number")


def _confirm(prompt):
    answer = _ask(f"{prompt} (y/N)", "n")
    return answer is not None and answer.lower().startswith("y")


def _pause():
    _ask("press Enter to return to the menu")


def _needs_repo():
    """True when the action may proceed; otherwise explain and refuse.

    capture.sh, soop_capture.sh and MAIN.py live in the science repository, not
    in this package. Without one configured, a job's argv would name a script
    at a path built from an empty root — and the failure would surface as a
    shell "No such file or directory" from a subprocess, which tells the user
    nothing about SRETO_REPO_ROOT.
    """
    if config.is_configured():
        return True
    con.warn("no science repository configured")
    con.say()
    print(config.missing_repo_message())
    con.say()
    return False


# ══════════════════════════════════════════════════════════════════════════
#  running things
# ══════════════════════════════════════════════════════════════════════════
def _run(argv, cwd=None, env=None, echo=True, stdin_lines=None):
    """Run a subprocess with the terminal attached. Returns the exit code.

    stdio is INHERITED rather than captured: the pipeline draws progress bars
    with \\r and colours its own output, and piping it through here would turn
    a live run into a wall of text that arrives at the end. It also means Ctrl-C
    reaches the child directly, which is what stops a capture cleanly.

    `stdin_lines` answers an interactive script's prompts — capture.sh's prompt
    contract, the same sequence soop_capture.sh:605 sends and the same one
    gui.runner feeds through its pty. Only stdin is redirected; stdout stays on
    the terminal, so capture.sh's \\r progress bar still draws live.

    Note that capture.sh's prompt TEXT disappears in this mode: bash only
    renders `read -p` when stdin is a terminal. That is why the menu asks the
    questions itself and prints the answers back before starting — otherwise a
    field session would show a bare progress bar with no record of what it was
    told to record.
    """
    if echo:
        con.note("$ " + " ".join(str(a) for a in argv))
    # Flush before handing the terminal over. Python block-buffers stdout when
    # it is not a TTY, so `python MAIN.py --tests > run.log` (or the same thing
    # over ssh into a file) would otherwise interleave this menu's lines with
    # the child's in the wrong order — the summary appearing above the output
    # it summarises, which is worse than useless in a log you are reading later.
    sys.stdout.flush()
    sys.stderr.flush()
    environment = dict(os.environ)
    environment.setdefault("PYTHONPATH", paths.CODE_DIR)
    environment.update(env or {})
    argv = [str(a) for a in argv]
    cwd = cwd or paths.CODE_DIR
    try:
        if not stdin_lines:
            return subprocess.call(argv, cwd=cwd, env=environment)
        proc = subprocess.Popen(argv, cwd=cwd, env=environment,
                                stdin=subprocess.PIPE)
        try:
            payload = "".join(f"{line}\n" for line in stdin_lines)
            proc.stdin.write(payload.encode())
            proc.stdin.flush()
        except OSError:
            # BrokenPipeError included: the script exited before reading its
            # answers — a missing radio,
            # an unwritable capture directory. Its own message is already on
            # the terminal and its exit code is the real verdict, so let the
            # wait() below report that rather than masking it with a pipe error.
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # Ctrl-C already reached the child: it shares this process group.
            # Give it a moment to stop the radio and write its .json rather
            # than returning immediately and leaving it orphaned mid-capture.
            con.warn("interrupted — waiting for the capture to close cleanly")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 130
    except KeyboardInterrupt:
        con.warn("interrupted")
        return 130
    except OSError as e:
        con.warn(f"could not start: {e}")
        return 127


def _run_job(job):
    """Run a gui.jobs.Job the way the window would, minus the window."""
    con.banner(f"{job.name.upper()} — {job.summary}")
    started = time.time()
    code = _run(job.argv, cwd=job.cwd, env=job.env,
                stdin_lines=job.stdin_lines)
    elapsed = history.fmt_duration(time.time() - started)
    if code == 0:
        con.say(f"   finished in {elapsed}", "green")
        if job.output_dir:
            con.kv("output", job.output_dir)
    else:
        con.say(f"   FAILED (exit {code}) after {elapsed}", "red")
    return code


# ══════════════════════════════════════════════════════════════════════════
#  choosing a capture
# ══════════════════════════════════════════════════════════════════════════
def _capture_list(limit=20):
    return system_open.newest_captures(paths.data_dirs())[:limit]


def _describe_capture(bin_path, json_path):
    """One line: name, dual/single channel, size. Unreadable metadata is shown,
    not hidden — a capture whose .json glitched is exactly what you want to see."""
    import json as _json
    size = os.path.getsize(bin_path) if os.path.exists(bin_path) else 0
    try:
        with open(json_path, encoding="utf-8") as f:
            meta = _json.load(f)
        channels = str(meta.get("channels", "?"))
        freq = meta.get("frequency_MHz", "?")
        dual = "2" in channels
        tag = "dual" if dual else "SINGLE"
        return f"{freq} MHz  {tag:>6s}  {jobs.human_bytes(size):>10s}", dual
    except (OSError, ValueError):
        return f"{'':>18s}{jobs.human_bytes(size):>10s}  (metadata unreadable)", True


def _choose_capture():
    """Returns a capture .bin basename, or None."""
    captures = _capture_list()
    if not captures:
        con.warn("no captures found in " + ", ".join(paths.data_dirs()))
        return None

    con.banner("CAPTURES (newest first)")
    for i, (bin_path, json_path) in enumerate(captures, start=1):
        summary, dual = _describe_capture(bin_path, json_path)
        name = os.path.basename(bin_path)
        con.say(f"   {i:>3d}  {name:<52s} {summary}",
                *(() if dual else ("yellow",)))
    con.say()
    con.note("single-channel captures cannot run this pipeline at all — it "
             "compares rx1 (direct) against rx2 (reflected).")

    raw = _ask("capture number", "1")
    if raw is None:
        return None
    try:
        index = int(raw)
        bin_path, json_path = captures[index - 1]
    except (ValueError, IndexError):
        con.warn(f"{raw!r} is not one of 1..{len(captures)}")
        return None

    _summary, dual = _describe_capture(bin_path, json_path)
    if not dual and not _confirm(
            "That capture is SINGLE-channel and the run will fail at the phase "
            "stage. Continue anyway?"):
        return None
    return os.path.basename(bin_path)


def _ask_overrides(preset):
    """Capture + speed + band, as a MAIN.py override dict. None to go back."""
    name = _choose_capture()
    if name is None:
        return None

    overrides = dict(PRESETS.get(preset, {}))
    overrides["file_name"] = name

    defaults = {}
    try:
        defaults = main_params.read_defaults()
    except main_params.MainParamError as e:
        con.warn(str(e))

    con.say()
    fraction = _ask_float("process fraction 0-1 (speed knob)",
                          defaults.get("PROCESS_PERCENTAGE", 1.0))
    if fraction is None:
        return None
    overrides["PROCESS_PERCENTAGE"] = fraction

    if _confirm("set the analysis band (bandwidth + centre offset)"):
        bandwidth = _ask_float("analysis bandwidth (MHz)",
                               defaults.get("ANALYSIS_BANDWIDTH_MHZ", 1.0))
        if bandwidth is None:
            return None
        offset = _ask_float("centre frequency offset (MHz)",
                            defaults.get("CENTER_FREQ_OFFSET_MHZ", 0.0))
        if offset is None:
            return None
        overrides["ANALYSIS_BANDWIDTH_MHZ"] = bandwidth
        overrides["CENTER_FREQ_OFFSET_MHZ"] = offset
    return overrides


def _show_overrides(overrides):
    """What will change relative to MAIN.py, before anything runs."""
    try:
        defaults = main_params.read_defaults()
    except main_params.MainParamError:
        defaults = {}
    con.banner("PARAMETERS FOR THIS RUN")
    for key in sorted(overrides):
        current, new = defaults.get(key), overrides[key]
        marker = " " if str(current) == str(new) else "*"
        con.say(f"   {marker} {key:<32s} {str(current):<24s} -> {new}")
    con.note("* = differs from MAIN.py. MAIN.py itself is not modified; a "
             "parameterised copy is written to gui/.state/tmp/ and run.")


# ══════════════════════════════════════════════════════════════════════════
#  capturing
# ══════════════════════════════════════════════════════════════════════════
#  The menu used to stop short of the one thing the instrument is for. Every
#  entry below this line was reachable from the window and from a bare shell,
#  but not over SSH — so a field session on a headless machine had to fall back
#  to running capture.sh by hand and answering fourteen prompts from memory,
#  with no size estimate, no disk check and no record in the app's journal.
#
#  Nothing here reimplements a capture. Both actions build a gui.jobs.Job — the
#  same object the Capture and Automation tabs build — and hand it to the same
#  scripts. The menu's whole contribution is deciding what to type, which is
#  exactly what jobs.py says its contribution is.
# ══════════════════════════════════════════════════════════════════════════
def _ask_capture_values():
    """Walk capture.sh's prompt contract. Returns a values dict, or None.

    The order comes from jobs.CAPTURE_PROMPTS rather than being retyped here,
    so tests/test_capture_contract.py — which re-derives that order from
    capture.sh itself — guards this menu too. Blank keeps the script's own
    default, the same convention the GUI form uses.
    """
    saved = radio_settings.load()
    con.banner("CAPTURE — press Enter to accept each default")
    con.note("blank answers are sent as blank lines, which is how capture.sh "
             "selects its own defaults. Ctrl-C or EOF goes back.")
    con.say()

    values = {}
    for key, label, script_default in jobs.CAPTURE_PROMPTS:
        # AGC skips the matching gain prompt inside capture.sh, so skip it here
        # too — asking for a gain the script will never read would put the
        # remaining answers one line out of step for the rest of the sequence.
        if key == "rx1_gain" and (values.get("agc_rx1") or "off") in jobs.AGC_ON_VALUES:
            continue
        if key == "rx2_gain" and (values.get("agc_rx2") or "off") in jobs.AGC_ON_VALUES:
            continue
        answer = _ask(label, saved.get(key, script_default))
        if answer is None:
            return None
        values[key] = answer
    return values


def _show_capture_plan(values):
    """Size, duration and sustained write rate, before anything is recorded.

    The write rate is the part worth stopping for. capture.sh only discovers
    that the disk could not keep up AFTER the pass, by comparing the file it
    got against the file it expected, and by then the satellite has set. See
    jobs.capture_rate_bytes_per_sec.
    """
    size, duration, readable = jobs.estimate_capture(values)
    con.banner("THIS CAPTURE")
    con.kv("frequency", f"{values.get('freq') or '433'} MHz")
    con.kv("sample rate", f"{values.get('samplerate') or '2'} MS/s")
    con.kv("channels", values.get("channels") or "1,2")
    con.kv("bit mode", values.get("bitmode") or "16bit")

    if size is None:
        con.warn(f"cannot estimate the size: {readable}")
        return True

    con.kv("duration", f"{duration:.0f} s")
    con.kv("estimated size", readable)

    rate = jobs.capture_rate_bytes_per_sec(values)
    ok = True
    if rate:
        mb_s = rate / 1e6
        con.kv("sustained write", f"{mb_s:.1f} MB/s",
               val_style="red" if mb_s > 90 else "yellow" if mb_s > 40 else None)
        # Thresholds are measured sustained sequential write rates, not
        # marketing figures: Pi 4 microSD tops out at ~40 MB/s (DDR50-capped),
        # Pi 5 microSD at ~50-70 MB/s (SDR104), a USB 3 SSD with UASP at
        # ~250-330 MB/s. Benchmarks are on an empty card; budget ~70% of them
        # for an hours-long session once the SLC cache is exhausted.
        if mb_s > MICROSD_CEILING_MB_S:
            con.warn(f"{mb_s:.0f} MB/s is past every microSD card "
                     f"(Pi 4 ~40 MB/s, Pi 5 ~70 MB/s). On a Pi this drops "
                     f"samples and lands as a SHORT file — capture to a USB 3 "
                     f"SSD, halve the sample rate, or use 8bit.")
            ok = False
        elif mb_s > MICROSD_SAFE_MB_S:
            con.note(f"         {mb_s:.0f} MB/s is nothing to an SSD but is at or "
                     f"past a Pi 4's microSD (~40 MB/s) — check the size "
                     f"verdict after the run.")

    try:
        free = __import__("shutil").disk_usage(paths.DATA_DIR).free
        con.kv("free on disk", jobs.human_bytes(free),
               val_style="red" if free < size else None)
        if free < size:
            con.warn("this capture does not fit — free some space first")
            ok = False
    except OSError as e:
        con.kv("free on disk", f"unavailable ({e})")
    return ok


def action_capture():
    """One manual capture, through capture.sh, with its answers pre-filled."""
    if not _needs_repo():
        return
    values = _ask_capture_values()
    if values is None:
        return
    fits = _show_capture_plan(values)

    con.say()
    if not fits and not _confirm("start it anyway"):
        return
    if fits and not _confirm("start the capture"):
        return

    # Remember the radio for next time and for the automatic captures, which is
    # what the Capture TAB's Save does — one shared store, either front-end.
    radio_settings.save(values)
    _run_job(jobs.capture_job(values))


def action_autocapture():
    """An unattended SoOp session through soop_capture.sh.

    Blank fields emit no flag at all (not an empty one), so an untouched form
    runs the script's own defaults. List and dry-run are offered first because
    on a battery-powered rig, finding out what a session WOULD do is worth
    doing before committing the battery to it.
    """
    if not _needs_repo():
        return
    con.banner("AUTOMATIC SOOP CAPTURE SESSION")
    con.note("blank = the script's own default. Ctrl-C or EOF goes back.")
    con.say()

    mode = _ask("mode (list / dry-run / run)", "list")
    if mode is None:
        return
    mode = mode.strip().lower()
    if mode not in ("list", "dry-run", "run"):
        con.warn(f"{mode!r} is not list, dry-run or run")
        return

    values = {"run_mode": mode}
    for key, label, default in (
        ("for", "session length (45m / 6h / 1.5h / 2d)", "24h"),
        ("sat", "only satellites matching (blank = all)", ""),
        ("min_elev", "minimum peak elevation (deg)", "15"),
        ("max_sec", "cap each capture at (s)", "120"),
        ("budget_gb", "stop after about (GB)", "100"),
        ("count", "stop after N captures (blank = unlimited)", ""),
    ):
        answer = _ask(label, default)
        if answer is None:
            return
        values[key] = answer

    job = jobs.autocapture_job(values)
    con.banner("SESSION COMMAND")
    con.say("   " + jobs.describe(job))
    con.say()
    if mode == "run":
        con.note("this runs until the session ends or Ctrl-C. Over SSH, start "
                 "it inside tmux or screen so a dropped connection does not "
                 "take the session down with it.")
        if not _confirm("start the session"):
            return
    _run_job(job)


# ══════════════════════════════════════════════════════════════════════════
#  menu actions
# ══════════════════════════════════════════════════════════════════════════
def action_analysis(preset):
    if not _needs_repo():
        return
    overrides = _ask_overrides(preset)
    if overrides is None:
        return
    _show_overrides(overrides)
    if not _confirm("run it"):
        return
    try:
        job = jobs.analysis_job(overrides, nice=10)
    except main_params.MainParamError as e:
        con.warn(str(e))
        return
    _run_job(job)


def action_planner():
    if not _needs_repo():
        return
    con.banner("SOOP PASS PLANNER")
    mode = _ask("mode (now_plus_24h / tonight / custom hours)", "now_plus_24h")
    if mode is None:
        return
    values = {"mode": mode}
    if mode == "custom hours":
        hours = _ask_float("hours ahead", 24)
        if hours is None:
            return
        values = {"mode": "now_plus_24h", "hours": hours}
    _run_job(jobs.planner_job(values))


def action_prechecks():
    con.banner("PRE-FLIGHT CHECKS")
    probe = _confirm("probe the radio over USB (slower, needs the bladeRF)")
    checks = prechecks.run_all(probe_radio=probe)
    worst, counts = prechecks.summarise(checks)
    styles = {"OK": "green", "WARN": "yellow", "FAIL": "red", "INFO": "dim"}
    for check in checks:
        con.say(f"   [{check.status:^4s}] {check.name:<34s} {check.detail}",
                styles.get(check.status, "dim"))
        if check.hint and check.status in ("WARN", "FAIL"):
            con.note(f"         -> {check.hint}")
    con.say()
    con.say(f"   worst: {worst}   " +
            "  ".join(f"{k}={v}" for k, v in counts.items() if v),
            styles.get(worst, "dim"))


def action_tests(interactive=True):
    """Run the app's own self-checks and report a board. Returns an exit code.

    This is the reason the menu exists at all for a remote user: one command
    that answers 'is the analysis chain on that machine still correct', without
    a display and without remembering five module paths.
    """
    con.banner("SELF-CHECKS")
    results = []
    for name, argv in SELFTESTS:
        con.say()
        con.say(f"   ── {name} " + "─" * max(0, con.WIDTH - len(name) - 7), "dim")
        code = _run(argv, echo=False)
        results.append((name, code))

    con.banner("SELF-CHECK SUMMARY")
    for name, code in results:
        ok = code == 0
        con.say(f"   [{'PASS' if ok else 'FAIL'}] {name}",
                "green" if ok else "red")
    failed = [n for n, c in results if c != 0]
    con.say()
    if failed:
        con.say(f"   {len(failed)} of {len(results)} FAILED: "
                + ", ".join(failed), "red")
    else:
        con.say(f"   all {len(results)} self-checks passed", "green")
    if interactive:
        _pause()
    return 1 if failed else 0


def action_status():
    """Where everything is, what ran last, and how much room is left."""
    con.banner("STATUS")

    con.say(" paths", "bold")
    con.kv("repo", paths.REPO_ROOT)
    con.kv("application", paths.rel(paths.GUI_DIR))
    con.kv("captures", paths.rel(paths.DATA_DIR))
    con.kv("figures", paths.rel(paths.ANALYSIS_DIR))
    con.kv("python", sys.executable)

    con.say()
    con.say(" storage", "bold")
    try:
        usage = __import__("shutil").disk_usage(paths.REPO_ROOT)
        free_gb = usage.free / 1e9
        con.kv("free on disk", f"{free_gb:.1f} GB",
               val_style="red" if free_gb < 20 else None)
    except OSError as e:
        con.kv("free on disk", f"unavailable ({e})")
    try:
        con.kv("captured so far", f"{history.data_volume_gb():.1f} GB")
    except Exception as e:                                  # noqa: BLE001
        con.kv("captured so far", f"unavailable ({e})")

    con.say()
    con.say(" capture plan", "bold")
    plan = str(paths.PLAN_TSV)
    if os.path.isfile(plan):
        age_h = (time.time() - os.path.getmtime(plan)) / 3600.0
        con.kv("latest plan", paths.rel(plan))
        con.kv("age", f"{age_h:.1f} h",
               val_style="yellow" if age_h > 6 else "green")
    else:
        con.kv("latest plan", "none — run the planner")

    con.say()
    con.say(" recent runs", "bold")
    try:
        rows = history.merged_rows(limit=10)
    except Exception as e:                                  # noqa: BLE001
        con.warn(f"could not read the run history: {e}")
        rows = []
    if not rows:
        con.note("nothing recorded yet")
    for row in rows[:10]:
        style = {"SUCCESS": "green", "FAILED": "red",
                 "STOPPED": "yellow"}.get(row.status, "dim")
        con.say(f"   {history.fmt_berlin(row.unix, '%m-%d %H:%M')}  "
                f"{row.kind:<11s} {row.status:<8s} {str(row.title)[:52]}", style)


def _log_files(limit=25):
    """Every run log this installation writes, newest first.

    Three producers, one list: the app's per-run console captures, the
    unattended session logs soop_capture.sh appends to, and the planner's own.
    Which one you want depends on what went wrong, and hunting for them by path
    over SSH is exactly the friction this menu exists to remove.
    """
    roots = [(paths.GUI_LOG_DIR, "app run"),
             (paths.SOOP_LOG_DIR, "planner"),
             (paths.SOOP_AUTO_DIR, "auto session")]
    found = []
    for root, kind in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not name.endswith((".log", ".txt")):
                continue
            path = os.path.join(root, name)
            try:
                found.append((os.path.getmtime(path), path, kind))
            except OSError:
                continue
    found.sort(reverse=True)
    return found[:limit]


def action_logs():
    """List recent logs and print the tail of one. The reason for --status's
    sibling: status says what happened, a log says why."""
    logs = _log_files()
    if not logs:
        con.banner("LOGS")
        con.note("no logs yet — they appear once something has run")
        return

    con.banner(f"LOGS ({len(logs)} most recent)")
    for i, (mtime, path, kind) in enumerate(logs, start=1):
        age_h = (time.time() - mtime) / 3600.0
        size = os.path.getsize(path)
        con.say(f"   {i:>3d}  {kind:<13s} {os.path.basename(path):<44s} "
                f"{age_h:>6.1f} h  {jobs.human_bytes(size):>10s}")

    raw = _ask("log number (blank to go back)", "")
    if not raw:
        return
    try:
        _mtime, path, _kind = logs[int(raw) - 1]
    except (ValueError, IndexError):
        con.warn(f"{raw!r} is not one of 1..{len(logs)}")
        return

    lines_raw = _ask("how many lines from the end", "60")
    if lines_raw is None:
        return
    try:
        count = max(1, int(lines_raw))
    except ValueError:
        count = 60

    con.banner(os.path.basename(path))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.read().splitlines()[-count:]
    except OSError as e:
        con.warn(f"could not read it: {e}")
        return
    for line in tail:
        print(line)
    con.say()
    con.kv("full path", path)


def action_show_parameters():
    con.banner("MAIN.py USER PARAMETERS")
    try:
        defaults = main_params.read_defaults()
    except main_params.MainParamError as e:
        con.warn(str(e))
        return
    for key in sorted(defaults):
        con.kv(key, str(defaults[key]), key_w=32)
    con.say()
    con.note(f"edit them in {paths.rel(paths.MAIN_PY)}, or override per-run "
             f"from this menu.")


# ══════════════════════════════════════════════════════════════════════════
#  the menu
# ══════════════════════════════════════════════════════════════════════════
MENU = [
    # Capture first: it is what the instrument is for, and on the field rig it
    # is usually the only reason the session exists.
    ("c", "Capture                one manual capture, through capture.sh",
     action_capture),
    ("a", "Auto capture           unattended SoOp session (list / dry-run / run)",
     action_autocapture),
    ("1", "Full analysis          waterfalls + every stage MAIN.py has on",
     lambda: action_analysis("full")),
    ("2", "Waterfalls only        power + phase + 1D, band selectable",
     lambda: action_analysis("waterfalls")),
    ("3", "Physics retrieval      coherence -> soil moisture, scored",
     lambda: action_analysis("physics")),
    ("4", "IQ dashboard           scatter, envelopes, phase difference",
     lambda: action_analysis("dashboard")),
    ("5", "Band cross-correlation side-channel delay estimate",
     lambda: action_analysis("xcorr")),
    ("6", "SoOp pass planner      refresh the capture plan",
     action_planner),
    ("7", "Pre-flight checks      radio, disk, environment, plan age",
     action_prechecks),
    ("8", "Self-checks            run every test in the application",
     action_tests),
    ("9", "Status                 paths, storage, plan age, recent runs",
     action_status),
    ("l", "Logs                   read any run / session / planner log",
     action_logs),
    ("p", "Show MAIN.py parameters",
     action_show_parameters),
]


def _print_menu():
    con.banner("SReTo — SDR REFLECTOMETRY  ·  terminal menu")
    if config.is_configured():
        con.note(f"science repo: {paths.REPO_ROOT}")
    else:
        con.warn("no science repository configured — every action below that "
                 "drives the radio or the pipeline will refuse")
        con.note("fix it with:  sreto --set-repo /path/to/sdr_r")
    con.say()
    for key, label, _action in MENU:
        con.say(f"   [{key}]  {label}")
    con.say()
    con.say("   [q]  quit")


def menu():
    """The interactive loop. Returns a process exit code."""
    while True:
        _print_menu()
        choice = _ask("choose")
        if choice is None or choice.lower() in ("q", "quit", "exit"):
            con.say()
            con.note("bye")
            return 0
        for key, _label, action in MENU:
            if choice.lower() == key:
                try:
                    action()
                except KeyboardInterrupt:
                    con.say()
                    con.warn("cancelled")
                break
        else:
            con.warn(f"{choice!r} is not on the menu")


# ══════════════════════════════════════════════════════════════════════════
#  entry points — reached from __main__'s argparse, not from MAIN.py
#
#  The science repo's copy of this module is dispatched by MAIN.py, which has
#  to distinguish a TUI flag from "run the pipeline". SReTo has a real CLI, so
#  each action is simply a function __main__ calls for its own flag. Same
#  actions, same output; only the routing differs.
# ══════════════════════════════════════════════════════════════════════════
def run_menu():
    """`sreto --menu`."""
    return menu()


def run_tests():
    """`sreto --selftests` — one command that answers 'is the chain intact'."""
    return action_tests(interactive=False)


def run_status():
    """`sreto --status`."""
    action_status()
    return 0


def run_logs():
    """`sreto --logs`."""
    logs = _log_files()
    con.banner(f"LOGS ({len(logs)})")
    for mtime, path, kind in logs:
        age_h = (time.time() - mtime) / 3600.0
        con.say(f"   {kind:<13s} {age_h:>6.1f} h  {path}")
    if not logs:
        con.note("no logs yet")
    return 0


def run_captures():
    """`sreto --captures`."""
    captures = _capture_list(limit=200)
    con.banner(f"CAPTURES ({len(captures)})")
    for bin_path, json_path in captures:
        summary, _dual = _describe_capture(bin_path, json_path)
        con.say(f"   {os.path.basename(bin_path):<52s} {summary}")
    return 0
