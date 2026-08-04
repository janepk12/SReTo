"""
automation_panel.py — soop_capture.sh (hands-free sessions) and soop_planner.py.

Same rule as the manual capture panel: a blank field emits no flag at all, so an
untouched form runs `bash soop_capture.sh` with the script's own defaults
(24 h session, every target above 15°, 120 s per capture, 100 GB budget).

List and Dry-run are given equal billing with Start because they are the only
safe way to find out what a 24-hour session is about to do to the disk, and the
script already supports both.

WHERE THE RADIO SETTINGS COME FROM. Bandwidth, sample rate and gain are
inherited from the Capture tab (radio_settings.py) unless overridden in the
per-capture card below. The card at the top of this tab lists every value the
session will actually use and where each one came from, because "blank field"
previously meant three different things — plan value, script constant, or
mirrored bandwidth — and none of them were visible.
"""

import tkinter as tk
from tkinter import ttk

from .. import jobs, paths, radio_settings, soop_availability, theme, widgets


class AutomationPanel(ttk.Frame):

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._build()

    def _build(self):
        scroll = widgets.ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        root = scroll.inner

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="Automated SoOp capture", style="Submenu.TLabel").pack(
            side="left")
        ttk.Label(header, text=f"→ bash {paths.rel(paths.SOOP_CAPTURE_SH)}",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        ttk.Label(root, style="Muted.TLabel", wraplength=880, justify="left",
                  text="Waits for each planned pass, opens capture.sh at the "
                       "right moment, closes it when the window ends, writes a "
                       ".soop.json sidecar, then waits for the next one. Blank "
                       "fields keep the script's defaults.").pack(
            anchor="w", pady=(0, 12))

        # ── session ──
        session = widgets.Card(root, "Session")
        session.pack(fill="x", pady=(0, 10))

        mode_row = ttk.Frame(session.body, style="Panel.TFrame")
        mode_row.pack(fill="x", pady=(0, 6))
        self.session_mode = tk.StringVar(value="for")
        ttk.Radiobutton(mode_row, text="Run for a duration", value="for",
                        variable=self.session_mode).pack(side="left")
        ttk.Radiobutton(mode_row, text="Run until a UTC time", value="until",
                        variable=self.session_mode).pack(side="left", padx=(18, 0))

        self.session_form = widgets.Form(session.body, columns=3, style="Panel.TFrame")
        self.session_form.pack(fill="x")
        s = self.session_form
        s.add("for", "Session length", placeholder="24h", width=12,
              help_text="45m, 6h, 1.5h, 2d, or plain seconds.")
        s.add("until", "Until (UTC)", placeholder="HH:MM", width=14,
              help_text="'22:00' (today, or tomorrow if already past) or "
                        "'YYYY-MM-DD HH:MM'.")
        s.add("count", "Max captures", placeholder="unlimited", width=12,
              help_text="Stop after N captures.")

        opts = ttk.Frame(session.body, style="Panel.TFrame")
        opts.pack(fill="x", pady=(6, 0))
        self.next_only = tk.BooleanVar(value=False)
        self.include_geo = tk.BooleanVar(value=False)
        self.refresh = tk.BooleanVar(value=False)
        self.no_refresh = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Next pass only (--next)", variable=self.next_only,
                        style="Panel.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opts, text="Include GEO targets", variable=self.include_geo,
                        style="Panel.TCheckbutton").pack(side="left", padx=(18, 0))
        ttk.Checkbutton(opts, text="Force re-plan first", variable=self.refresh,
                        style="Panel.TCheckbutton").pack(side="left", padx=(18, 0))
        ttk.Checkbutton(opts, text="Never re-plan", variable=self.no_refresh,
                        style="Panel.TCheckbutton").pack(side="left", padx=(18, 0))

        # ── targets ──
        targets = widgets.Card(root, "Target selection")
        targets.pack(fill="x", pady=(0, 10))
        self.target_form = widgets.Form(targets.body, columns=3, style="Panel.TFrame")
        self.target_form.pack(fill="x")
        t = self.target_form
        t.add("sat", "Satellite filter", placeholder="all", width=18,
              help_text="Case-insensitive substring of the name or CATNR: "
                        "IRIDIUM, GLOBALSTAR, 43250 …")
        t.add("min_elev", "Min peak elevation", placeholder="15", unit="deg", width=10,
              help_text="Passes whose peak stays below this are skipped.")
        t.add("lead", "Start lead", placeholder="10", unit="s", width=10,
              help_text="Begin the capture this many seconds before the pass.")

        # ── radio settings actually in force ──
        radio = widgets.Card(root, "Radio settings in force",
                             "what every capture in this session will use")
        radio.pack(fill="x", pady=(0, 10))

        inherit_row = ttk.Frame(radio.body, style="Panel.TFrame")
        inherit_row.pack(fill="x", pady=(0, 8))
        self.inherit = tk.BooleanVar(value=True)
        ttk.Checkbutton(inherit_row, text="Inherit bandwidth / sample rate / "
                                          "gain from the Capture tab",
                        variable=self.inherit, style="Panel.TCheckbutton",
                        command=self.refresh_radio_settings).pack(side="left")
        ttk.Button(inherit_row, text="Edit on the Capture tab", width=22,
                   command=self._goto_capture_tab).pack(side="right")

        self.radio_table = widgets.SettingsTable(radio.body)
        self.radio_table.pack(fill="x")

        # ── per-capture ──
        cap = widgets.Card(root, "Per-capture overrides",
                           "leave blank to use the inherited values above")
        cap.pack(fill="x", pady=(0, 10))
        self.capture_form = widgets.Form(cap.body, columns=3, style="Panel.TFrame")
        self.capture_form.pack(fill="x")
        c = self.capture_form
        c.add("max_sec", "Max per capture", placeholder="120", unit="s", width=10,
              help_text="Centred on the pass peak when the pass is longer. "
                        "10 MS/s dual-channel is ~0.8 GB per 10 s.")
        c.add("budget_gb", "Data budget", placeholder="100", unit="GB", width=10,
              help_text="Session stops after roughly this much data. 0 = unlimited.")
        c.add("gain", "RX gain", placeholder="from Capture tab", unit="dB",
              width=14,
              help_text="soop_capture.sh's --gain applies ONE value to both RX1 "
                        "and RX2. Blank inherits the Capture tab's gain when "
                        "both chains are set to the same number.")
        c.add("bw", "Force bandwidth", placeholder="from Capture tab", unit="MHz",
              width=14,
              help_text="Blank inherits the Capture tab's bandwidth, or each "
                        "satellite's value from the plan when that is empty too.")
        c.add("sr", "Force sample rate", placeholder="from Capture tab",
              unit="MHz", width=14,
              help_text="Blank inherits the Capture tab's sample rate, or "
                        "mirrors the applied bandwidth when that is empty too.")
        c.add("plan", "Plan file", placeholder="latest_capture_plan.tsv", width=22,
              help_text="Alternative plan TSV.")

        for field in ("gain", "bw", "sr"):
            widget = self.capture_form.fields[field].widget
            widget.bind("<KeyRelease>",
                        lambda _e: self.refresh_radio_settings(), add="+")

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(4, 14))
        ttk.Button(actions, text="Start session", style="Accent.TButton",
                   command=lambda: self._start("run")).pack(side="left")
        ttk.Button(actions, text="List queue",
                   command=lambda: self._start("list")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Dry run",
                   command=lambda: self._start("dry-run")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Preview command",
                   command=self._preview).pack(side="left", padx=(8, 0))

        ttk.Separator(root, orient="horizontal").pack(fill="x", pady=(0, 14))

        # ── planner ──
        planner = widgets.Card(root, "Pass planner",
                               f"→ python {paths.rel(paths.SOOP_PLANNER_PY)}")
        planner.pack(fill="x")
        ttk.Label(planner.body, style="PanelMuted.TLabel", wraplength=860,
                  justify="left",
                  text="Fetches the latest TLEs from CelesTrak and writes the "
                       "capture plan, skymaps and CSV into "
                       f"{paths.rel(paths.SOOP_DIR)}. soop_capture.sh runs this "
                       "itself when its plan goes stale.").pack(anchor="w",
                                                                pady=(0, 8))
        self.planner_form = widgets.Form(planner.body, columns=3, style="Panel.TFrame")
        self.planner_form.pack(fill="x")
        p = self.planner_form
        p.add("mode", "Mode", kind="choice", default="",
              choices=["", "now_plus_24h", "future", "past"], width=14,
              help_text="Blank uses the MODE constant inside soop_planner.py.")
        p.add("hours", "Hours ahead", placeholder="from script", width=10,
              help_text="Overrides CUSTOM_HOURS_AHEAD in now_plus_24h mode.")

        self.plan_status = ttk.Label(planner.body, style="PanelMuted.TLabel",
                                     font=theme.F.mono_small)
        self.plan_status.pack(anchor="w", pady=(8, 6))

        prow = ttk.Frame(planner.body, style="Panel.TFrame")
        prow.pack(fill="x")
        ttk.Button(prow, text="Run planner", style="Accent.TButton",
                   command=self._run_planner).pack(side="left")
        ttk.Button(prow, text="Planner self-test",
                   command=lambda: self._run_planner(selftest=True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(prow, text="Open SOOP folder",
                   command=lambda: self.app.reveal(paths.SOOP_DIR)).pack(
            side="left", padx=(8, 0))

        self.refresh_status()
        self.refresh_radio_settings()

    # ── behaviour ─────────────────────────────────────────────────────────
    def refresh_status(self):
        provider = soop_availability.get_provider("plan")
        ok, reason = provider.available()
        if not ok:
            self.plan_status.configure(text=reason, foreground=theme.AMBER)
            return
        age = provider.age_hours() or 0.0
        n = len(provider.passes(horizon_h=24 * 7, min_elev_deg=0))
        color = theme.OK if age <= 6 else theme.AMBER
        note = "" if age <= 6 else "  (soop_capture.sh will re-plan on start)"
        self.plan_status.configure(
            text=f"plan: {n} pass(es), written {age:.1f} h ago{note}",
            foreground=color)

    # ── radio settings ────────────────────────────────────────────────────
    def refresh_radio_settings(self):
        """Re-render the 'in force' table. Called when either tab changes."""
        rows = radio_settings.effective(
            use_shared=self.inherit.get(),
            overrides=self._explicit_radio_overrides())
        self.radio_table.set_rows(rows)

    def _explicit_radio_overrides(self):
        """Only what this tab's own fields say — blank means 'inherit'."""
        values = self.capture_form.values()
        return {k: values.get(k, "") for k in ("bw", "sr", "gain")
                if str(values.get(k, "") or "").strip()}

    def _goto_capture_tab(self):
        self.app.notebook.select(self.app.capture_panel)

    def _values(self):
        v = {}
        v.update(self.session_form.values())
        v.update(self.target_form.values())
        v.update(self.capture_form.values())
        v["session_mode"] = self.session_mode.get()
        v["next_only"] = self.next_only.get()
        v["include_geo"] = self.include_geo.get()
        v["refresh"] = self.refresh.get()
        v["no_refresh"] = self.no_refresh.get()

        # Inherit from the Capture tab for anything this tab left blank. Done
        # HERE rather than inside jobs.autocapture_job so that an untouched form
        # with nothing shared still produces `bash soop_capture.sh` with no
        # flags at all — the contract tests/test_capture_contract.py enforces.
        if self.inherit.get():
            for key, value in radio_settings.auto_overrides().items():
                if not str(v.get(key, "") or "").strip():
                    v[key] = value
        return v

    def _start(self, mode):
        values = self._values()
        values["run_mode"] = mode
        if values["refresh"] and values["no_refresh"]:
            self.app.error("Conflicting options",
                           "'Force re-plan' and 'Never re-plan' cannot both be "
                           "set — soop_capture.sh would take whichever came last.")
            return
        self.app.run_job(jobs.autocapture_job(values))

    def _preview(self):
        values = self._values()
        values["run_mode"] = "run"
        job = jobs.autocapture_job(values)
        self.app.console.gui_banner("SOOP SESSION — PREVIEW (nothing has run)")
        self.app.console.gui_note(jobs.describe(job))
        self.app.console.gui_note("radio settings every capture will use:")
        for s in radio_settings.effective(
                use_shared=self.inherit.get(),
                overrides=self._explicit_radio_overrides()):
            self.app.console.gui_note(
                f"   {s.label:<16s} {s.display():<24s} ← {s.origin}")

    def _run_planner(self, selftest=False):
        values = self.planner_form.values()
        values["selftest"] = selftest
        values["nice"] = 10        # never let a TLE crawl fight the foreground
        self.app.run_job(jobs.planner_job(values),
                         on_finish=lambda _h: self.refresh_status())
