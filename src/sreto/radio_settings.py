"""
radio_settings.py — ONE answer to "what will this capture actually use?".

THE PROBLEM THIS SOLVES
-----------------------
A manual capture takes its parameters from the Capture tab. An automatic
capture took them from three places at once, none of them visible in the GUI:

    frequency, duration   the plan TSV, per satellite   (soop_planner.py wrote it)
    bandwidth, samplerate the plan TSV, per satellite   — UNLESS --bw/--sr override
    gain, channels, bitmode, bias-tee, AGC, antenna info
                          hardcoded near the top of soop_capture.sh

So "what gain did that 3 a.m. capture run at?" had no answer anywhere on screen.
This module makes the answer explicit and single-sourced:

  * the Capture tab's radio fields are SAVED here and become the shared
    settings, so what you set for a manual capture is what an automatic one uses;
  * the three the script accepts as flags (--gain, --bw, --sr) are forwarded;
  * everything else is READ OUT OF soop_capture.sh ITSELF and displayed with the
    line it came from, because the GUI cannot change those without editing
    01_CODE — and it never edits 01_CODE.

WHY NOT JUST SEND EVERY PARAMETER
---------------------------------
soop_capture.sh:128-133 parses exactly --sr, --max-sec, --budget-gb, --gain,
--bw, --lead (plus session/target flags). There is no --channels, no --bitmode,
no --agc. Inventing them would mean patching the script, which breaks the
dual-mode guarantee. Showing them as fixed, with their source line, is honest;
showing them as editable would be a lie the user only discovers from a sidecar.

ORIGIN is carried on every value for that reason. A number with no provenance is
what caused the confusion in the first place.
"""

import json
import os
import re

from . import paths

# ── the shared radio parameters ───────────────────────────────────────────
# Keys match the Capture tab's form fields, which match jobs.CAPTURE_PROMPTS.
SHARED_KEYS = ("freq", "samplerate", "bandwidth", "channels", "bitmode",
               "biastee", "agc_rx1", "rx1_gain", "agc_rx2", "rx2_gain",
               "antenna_info")

# Of those, the ones soop_capture.sh can be TOLD about, and the flag it wants.
# Verified against the script's own argument parser by
# tests/test_capture_contract.py.
AUTO_FORWARDED = {
    "bandwidth": "--bw",
    "samplerate": "--sr",
    "rx1_gain": "--gain",        # --gain sets RX1_GAIN *and* RX2_GAIN together
}

PRESETS_SECTION = "radio"

# Settings baked into soop_capture.sh, with the shell variable that holds each.
SCRIPT_FIXED = (
    ("channels", "CHANNELS", "Channels", ""),
    ("bitmode", "BITMODE", "Bit mode", ""),
    ("biastee", "BIASTEE", "Bias-tee", ""),
    ("agc_rx1", "AGC_RX1", "AGC rx1", ""),
    ("agc_rx2", "AGC_RX2", "AGC rx2", ""),
    ("rx1_gain", "RX1_GAIN", "Gain rx1", "dB"),
    ("rx2_gain", "RX2_GAIN", "Gain rx2", "dB"),
    ("antenna_info", "ANTENNA_INFO", "Antenna info", ""),
    ("bandwidth", "FIXED_BW_MHZ", "Bandwidth", "MHz"),
    ("samplerate", "FIXED_SR_MHZ", "Sample rate", "MHz"),
)

_ASSIGN_RE = re.compile(r'^([A-Z][A-Z0-9_]*)=("([^"]*)"|[^\s#]*)', re.MULTILINE)

_cache = {"mtime": None, "values": {}, "lines": {}}


# ── what soop_capture.sh itself will do ───────────────────────────────────
def script_defaults(path=None):
    """{VAR: (value, line_number)} parsed from soop_capture.sh.

    Re-read whenever the script changes on disk, so editing the script updates
    the GUI's display without a restart. Parsed rather than duplicated for the
    same reason test_capture_contract.py re-derives the prompt order: a constant
    copied into the GUI is a second source of truth that drifts silently.
    """
    path = path or paths.SOOP_CAPTURE_SH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _cache["mtime"] == mtime:
        return _cache["values"]

    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f, start=1):
                # Only the assignments at column 0 — the ones in the settings
                # block. Indented ones live inside functions and the case arms,
                # where they are per-pass working state, not a default.
                if line.startswith((" ", "\t")):
                    continue
                m = _ASSIGN_RE.match(line)
                if m and m.group(1) not in values:
                    raw = m.group(3) if m.group(3) is not None else m.group(2)
                    values[m.group(1)] = (raw, number)
    except OSError:
        return {}

    _cache.update({"mtime": mtime, "values": values})
    return values


def script_value(var, fallback=""):
    entry = script_defaults().get(var)
    return entry[0] if entry else fallback


def script_origin(var):
    """'soop_capture.sh:76' — where a fixed value physically comes from."""
    entry = script_defaults().get(var)
    return f"soop_capture.sh:{entry[1]}" if entry else "soop_capture.sh"


# ── the shared store ──────────────────────────────────────────────────────
def load():
    """The radio settings the Capture tab last saved. {} when never touched."""
    try:
        with open(paths.PRESETS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    section = data.get(PRESETS_SECTION)
    if not isinstance(section, dict):
        return {}
    return {k: str(v) for k, v in section.items()
            if k in SHARED_KEYS and str(v).strip()}


def save(values):
    """Persist the shared radio settings. Blank fields are dropped, not stored
    as empty strings, because blank means 'let the tool decide' everywhere in
    this GUI and a stored '' would be indistinguishable from a chosen one."""
    paths.ensure_state_dirs()
    try:
        with open(paths.PRESETS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[PRESETS_SECTION] = {k: str(values.get(k, "") or "").strip()
                             for k in SHARED_KEYS
                             if str(values.get(k, "") or "").strip()}
    try:
        with open(paths.PRESETS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        return False
    return True


def is_configured():
    """Has the user actually set anything to share?"""
    return bool(load())


# ── what an automatic capture will use ────────────────────────────────────
def auto_overrides(shared=None):
    """{automation form key: value} for the flags soop_capture.sh accepts.

    Returns automation-panel keys ('bw', 'sr', 'gain'), not capture-panel ones,
    so the caller can drop it straight into the automation values dict and let
    jobs.autocapture_job build the argv. Only non-empty values appear, so an
    unconfigured Capture tab still produces a flagless invocation.
    """
    shared = load() if shared is None else shared
    out = {}
    if shared.get("bandwidth"):
        out["bw"] = shared["bandwidth"]
    if shared.get("samplerate"):
        out["sr"] = shared["samplerate"]
    # --gain is a single knob for both chains. Sending rx1's value when the two
    # differ would silently change rx2, so that case is reported, not guessed.
    gain = _shared_gain(shared)
    if gain:
        out["gain"] = gain
    return out


def _shared_gain(shared):
    rx1 = (shared.get("rx1_gain") or "").strip()
    rx2 = (shared.get("rx2_gain") or "").strip()
    if rx1 and rx2 and rx1 != rx2:
        return ""                # ambiguous — see gain_conflict()
    return rx1 or rx2


def gain_conflict(shared=None):
    """The rx1/rx2 gains the Capture tab holds, when they cannot both be sent.

    soop_capture.sh:131 makes --gain set BOTH chains. Two different manual gains
    therefore cannot be forwarded, and the honest outcome is to forward neither
    and say so — the reflected chain running at the direct chain's gain would
    corrupt the very ratio the experiment measures.
    """
    shared = load() if shared is None else shared
    rx1 = (shared.get("rx1_gain") or "").strip()
    rx2 = (shared.get("rx2_gain") or "").strip()
    if rx1 and rx2 and rx1 != rx2:
        return rx1, rx2
    return None


class Setting:
    """One row of the 'what will actually be used' table."""

    __slots__ = ("key", "label", "value", "unit", "origin", "kind")

    # kind drives the colour: shared = from your Capture tab, plan = per
    # satellite, script = fixed inside soop_capture.sh.
    def __init__(self, key, label, value, unit="", origin="", kind="script"):
        self.key = key
        self.label = label
        self.value = value
        self.unit = unit
        self.origin = origin
        self.kind = kind

    def display(self):
        return f"{self.value} {self.unit}".strip() if self.value else "—"

    def __repr__(self):                                    # pragma: no cover
        return f"<Setting {self.key}={self.value!r} from {self.origin}>"


GAIN_LABEL = "Gain rx1 / rx2"

# (shared key, label, unit, automation-tab key, shell variable)
_TUNABLE = (
    ("bandwidth", "Bandwidth", "MHz", "bw", "FIXED_BW_MHZ"),
    ("samplerate", "Sample rate", "MHz", "sr", "FIXED_SR_MHZ"),
)


def _tunable_row(key, label, unit, auto_key, script_var, shared, overrides):
    """Bandwidth / sample rate: automation tab > capture tab > script > plan."""
    if overrides.get(auto_key):
        return Setting(key, label, overrides[auto_key], unit,
                       "Automation tab", "shared")
    if shared.get(key):
        return Setting(key, label, shared[key], unit, "Capture tab", "shared")
    if script_value(script_var):
        return Setting(key, label, script_value(script_var), unit,
                       script_origin(script_var), "script")
    if key == "samplerate":
        # soop_capture.sh:258-261 — a blank FIXED_SR_MHZ mirrors whatever
        # bandwidth ended up applied, it does not fall back to the plan.
        return Setting(key, label, "matched to bandwidth", "",
                       "soop_capture.sh:258", "script")
    return Setting(key, label, "per satellite", unit, "plan TSV", "plan")


def _script_gain_pair():
    return (f"{script_value('RX1_GAIN', '30')} / "
            f"{script_value('RX2_GAIN', '30')}")


def _gain_row(shared, overrides, use_shared):
    gain = overrides.get("gain") or (_shared_gain(shared) if use_shared else "")
    if gain:
        origin = "Automation tab" if overrides.get("gain") else "Capture tab"
        return Setting("gain", GAIN_LABEL, f"{gain} / {gain}", "dB",
                       origin, "shared")

    conflict = gain_conflict(shared) if use_shared else None
    if conflict:
        return Setting(
            "gain", GAIN_LABEL, _script_gain_pair(), "dB",
            f"{script_origin('RX1_GAIN')} — your {conflict[0]}/{conflict[1]} "
            f"dB split cannot be sent (--gain sets both)", "conflict")
    return Setting("gain", GAIN_LABEL, _script_gain_pair(), "dB",
                   script_origin("RX1_GAIN"), "script")


def effective(shared=None, use_shared=True, overrides=None):
    """Every radio parameter an automatic capture will use, with its origin.

    `overrides` is the automation panel's own per-capture form ('bw', 'sr',
    'gain'), which wins over the shared settings — a flag typed on that tab is
    the most specific thing the user said.
    """
    shared = (load() if shared is None else shared) if use_shared else {}
    overrides = {k: str(v).strip() for k, v in (overrides or {}).items()
                 if str(v or "").strip()}

    rows = [Setting("freq", "Frequency", "per satellite", "MHz",
                    "plan TSV", "plan")]
    rows += [_tunable_row(*spec, shared, overrides) for spec in _TUNABLE]
    rows.append(_gain_row(shared, overrides, use_shared))
    rows += [Setting(key, label, script_value(var), unit, script_origin(var),
                     "script")
             for key, var, label, unit in SCRIPT_FIXED
             # bandwidth/samplerate/gain are placed above with their own
             # override precedence; the rest have none.
             if key not in ("bandwidth", "samplerate", "rx1_gain", "rx2_gain")]
    rows.append(Setting("amount", "Duration", "pass length, capped", "s",
                        f"plan TSV / {script_origin('MAX_CAPTURE_SEC')}", "plan"))
    return rows


def summary_line(shared=None, use_shared=True, overrides=None):
    """One-line version for a status strip: the parameters that vary."""
    rows = {s.key: s for s in effective(shared, use_shared, overrides)}
    bits = []
    for key, label in (("bandwidth", "bw"), ("samplerate", "sr"),
                       ("gain", "gain")):
        s = rows.get(key)
        if s is not None:
            bits.append(f"{label} {s.display()}")
    return "   ·   ".join(bits)
