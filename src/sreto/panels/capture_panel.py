"""
capture_panel.py — the manual capture front-end for capture.sh.

Every field is OPTIONAL. Each one shows capture.sh's own default in muted text
and sends an empty line when untouched, so pressing Start on a form nobody has
touched runs precisely the default capture the CLI would run. That is the
requirement, and it is also the safest behaviour: the GUI never invents a value
the script did not ask for.

The size/duration readout is capture.sh's own arithmetic (jobs.estimate_capture),
so the estimate shown before Start matches the one the script prints after it.

THIS TAB IS ALSO WHERE AUTOMATIC CAPTURES GET THEIR RADIO SETTINGS. The Radio
and Gain cards are saved to radio_settings as you edit them, and the Automation
tab forwards the three soop_capture.sh accepts (--bw, --sr, --gain). See
radio_settings.py for why only three, and what happens to the rest.
"""

import tkinter as tk
from tkinter import ttk

from .. import jobs, paths, radio_settings, theme, widgets

PRESETS = {
    "capture.sh defaults": {},
    "SoOp dual-channel (10 MS/s)": {
        "freq": "1621.25", "samplerate": "10", "bandwidth": "10",
        "channels": "1,2", "bitmode": "16bit",
        "antenna_info": "rx1: direct (RE), rx2: reflected (GR)",
        "agc_rx1": "off", "rx1_gain": "30", "agc_rx2": "off", "rx2_gain": "30",
        "capture_mode": "2", "amount": "120",
    },
    "Quick 10 s test": {
        "freq": "1621.25", "samplerate": "2", "bandwidth": "2",
        "channels": "1,2", "experiment_info": "sreto quick test",
        "capture_mode": "2", "amount": "10",
    },
    "L-band GNSS (1575.42)": {
        "freq": "1575.42", "samplerate": "4", "bandwidth": "4",
        "channels": "1,2",
        "antenna_info": "rx1: direct (RE), rx2: reflected (GR)",
        "rx1_gain": "30", "rx2_gain": "30",
        "capture_mode": "2", "amount": "60",
    },
}


class CapturePanel(ttk.Frame):

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._persist_job = None
        self._build()
        self._refresh_estimate()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        scroll = widgets.ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        root = scroll.inner

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Manual capture", style="Submenu.TLabel").pack(side="left")
        ttk.Label(header, text=f"→ bash {paths.rel(paths.CAPTURE_SH)}",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        ttk.Label(root, style="Muted.TLabel", wraplength=880, justify="left",
                  text="Leave a field blank to use capture.sh's own default "
                       "(shown in grey). An untouched form runs the default "
                       "capture exactly as the CLI would.").pack(
            anchor="w", pady=(0, 12))

        # ── presets ──
        preset_row = ttk.Frame(root)
        preset_row.pack(fill="x", pady=(0, 12))
        ttk.Label(preset_row, text="Preset", style="Muted.TLabel").pack(side="left")
        self.preset_var = tk.StringVar(value="capture.sh defaults")
        combo = ttk.Combobox(preset_row, textvariable=self.preset_var,
                             values=list(PRESETS), state="readonly", width=30)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", self._apply_preset)
        ttk.Button(preset_row, text="Clear all", command=self._clear,
                   width=10).pack(side="left")

        # ── radio settings ──
        radio = widgets.Card(root, "Radio",
                             "fed to capture.sh's prompts — and shared with the "
                             "Automation tab")
        radio.pack(fill="x", pady=(0, 10))
        self.form = widgets.Form(radio.body, columns=3, style="Panel.TFrame")
        self.form.pack(fill="x")
        f = self.form
        f.add("freq", "Frequency", placeholder="433", unit="MHz", width=12,
              help_text="Tuned centre frequency for both RX chains.")
        f.add("samplerate", "Sample rate", placeholder="2", unit="MHz", width=12,
              help_text="Complex sample rate per channel. 10 MS/s dual-channel "
                        "16-bit is ~0.8 GB per 10 s.")
        f.add("bandwidth", "Analog bandwidth", placeholder="1", unit="MHz", width=12,
              help_text="bladeRF analog filter bandwidth. Normally matched to "
                        "the sample rate.")
        f.add("channels", "Channels", placeholder="1,2", width=12,
              help_text="rx1 = direct/reference (RE), rx2 = ground-reflected "
                        "(GR). '1,2' captures both interleaved.")
        f.add("bitmode", "Bit mode", kind="choice", default="",
              choices=["", "16bit", "8bit"], width=10,
              help_text="Blank = capture.sh's default (16bit). 8bit halves the "
                        "file size and the dynamic range.")
        f.add("biastee", "Bias-tee", kind="choice", default="",
              choices=["", "off", "on"], width=10,
              help_text="DC power on the antenna port for an active LNA.")

        # ── gain ──
        gain = widgets.Card(root, "Gain", "the gain prompt is skipped when AGC is on")
        gain.pack(fill="x", pady=(0, 10))
        self.gain_form = widgets.Form(gain.body, columns=2, style="Panel.TFrame")
        self.gain_form.pack(fill="x")
        g = self.gain_form
        g.add("agc_rx1", "AGC rx1 (direct/RE)", kind="choice", default="",
              choices=["", "off", "on"], width=10,
              help_text="capture.sh only asks for a manual gain when AGC is "
                        "not literally 'on'.")
        g.add("rx1_gain", "Gain rx1", placeholder="20", unit="dB", width=10,
              help_text="Ignored by capture.sh when AGC rx1 is on.")
        g.add("agc_rx2", "AGC rx2 (reflected/GR)", kind="choice", default="",
              choices=["", "off", "on"], width=10)
        g.add("rx2_gain", "Gain rx2", placeholder="20", unit="dB", width=10,
              help_text="The reflected chain usually needs more gain than the "
                        "direct one.")

        # ── what the Radio and Gain cards above mean for an automatic session ──
        self.shared_label = ttk.Label(gain.body, style="PanelMuted.TLabel",
                                      wraplength=860, justify="left",
                                      font=theme.F.mono_small)
        self.shared_label.pack(anchor="w", pady=(10, 0))

        # ── amount ──
        amount = widgets.Card(root, "Acquisition length")
        amount.pack(fill="x", pady=(0, 10))
        mode_row = ttk.Frame(amount.body, style="Panel.TFrame")
        mode_row.pack(fill="x", pady=(0, 6))
        self.mode_var = tk.StringVar(value="2")
        ttk.Radiobutton(mode_row, text="Time of acquisition", value="2",
                        variable=self.mode_var, command=self._on_mode).pack(side="left")
        ttk.Radiobutton(mode_row, text="Number of samples", value="1",
                        variable=self.mode_var, command=self._on_mode).pack(
            side="left", padx=(18, 0))

        self.amount_form = widgets.Form(amount.body, columns=2, style="Panel.TFrame")
        self.amount_form.pack(fill="x")
        self.amount_field = self.amount_form.add(
            "amount", "Duration", placeholder="10", unit="s", width=12,
            help_text="Blank uses capture.sh's default of 10.")
        self.amount_unit_label = None

        self.estimate = ttk.Label(amount.body, style="PanelMuted.TLabel",
                                  font=theme.F.mono_small)
        self.estimate.pack(anchor="w", pady=(8, 0))

        # ── metadata ──
        meta = widgets.Card(root, "Experiment metadata",
                            "written into the .json sidecar and the master log")
        meta.pack(fill="x", pady=(0, 10))
        self.meta_form = widgets.Form(meta.body, columns=1, style="Panel.TFrame")
        self.meta_form.pack(fill="x")
        self.meta_form.add("antenna_info", "Antenna info", placeholder="none",
                           width=64,
                           help_text="e.g. 'rx1: direct (RE), rx2: reflected (GR)'")
        self.meta_form.add("experiment_info", "Experiment info", placeholder="none",
                           width=64,
                           help_text="Free text. soop_capture.sh writes a "
                                     "'SOOP_AUTO <sat> catnrNNNNN …' tag here, "
                                     "which MAIN.py can resolve the satellite from.")

        # ── actions ──
        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(6, 4))
        self.start_btn = ttk.Button(actions, text="Start capture",
                                    style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="left")
        ttk.Button(actions, text="Preview command",
                   command=self._preview).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open 02_DATA",
                   command=lambda: self.app.reveal(paths.DATA_DIR)).pack(side="left",
                                                                         padx=(8, 0))

        # Live estimate updates. The gain form is included because it feeds the
        # shared settings even though it does not change the file size.
        for form in (self.form, self.gain_form, self.amount_form):
            for field in form.fields.values():
                if hasattr(field.widget, "bind"):
                    field.widget.bind("<KeyRelease>", self._refresh_estimate, add="+")
                    field.widget.bind("<<ComboboxSelected>>", self._refresh_estimate,
                                      add="+")

        self._load_shared()

    # ── the settings shared with the Automation tab ───────────────────────
    def shared_values(self):
        """The radio parameters an automatic capture can inherit from here."""
        v = self.values()
        return {k: v.get(k, "") for k in radio_settings.SHARED_KEYS
                if str(v.get(k, "") or "").strip()}

    def _load_shared(self):
        """Restore the radio settings from the last session.

        Only the radio and gain cards: the acquisition length and the experiment
        text describe one capture, not the station, and restoring them would put
        a stale duration behind a form the user thinks is untouched.
        """
        saved = radio_settings.load()
        if not saved:
            self._refresh_shared_label()
            return
        self.form.set_values(saved)
        self.gain_form.set_values(saved)
        self.meta_form.set_values({k: v for k, v in saved.items()
                                   if k == "antenna_info"})
        self._refresh_estimate()

    def _schedule_persist(self, delay_ms=350):
        if self._persist_job is not None:
            self.after_cancel(self._persist_job)
        self._persist_job = self.after(delay_ms, self._persist_shared)

    def _persist_shared(self):
        self._persist_job = None
        radio_settings.save(self.shared_values())
        self._refresh_shared_label()
        # The other two tabs display these values, so they have to be told.
        for panel in (getattr(self.app, "automation_panel", None),
                      getattr(self.app, "availability_panel", None)):
            if panel is not None and hasattr(panel, "refresh_radio_settings"):
                panel.refresh_radio_settings()

    def _refresh_shared_label(self):
        shared = self.shared_values()
        conflict = radio_settings.gain_conflict(shared)
        forwarded = radio_settings.auto_overrides(shared)
        if not forwarded and not conflict:
            self.shared_label.configure(
                text="automatic captures: nothing set here yet, so they use "
                     "each satellite's bandwidth from the plan and "
                     "soop_capture.sh's own gain.",
                foreground=theme.MUTED)
            return

        bits = [f"--{k} {v}" for k, v in sorted(forwarded.items())]
        text = ("automatic captures inherit: " + "  ".join(bits)) if bits else ""
        color = theme.MUTED
        if conflict:
            text = ((text + "   ·   ") if text else "") + (
                f"rx1 {conflict[0]} dB / rx2 {conflict[1]} dB CANNOT be "
                f"inherited — soop_capture.sh's --gain sets both chains, so it "
                f"keeps its own {radio_settings.script_value('RX1_GAIN', '30')} dB")
            color = theme.AMBER
        self.shared_label.configure(text=text, foreground=color)

    # ── behaviour ─────────────────────────────────────────────────────────
    def values(self):
        v = {}
        v.update(self.form.values())
        v.update(self.gain_form.values())
        v.update(self.meta_form.values())
        v.update(self.amount_form.values())
        v["capture_mode"] = self.mode_var.get()
        return v

    def _on_mode(self):
        is_samples = self.mode_var.get() == "1"
        self.amount_field.widget.placeholder = "10"
        # Relabel in place: capture.sh asks a different question per mode.
        for child in self.amount_form.grid_slaves(row=0, column=0):
            child.configure(text="Samples per channel" if is_samples else "Duration")
        for child in self.amount_form.grid_slaves(row=0, column=2):
            child.configure(text="M" if is_samples else "s")
        self._refresh_estimate()

    def _apply_preset(self, _e=None):
        preset = PRESETS.get(self.preset_var.get(), {})
        self._clear(keep_preset=True)
        self.form.set_values(preset)
        self.gain_form.set_values(preset)
        self.meta_form.set_values(preset)
        self.amount_form.set_values(preset)
        if "capture_mode" in preset:
            self.mode_var.set(preset["capture_mode"])
            self._on_mode()
        self._refresh_estimate()

    def _clear(self, keep_preset=False):
        for form in (self.form, self.gain_form, self.meta_form, self.amount_form):
            for field in form.fields.values():
                field.set("")
        if not keep_preset:
            self.preset_var.set("capture.sh defaults")
        self._refresh_estimate()

    def _refresh_estimate(self, _e=None):
        # Every path that changes a radio field ends here, so this is the one
        # place the shared settings need to be written back from. Debounced:
        # this fires on every keystroke, and each save rewrites presets.json and
        # re-renders two other tabs.
        self._schedule_persist()
        size, duration, readable = jobs.estimate_capture(self.values())
        if size is None:
            self.estimate.configure(text=f"estimate unavailable — {readable}",
                                    foreground=theme.AMBER)
            return
        gb = size / 1e9
        color = theme.MUTED
        note = ""
        if gb > 20:
            color, note = theme.FAIL, "   ← very large, check free space"
        elif gb > 5:
            color, note = theme.AMBER, "   ← large"
        self.estimate.configure(
            text=f"estimated {readable}  ·  {duration:g} s on disk in "
                 f"{paths.rel(paths.DATA_DIR)}{note}",
            foreground=color)

    def _preview(self):
        values = self.values()
        job = jobs.capture_job(values)
        self.app.console.gui_banner("CAPTURE — PREVIEW (nothing has run)")
        self.app.console.gui_note(jobs.describe(job))
        self.app.console.gui_note("answers fed to capture.sh's prompts, in order:")
        for prompt, answer in zip(_prompt_labels(values), job.stdin_lines):
            shown = answer if answer else "<blank → script default>"
            self.app.console.gui_note(f"   {prompt:<28s} {shown}")
        size, duration, readable = jobs.estimate_capture(values)
        if size is not None:
            self.app.console.gui_note(f"   estimated size {readable} over {duration:g} s")

    def _start(self):
        values = self.values()
        size, _duration, _readable = jobs.estimate_capture(values)
        if size is None:
            self.app.error("Capture parameters",
                           "Some values are not numeric — check frequency, "
                           "sample rate and duration.")
            return
        self.app.run_job(jobs.capture_job(values))


def _prompt_labels(values):
    """Prompt names in the order capture.sh asks them, for this form's answers."""
    labels = ["frequency", "sample rate", "bandwidth", "channels", "antenna info",
              "experiment info", "bit mode", "bias-tee", "AGC rx1"]
    if (str(values.get("agc_rx1") or "off")) not in jobs.AGC_ON_VALUES:
        labels.append("gain rx1")
    labels.append("AGC rx2")
    if (str(values.get("agc_rx2") or "off")) not in jobs.AGC_ON_VALUES:
        labels.append("gain rx2")
    labels += ["capture mode", "duration / samples"]
    return labels
