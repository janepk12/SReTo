"""
console.py — one shared terminal style for the whole reflectometry pipeline.

Every tool (MAIN, the IQ dashboard, the waterfalls, the physics extractor, the
SoOp planner) prints through say() / banner() / kv() so a run reads the same way
everywhere: a coloured, sectioned report that is ALSO mirrored verbatim (ANSI
stripped) into a plain-text run log when the caller asks for one.

This is the console the SoOp planner used to keep to itself, lifted out so the
rest of the pipeline can share it instead of each script re-inventing print().

    banner("IQ DASHBOARD")                 # ═══ boxed title ═══
    step_banner(1, 4, "FETCHING TLES")     # ═══ [1/4] boxed title ═══
    say("plain line")                      # print + capture for the log
    say("highlighted", "green")            # ANSI colour on a TTY, plain in logs
    kv("Coherence |γ|", "0.97")            #   aligned  key : value
    saved("/path/to/fig.png")              #   [saved] ...
    flush_log(save_dir, "iq_run")          # write logs/iq_run_<ts>.log

VENDORED COPY — byte-identical to 01_CODE/console.py in the sdr_r science
repo (this module has no local imports, so nothing needed to change). See
sreto.analysis's package docstring (__init__.py) for the sync story.
"""

import os
import re
import sys
from datetime import datetime, timezone

WIDTH = 104
KEY_W = 22  # default column width for kv() keys

_LOG_LINES = []
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_ANSI = {"bold": "\033[1m", "dim": "\033[2m", "green": "\033[32m",
         "yellow": "\033[33m", "red": "\033[31m", "cyan": "\033[36m",
         "magenta": "\033[35m", "reset": "\033[0m"}
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def use_color(flag):
    """Force colour on/off (defaults to: on iff stdout is a TTY and not NO_COLOR)."""
    global _USE_COLOR
    _USE_COLOR = bool(flag)


def c(text, *styles):
    """Wrap text in ANSI styles (no-op when not a TTY / NO_COLOR set)."""
    if not _USE_COLOR or not styles:
        return str(text)
    return "".join(_ANSI[s] for s in styles) + str(text) + _ANSI["reset"]


def say(text="", *styles):
    """print() that also records a plain (ANSI-free) copy for the run log."""
    _LOG_LINES.append(_ANSI_RE.sub("", str(text)))
    print(c(str(text), *styles))


def rule(char="─", style="cyan", width=WIDTH):
    say(char * width, style)


def banner(title, style="bold"):
    """Blank line + boxed title — the standard header for a pipeline stage."""
    say()
    rule("═", "cyan")
    say(f" {title}", style)
    rule("═", "cyan")


def step_banner(step, total, title):
    """Numbered variant used by the SoOp planner: ═══ [step/total] TITLE ═══."""
    banner(f"[{step}/{total}] {title}")


def kv(key, value, key_w=KEY_W, val_style=None):
    """Aligned 'key : value' line — the workhorse for summary blocks."""
    say(f"   {str(key):<{key_w}s}: {c(value, *((val_style,) if val_style else ()))}")


def formula(name, ref, *lines, result=None, result_style="cyan"):
    """One thesis equation, printed being evaluated.

        Γ_soil                                        [Eq. 04_forward_model]
           Γ = |R_vv(ε′,θ₀)|² · exp[−(2kσ cosθ₀)²] · exp[−2τ/cosθ₀]
             = 0.03118 · 0.54783 · 0.90056
             = 0.015382  (−18.13 dB)

    `name`   what is being computed, in the symbol the thesis uses.
    `ref`    the thesis equation label, or "" for a step that has none. It is
             printed flush right so a reader can go from the run log to the
             document without searching.
    `lines`  the derivation, one line per substitution. The FIRST is the
             symbolic form as written in the thesis; each one after it is the
             same equation one substitution further along. Callers pass the
             numbers they actually used — nothing is evaluated here, precisely
             so the printed derivation cannot disagree with the answer.
    `result` the answer, highlighted. Omit it when the last of `lines` already
             is the answer and needs no emphasis.

    Always printed, never folded behind -v: the substitution is the evidence
    for the number, and a stage that hides it is asking to be taken on trust.
    """
    head = f"   {name}"
    if ref:
        pad = max(1, WIDTH - len(head) - len(ref) - 3)
        head = f"{head}{' ' * pad}[{ref}]"
    say(head, "bold")
    for line in lines:
        say(f"      {line}")
    if result is not None:
        say(f"      = {c(str(result), result_style)}")


def saved(path):
    """The one canonical 'a file was written' line, used by every save_*()."""
    say(f"   [saved] {path}", "dim")


def note(text, style="dim"):
    say(f"   {text}", style)


def warn(text):
    say(f"   WARNING: {text}", "yellow")


def flush_log(save_dir, name):
    """Write the accumulated console lines to <save_dir>/logs/<name>_<ts>.log."""
    log_dir = os.path.join(save_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{name}_{ts}.log")
    with open(path, "w") as f:
        f.write("\n".join(_LOG_LINES) + "\n")
    return path


def reset_log():
    """Drop the captured lines (call at the start of a run that will flush_log)."""
    _LOG_LINES.clear()
