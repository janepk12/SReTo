"""
diagnostics.py — say what actually went wrong.

A failed run already prints its cause: the traceback is right there. The GUI's
job is not to guess alongside it but to READ it and add the one thing a
traceback cannot give — what to do next, in terms of this pipeline.

The first version of this guessed from the exit code alone and told a user with
a single-channel capture to check for "a missing .json, an unresolved satellite,
or an unreachable CelesTrak". All three were wrong, the real answer was in the
exception two lines above, and the run had already failed four times. Hence:
match the actual output, and when nothing matches, quote the exception rather
than inventing a cause.

Rules are ordered — first match wins — so specific patterns must precede
general ones.
"""

import re

# (compiled pattern, headline, what to do). Checked against the run's output
# tail, most specific first.
_RULES = [
    (re.compile(r"\[PHASE\] Requires dual-channel data|"
                r"requires a dual-channel|"
                r"must be dual-channel|"
                r"Requires dual-channel"),
     "this capture is SINGLE-channel, and the pipeline is dual-channel",
     "Reflectometry compares rx1 (direct/RE) against rx2 (reflected/GR), so "
     "every stage past the power waterfall needs a ch1_2 capture.\n"
     "MAIN.py calls the phase stage unconditionally (MAIN.py:507) — unticking "
     "boxes will NOT get past it.\n"
     "Pick a capture whose name ends in _ch1_2, or re-capture with channels "
     "'1,2'."),

    (re.compile(r"FileNotFoundError.*has no \.json in any of"),
     "the capture's .json metadata was not found",
     "MAIN.py needs <capture>.json beside <capture>.bin, in one of its "
     "DATA_DIRS. Re-pick the capture, or check the .json was not moved."),

    (re.compile(r"\[tle\] ABORTING"),
     "the requested satellite could not be resolved",
     "CelesTrak rate-limits; retry, or set the satellite field empty to run "
     "config-driven. MAIN.py refuses to fall back to a DIFFERENT object's TLE "
     "on purpose — that would produce confident, wrong plots."),

    (re.compile(r"ModuleNotFoundError|ImportError"),
     "a Python module the pipeline needs is missing",
     "Launch the GUI from the sdrr env: conda activate sdrr. "
     "The Pre-checks tab lists which imports resolve."),

    (re.compile(r"MemoryError|Killed|Cannot allocate memory"),
     "the run was killed for using too much memory",
     "Lower the process fraction or raise the decimation factor on this tab. "
     "The estimate under 'Speed & memory' predicts this before you start."),

    (re.compile(r"PermissionError|Read-only file system"),
     "a file could not be written",
     "Check the output directory is writable (Pre-checks tab)."),

    (re.compile(r"No such device|Could not open device|libusb"),
     "the bladeRF could not be opened",
     "Check it is plugged in and no other process holds it (Pre-checks tab)."),
]

# The last exception line of a traceback: "SomeError: message".
_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit))\s*:\s*(.+)$",
                     re.MULTILINE)


def diagnose(output, returncode=None, kind=""):
    """(headline, guidance) for a failed run, or None when it looks fine.

    `output` is the run's console text (ANSI already stripped).
    """
    text = output or ""

    for pattern, headline, guidance in _RULES:
        if pattern.search(text):
            return headline, guidance

    # Nothing matched — quote the real exception instead of guessing.
    matches = _EXC_RE.findall(text)
    if matches:
        exc_type, message = matches[-1]
        return (f"{exc_type}: {message.strip()[:200]}",
                "The traceback above is the authoritative account. "
                "Nothing in the GUI's rule set matches this one.")

    return _from_returncode(returncode, kind)


def _from_returncode(returncode, kind):
    """Last resort: the exit code carries a little meaning on its own."""
    if returncode == 127:
        return ("exit 127 — command not found",
                "Is the sdrr env active? capture.sh calls bladeRF-cli by name.")
    if returncode in (130, -2):
        return ("exit 130 — interrupted (SIGINT)", "")
    if returncode in (137, -9):
        return ("exit 137 — killed by the OS, almost always out of memory",
                "Lower the process fraction or raise the decimation factor.")
    if kind in ("capture", "autocapture"):
        return ("the capture tool exited non-zero",
                "Check the radio is attached (Pre-checks tab) and that no other "
                "process is holding it.")
    return ("the run exited non-zero", "See the output above for the cause.")


def tail(text, lines=80):
    """Last `lines` lines — enough to hold a traceback, cheap to scan."""
    return "\n".join((text or "").splitlines()[-lines:])
