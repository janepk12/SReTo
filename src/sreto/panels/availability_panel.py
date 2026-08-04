"""
availability_panel.py — what is overhead, when, and whether you can SEE it.

Two layers:

  * WHAT IS UP — a provider supplies passes. The panel does no orbital
    mechanics of its own; whatever providers are registered appear in the
    dropdown, so injecting a target-selection algorithm later is an import away
    (see soop_availability's module docstring).

  * WHAT YOU CAN OBSERVE — the sky-view mask. The planner masks on ELEVATION
    only, so its plan lists passes that go behind your roof. Declaring which
    compass sectors you actually have a view of turns "78° pass" into "78° pass,
    of which you can see 12% and never above 15°", which is the number that
    decides whether the capture is worth the disk.

Mask evaluation propagates TLEs, so it runs on a worker thread and streams its
progress. Tracks are cached per pass, independent of the mask, so changing the
mask after the first pass over the data is instant.

When the placeholder provider is active the panel says so in amber,
permanently. A schedule that looks authoritative but is synthetic is the one
failure mode worth designing against.

  * WHAT IT WILL RECORD WITH — the radio settings card. An automatic capture
    started from this tab inherits the Capture tab's bandwidth/sample rate/gain
    and takes everything else from soop_capture.sh; the card names every value
    and where it came from, so the table's "Est GB" column and the session that
    follows it cannot be based on different numbers.

  * THE SKYMAP — the same passes, drawn where they actually are, coloured by
    constellation. It reads the az/el tracks the mask evaluation already cached,
    so it costs no propagation of its own.
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .. import (
    history,
    jobs,
    paths,
    prechecks,
    radio_settings,
    skymap,
    skyview,
    theme,
    widgets,
)
from .. import soop_availability as sa

REFRESH_MS = 15_000       # countdown resolution; nothing here is expensive
SKYMAP_MS = 5_000         # the dots move slowly; 5 s is smoother than needed


class AvailabilityPanel(ttk.Frame):

    COLUMNS = [
        ("when", "When", 118, "w"),
        ("rise", "Rise (Berlin)", 125, "w"),
        ("name", "Satellite", 190, "w"),
        ("catnr", "CATNR", 62, "w"),
        ("elev", "Peak el", 62, "e"),
        ("vis", "In view", 62, "e"),
        ("viselev", "Best vis el", 78, "e"),
        ("viswin", "Visible window", 150, "w"),
        ("dur", "Duration", 72, "e"),
        ("freq", "Freq MHz", 80, "e"),
        ("gb", "Est GB", 62, "e"),
    ]

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._passes = []
        self._visibility = {}
        self._after = None
        self._eval_thread = None
        self._eval_queue = queue.Queue()
        self._eval_stop = False
        self._fill_thread = None
        self._engine = skyview.VisibilityEngine()
        self._engine.load_cache()      # so the skymap can draw on first paint
        self._build()
        self._load_mask()
        self.refresh_radio_settings()
        self.refresh()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="SoOp availability", style="Submenu.TLabel").pack(side="left")
        self.chip = widgets.StatusChip(header, "—")
        self.chip.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(10, 0))

        # ── left: the sky-view mask ──
        self.sky_card = widgets.Card(body, "Sky view", "click a wedge to toggle")
        self.sky_card.pack(side="left", fill="y", padx=(0, 12))

        self.compass = widgets.SkyCompass(self.sky_card.body, size=168,
                                          on_change=self._on_sectors_changed)
        self.compass.pack(pady=(0, 8))

        self.preset_var = tk.StringVar(value="Full sky")
        preset = ttk.Combobox(self.sky_card.body, textvariable=self.preset_var,
                              values=list(skyview.PRESETS), state="readonly",
                              width=22)
        preset.pack(fill="x")
        preset.bind("<<ComboboxSelected>>", self._on_preset)

        floor_row = ttk.Frame(self.sky_card.body, style="Panel.TFrame")
        floor_row.pack(fill="x", pady=(8, 0))
        ttk.Label(floor_row, text="Horizon floor", style="PanelMuted.TLabel").pack(
            side="left")
        self.elev_var = tk.StringVar(value="10")
        elev = ttk.Combobox(floor_row, textvariable=self.elev_var, width=5,
                            state="readonly",
                            values=["0", "5", "10", "15", "20", "30", "40", "60"])
        elev.pack(side="left", padx=(6, 2))
        elev.bind("<<ComboboxSelected>>", lambda _e: self._on_mask_changed())
        ttk.Label(floor_row, text="deg", style="PanelMuted.TLabel").pack(side="left")

        self.mask_label = ttk.Label(self.sky_card.body, style="PanelMuted.TLabel",
                                    wraplength=190, justify="left")
        self.mask_label.pack(anchor="w", pady=(8, 0))

        self.rx_label = ttk.Label(self.sky_card.body, style="PanelMuted.TLabel",
                                  wraplength=190, justify="left",
                                  font=theme.F.mono_small)
        self.rx_label.pack(anchor="w", pady=(6, 0))

        self.eval_progress = ttk.Progressbar(self.sky_card.body, mode="determinate",
                                             length=190)

        ttk.Button(self.sky_card.body, text="Reset to full sky",
                   command=lambda: self._apply_preset("Full sky")).pack(
            fill="x", pady=(10, 0))

        # ── middle: the skymap ──
        self.map_card = widgets.Card(body, "Sky now", "click a satellite")
        self.map_card.pack(side="left", fill="y", padx=(0, 12))

        self.skymap = skymap.SkyMap(self.map_card.body, size=236,
                                    on_select=self._on_skymap_select)
        self.skymap.pack()

        time_row = ttk.Frame(self.map_card.body, style="Panel.TFrame")
        time_row.pack(fill="x", pady=(6, 0))
        ttk.Label(time_row, text="At", style="PanelMuted.TLabel").pack(side="left")
        self.map_offset = tk.DoubleVar(value=0.0)
        # A scrubber rather than a clock: "what will be up when I get home" is
        # the question this view is actually asked, and it is one drag away.
        scrub = ttk.Scale(time_row, from_=0.0, to=6.0, orient="horizontal",
                          variable=self.map_offset,
                          command=lambda _v: self._refresh_skymap())
        scrub.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.map_time_label = ttk.Label(self.map_card.body,
                                        style="PanelMuted.TLabel",
                                        font=theme.F.mono_small)
        self.map_time_label.pack(anchor="w")

        self.legend = skymap.Legend(self.map_card.body)
        self.legend.pack(anchor="w", pady=(6, 0))

        self.map_note = ttk.Label(self.map_card.body, style="PanelMuted.TLabel",
                                  wraplength=236, justify="left")
        self.map_note.pack(anchor="w", pady=(4, 0))

        # ── right: settings + controls + table ──
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self.radio_card = widgets.Card(
            right, "Capture settings in force",
            "what a capture started here will record with")
        self.radio_card.pack(fill="x", pady=(0, 8))
        # Two columns, not three: each row is label + value + origin, and the
        # origin strings ('soop_capture.sh:76') are long enough that a third
        # column pushes the last one off the right edge of the card.
        self.radio_table = widgets.SettingsTable(self.radio_card.body, columns=2)
        self.radio_table.pack(fill="x")
        self.radio_note = ttk.Label(self.radio_card.body,
                                    style="PanelMuted.TLabel", wraplength=820,
                                    justify="left")
        self.radio_note.pack(anchor="w", pady=(6, 0))

        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(0, 8))

        ttk.Label(controls, text="Source", style="Muted.TLabel").pack(side="left")
        self.provider_var = tk.StringVar()
        self.provider_combo = ttk.Combobox(controls, textvariable=self.provider_var,
                                           state="readonly", width=34)
        self.provider_combo.pack(side="left", padx=(6, 14))
        self.provider_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(controls, text="Horizon", style="Muted.TLabel").pack(side="left")
        self.horizon_var = tk.StringVar(value="24")
        h = ttk.Combobox(controls, textvariable=self.horizon_var, width=5,
                         state="readonly", values=["1", "3", "6", "12", "24", "48", "168"])
        h.pack(side="left", padx=(6, 2))
        h.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(controls, text="h", style="Muted.TLabel").pack(side="left", padx=(0, 14))

        self.hide_past = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Hide past", variable=self.hide_past,
                        command=self._refill).pack(side="left")

        self.hide_blocked = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Hide out-of-view",
                        variable=self.hide_blocked,
                        command=self._refill).pack(side="left", padx=(12, 0))

        ttk.Button(controls, text="Refresh", command=self.refresh,
                   width=9).pack(side="right")

        self.notice = ttk.Label(right, style="Muted.TLabel", wraplength=820,
                                justify="left")
        self.notice.pack(fill="x", pady=(0, 6))

        self.summary = ttk.Label(right, style="Mono.TLabel", wraplength=820,
                                 justify="left",
                                 font=theme.F.mono_small)
        self.summary.pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(right)
        table_frame.pack(fill="both", expand=True)
        # 'tree headings' keeps the #0 column, which carries the constellation
        # swatch. Row tags already encode pass state as a foreground colour, and
        # a second meaning on the same channel would be unreadable — so the
        # constellation gets its own column instead.
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in self.COLUMNS],
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=26, minwidth=26, stretch=False, anchor="center")
        self._swatches = {}
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        # Eleven columns do not fit a laptop window, and a Treeview silently
        # clips instead of scrolling unless it is given an x-scrollbar.
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("now", foreground=theme.OK,
                                font=theme.F.mono_small_bold)
        self.tree.tag_configure("soon", foreground=theme.C1)
        self.tree.tag_configure("upcoming", foreground=theme.TEXT)
        self.tree.tag_configure("past", foreground=theme.MUTED)
        self.tree.tag_configure("geo", foreground=theme.C3)
        self.tree.tag_configure("placeholder", foreground=theme.AMBER)
        self.tree.tag_configure("blocked", foreground=theme.MUTED)
        self.tree.tag_configure("partial", foreground=theme.AMBER)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Capture this pass",
                   command=self._capture_selected).pack(side="left")
        ttk.Button(actions, text="Re-plan (soop_planner.py)",
                   command=self._replan).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open SOOP folder",
                   command=lambda: self.app.reveal(paths.SOOP_DIR)).pack(
            side="left", padx=(8, 0))
        self.detail = ttk.Label(right, style="Muted.TLabel", wraplength=820,
                                justify="left",
                                font=theme.F.mono_small)
        self.detail.pack(fill="x", pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        self._reload_providers()

    # ── mask ──────────────────────────────────────────────────────────────
    def mask(self):
        try:
            floor = float(self.elev_var.get())
        except ValueError:
            floor = 10.0
        return skyview.HorizonMask(self.compass.open_sectors, min_elev_deg=floor)

    def _load_mask(self):
        """Restore the last-used sky view — it describes the physical site."""
        import json
        try:
            with open(paths.PRESETS_JSON, encoding="utf-8") as f:
                saved = json.load(f).get("sky_mask")
        except (OSError, ValueError):
            saved = None
        if saved:
            m = skyview.HorizonMask.from_dict(saved)
            self.compass.set_sectors(m.open_sectors)
            self.elev_var.set(f"{m.min_elev_deg:g}")
        self._update_mask_labels()

    def _save_mask(self):
        import json
        paths.ensure_state_dirs()
        try:
            with open(paths.PRESETS_JSON, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data["sky_mask"] = self.mask().to_dict()
        try:
            with open(paths.PRESETS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _on_preset(self, _e=None):
        self._apply_preset(self.preset_var.get())

    def _apply_preset(self, name):
        sectors = skyview.PRESETS.get(name)
        if sectors is None:
            return
        self.preset_var.set(name)
        self.compass.set_sectors(sectors)
        self._on_mask_changed()

    def _on_sectors_changed(self, sectors):
        match = next((label for label, s in skyview.PRESETS.items() if s == sectors),
                     "custom")
        self.preset_var.set(match if match != "custom" else "")
        self._on_mask_changed()

    def _on_mask_changed(self):
        self._update_mask_labels()
        self._save_mask()
        self._start_evaluation()

    def _update_mask_labels(self):
        m = self.mask()
        color = theme.FAIL if m.is_blind() else (
            theme.MUTED if m.is_full_sky() else theme.C1)
        self.mask_label.configure(text=m.describe(), foreground=color)

        rx = self._engine.receiver
        if rx:
            self.rx_label.configure(
                text=f"rx {rx[0]:.4f}, {rx[1]:.4f}\n{rx[2]:.0f} m ({rx[3]})",
                foreground=theme.MUTED)
        else:
            self.rx_label.configure(
                text="no receiver coordinates — the mask cannot be evaluated",
                foreground=theme.AMBER)

    # ── data ──────────────────────────────────────────────────────────────
    def _reload_providers(self):
        self._providers = {p.label: p for p in sa.providers()}
        self.provider_combo.configure(values=list(self._providers))
        if not self.provider_var.get() or self.provider_var.get() not in self._providers:
            default = sa.default_provider()
            self.provider_var.set(default.label if default else "")

    def _provider(self):
        return self._providers.get(self.provider_var.get()) or sa.default_provider()

    def refresh(self):
        self._reload_providers()
        provider = self._provider()
        if provider is None:
            self.notice.configure(text="no availability provider registered",
                                  foreground=theme.FAIL)
            return

        ok, reason = provider.available()
        try:
            horizon = float(self.horizon_var.get())
        except ValueError:
            horizon = 24.0

        # The horizon FLOOR is the mask's, so the provider is asked for
        # everything above it and the sector test is applied here.
        floor = self.mask().min_elev_deg
        self._passes = (provider.passes(horizon_h=horizon, min_elev_deg=floor)
                        if ok else [])

        if provider.is_placeholder:
            self.notice.configure(
                text="PLACEHOLDER DATA — these passes are synthetic, not "
                     "propagated. Register your own provider "
                     "(sreto/soop_availability.py) to replace them.",
                foreground=theme.AMBER)
        elif not ok:
            self.notice.configure(text=reason, foreground=theme.AMBER)
        else:
            self._describe_plan(provider)

        self._start_evaluation()
        self._schedule()

    def _describe_plan(self, provider):
        """Plan age matters more than plan contents — say it plainly."""
        age = provider.age_hours()
        header = provider.header_lines()
        window = next((h for h in header if h.startswith("window:")), "")

        if age is None:
            self.notice.configure(text=window, foreground=theme.MUTED)
        elif age > prechecks.PLAN_MAX_AGE_H:
            self.notice.configure(
                text=f"STALE PLAN — written {age:.1f} h ago (soop_capture.sh "
                     f"re-plans past {prechecks.PLAN_MAX_AGE_H:.0f} h). Passes "
                     f"below may all be in the past; press 'Re-plan'."
                     + (f"    {window}" if window else ""),
                foreground=theme.AMBER)
        else:
            self.notice.configure(
                text=f"plan written {age:.1f} h ago"
                     + (f"    ·    {window}" if window else ""),
                foreground=theme.MUTED)

    # ── mask evaluation (worker thread) ───────────────────────────────────
    def _start_evaluation(self):
        """Evaluate the mask off the Tk thread; stream results back."""
        self._eval_stop = True          # ask any in-flight run to give up
        mask = self.mask()

        if mask.is_full_sky() or not self._passes or not self._engine.receiver:
            self._visibility = {}
            self._refill()
            return

        # Only what the user could actually act on — never the whole 578-row
        # plan. Past passes cannot be captured, so they are not worth a TLE
        # propagation each.
        now = time.time()
        todo = [p for p in self._passes if p.state(now) != "past"]
        if not todo:
            self._visibility = {}
            self._refill()
            return

        self._refill()                  # show the table immediately, unfiltered
        self._eval_stop = False
        self.eval_progress.pack(fill="x", pady=(8, 0))
        self.eval_progress.configure(value=0, maximum=len(todo))

        def worker():
            def progress(done, total):
                self._eval_queue.put(("progress", done, total))
            try:
                result = self._engine.evaluate_many(
                    todo, mask, on_progress=progress,
                    should_stop=lambda: self._eval_stop)
                self._eval_queue.put(("done", result, None))
            except Exception as e:                      # noqa: BLE001
                self._eval_queue.put(("error", str(e), None))

        self._engine.load_cache()
        self._eval_thread = threading.Thread(target=worker, daemon=True,
                                             name="skyview")
        self._eval_thread.start()
        self.after(60, self._pump_evaluation)

    def _pump_evaluation(self):
        pending = True
        while True:
            try:
                kind, a, _b = self._eval_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.eval_progress.configure(value=a)
            elif kind == "done":
                self._visibility = a
                pending = False
                self.eval_progress.pack_forget()
                self._refill()
            elif kind == "error":
                pending = False
                self.eval_progress.pack_forget()
                self.notice.configure(
                    text=f"sky-view evaluation failed: {a}", foreground=theme.FAIL)
        if pending and not self._eval_stop:
            self.after(60, self._pump_evaluation)

    def _visibility_for(self, p):
        return self._visibility.get(skyview._pass_key(p))

    # ── the skymap ────────────────────────────────────────────────────────
    def _map_time(self):
        return time.time() + float(self.map_offset.get()) * 3600.0

    def _passes_up_at(self, when):
        return [p for p in self._passes
                if p.geo or (p.rise_unix <= when <= p.set_unix)]

    def _ensure_tracks(self, when):
        """Propagate the few passes that are up at `when` but not yet cached.

        Without this the map is empty until a mask evaluation happens to have
        covered the right passes — the on-disk cache holds whatever the last
        evaluation left, which is rarely what is overhead now. Bounded to a
        dozen, one worker at a time, and it feeds the same cache the mask uses.
        """
        if self._fill_thread is not None and self._fill_thread.is_alive():
            return
        if not self._engine.receiver:
            return
        missing = [p for p in self._passes_up_at(when)
                   if self._engine.cached_track(p) is None]
        if not missing:
            return

        def worker():
            filled = self._engine.prefetch(missing, limit=12)
            if filled:
                self.app.post_to_ui(self._refresh_skymap)

        self._fill_thread = threading.Thread(target=worker, daemon=True,
                                             name="skymap-fill")
        self._fill_thread.start()

    def _refresh_skymap(self):
        when = self._map_time()
        self._ensure_tracks(when)
        positions = skymap.positions_at(self._passes, self._engine, when,
                                        mask=self.mask())
        self.skymap.set_positions(positions, mask=self.mask())
        self.legend.set_constellations(
            sorted({s.constellation for s in positions}))

        offset = float(self.map_offset.get())
        when_label = history.fmt_berlin(when, "%H:%M:%S")
        self.map_time_label.configure(
            text=f"{when_label} Berlin" + (f"   (now +{offset:.1f} h)"
                                           if offset >= 0.05 else "   (now)"))

        if positions:
            self.map_note.configure(text=f"{len(positions)} above the horizon",
                                    foreground=theme.MUTED)
        elif not self._engine.receiver:
            self.map_note.configure(
                text="no receiver coordinates — nothing can be placed",
                foreground=theme.AMBER)
        elif self._passes_up_at(when):
            # Something IS up; its track is still being propagated by the
            # worker _ensure_tracks started, which will redraw when it lands.
            self.map_note.configure(text="propagating tracks…",
                                    foreground=theme.MUTED)
        else:
            self.map_note.configure(text="nothing above the horizon",
                                    foreground=theme.MUTED)

    def _on_skymap_select(self, sat):
        """Select the clicked satellite's row in the table."""
        rows = getattr(self, "_visible_rows", [])
        for index, p in enumerate(rows):
            if p is sat.pass_obj:
                item = self.tree.get_children()[index]
                self.tree.selection_set(item)
                self.tree.see(item)
                self._show_detail()
                return
        self.detail.configure(
            text=f"{sat.name} — {sat.el_deg:.1f}° at "
                 f"{skyview.great_circle_label(sat.az_deg)} "
                 f"(not in the table under the current filters)")

    # ── the radio settings shown here ─────────────────────────────────────
    def refresh_radio_settings(self):
        """Mirror what the Automation tab will actually send. Called by the
        Capture tab whenever its radio fields change."""
        rows = radio_settings.effective()
        self.radio_table.set_rows(rows)
        conflict = radio_settings.gain_conflict()
        if conflict:
            self.radio_note.configure(
                text=f"the Capture tab's split gain (rx1 {conflict[0]} dB / rx2 "
                     f"{conflict[1]} dB) cannot be forwarded — soop_capture.sh's "
                     f"--gain sets both chains at once, so it keeps its own "
                     f"value.",
                foreground=theme.AMBER)
        elif radio_settings.is_configured():
            self.radio_note.configure(
                text="blue = from the Capture tab · purple = per satellite from "
                     "the plan · grey = fixed inside soop_capture.sh",
                foreground=theme.MUTED)
        else:
            self.radio_note.configure(
                text="nothing set on the Capture tab yet, so every value below "
                     "comes from the plan or from soop_capture.sh itself.",
                foreground=theme.MUTED)

    # ── table ─────────────────────────────────────────────────────────────
    def _refill(self):
        now = time.time()
        self.tree.delete(*self.tree.get_children())
        mask = self.mask()

        visible = self._passes
        if self.hide_past.get():
            visible = [p for p in visible if p.state(now) != "past"]

        blocked = 0
        if self._visibility and self.hide_blocked.get() and not mask.is_full_sky():
            kept = []
            for p in visible:
                v = self._visibility_for(p)
                if v is not None and not v.visible and not v.error:
                    blocked += 1
                else:
                    kept.append(p)
            visible = kept

        self._visible_rows = visible
        if not visible:
            self._show_empty_state(now, blocked)

        for p in visible:
            self.tree.insert("", "end", values=self._row_values(p, now),
                             tags=self._row_tags(p, now),
                             image=self._swatch_for(p))

        self._update_summary(now, blocked, mask)
        self._refresh_skymap()

    def _swatch_for(self, p):
        """A small solid-colour PhotoImage for this pass's constellation.

        Cached per constellation, not per row: a 578-row plan would otherwise
        build 578 identical images and keep them all alive for the Treeview.
        """
        key = theme.constellation_of(p.name, p.geo)
        swatch = self._swatches.get(key)
        if swatch is None:
            color = theme.CONSTELLATION_COLORS.get(
                key, theme.CONSTELLATION_COLORS["OTHER"])
            swatch = tk.PhotoImage(width=10, height=10)
            swatch.put(color, to=(0, 0, 10, 10))
            self._swatches[key] = swatch
        return swatch

    def _row_values(self, p, now):
        state = p.state(now)
        if state == "now":
            when = "GEO (always up)" if p.geo else "OVERHEAD NOW"
        else:
            when = sa.fmt_countdown(p.seconds_until_rise(now))

        v = self._visibility_for(p)
        if v is None:
            vis = viselev = viswin = ""
        elif v.error:
            vis, viselev, viswin = "?", "", v.error[:24]
        elif not v.visible:
            vis, viselev, viswin = "0%", "", "out of view"
        else:
            vis = skyview.fmt_fraction(v)
            viselev = f"{v.best_el_deg:.1f}°"
            viswin = (f"{history.fmt_berlin(v.start_unix, '%H:%M:%S')}"
                      f"–{history.fmt_berlin(v.end_unix, '%H:%M:%S')}")

        return (when,
                history.fmt_berlin(p.rise_unix, "%m-%d %H:%M:%S"),
                p.name, p.catnr,
                f"{p.peak_el_deg:.1f}°",
                vis, viselev, viswin,
                _fmt_dur(p.duration_s),
                f"{p.freq_mhz:g}" if p.freq_mhz else "",
                f"{p.estimated_gb():.1f}" if p.samplerate_mhz else "")

    def _row_tags(self, p, now):
        state = p.state(now)
        tags = [state]
        if p.geo:
            tags.append("geo")
        elif state == "upcoming" and p.seconds_until_rise(now) < 900:
            tags.append("soon")
        if p.source == "placeholder":
            tags.append("placeholder")

        v = self._visibility_for(p)
        if v is not None and not v.error:
            if not v.visible:
                tags.append("blocked")
            elif v.fraction < 0.5:
                tags.append("partial")
        return tuple(tags)

    def _show_empty_state(self, now, blocked):
        total = len(self._passes)
        if total == 0:
            reason = ("nothing matched — lower the horizon floor, widen the "
                      "time horizon, or re-plan")
        elif blocked:
            reason = (f"all {blocked} upcoming pass(es) fall outside your sky "
                      f"view ({self.mask().describe()}) — open more sectors, or "
                      f"untick 'Hide out-of-view'")
        elif self.hide_past.get():
            newest = max(p.set_unix for p in self._passes)
            reason = (f"all {total} pass(es) in this plan are already over "
                      f"(last ended {sa.fmt_countdown(newest - now)}) — "
                      f"press 'Re-plan'")
        else:
            reason = "no rows to show"
        self.tree.insert("", "end",
                         values=("—", "", reason, "", "", "", "", "", "", "", ""),
                         tags=("past",))

    def _update_summary(self, now, blocked, mask):
        s = sa.summarise(self._passes, now)
        nxt, best = s["next"], s["best"]
        parts = [f"{s['total']} pass(es)", f"{s['visible_now']} overhead now",
                 f"{s['upcoming']} upcoming"]

        if self._visibility and not mask.is_full_sky():
            observable = [p for p in self._passes
                          if (self._visibility_for(p) or
                              skyview.Visibility()).visible]
            parts.append(f"{len(observable)} observable in view")
            if blocked:
                parts.append(f"{blocked} blocked")
            if observable:
                pick = max(observable,
                           key=lambda p: self._visibility_for(p).best_el_deg)
                pv = self._visibility_for(pick)
                parts.append(f"best in view: {pick.name} {pv.best_el_deg:.0f}° "
                             f"{skyview.great_circle_label(pv.best_az_deg)}")
        else:
            if nxt:
                parts.append(f"next: {nxt.name} "
                             f"{sa.fmt_countdown(nxt.seconds_until_rise(now))}")
            if best:
                parts.append(f"best: {best.name} {best.peak_el_deg:.0f}°")

        parts.append(f"~{s['total_gb']:.0f} GB if all captured")
        self.summary.configure(text="   ·   ".join(parts))

        if mask.is_blind():
            self.chip.set("no sky view open", theme.FAIL)
        elif s["visible_now"]:
            self.chip.set(f"{s['visible_now']} overhead now", theme.OK)
        elif nxt:
            self.chip.set(f"next {sa.fmt_countdown(nxt.seconds_until_rise(now))}",
                          theme.C1)
        else:
            self.chip.set("nothing scheduled", theme.MUTED)

    def _schedule(self):
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(REFRESH_MS, self._tick)

    def _tick(self):
        # Only the countdown column changes between plan rewrites, so refill
        # rather than re-reading the provider or re-propagating every 15 s.
        # _refill redraws the skymap too, off the same cached tracks.
        self._refill()
        self._schedule()

    # ── actions ───────────────────────────────────────────────────────────
    def _selected_pass(self):
        selection = self.tree.selection()
        if not selection:
            return None
        index = self.tree.index(selection[0])
        rows = getattr(self, "_visible_rows", [])
        return rows[index] if index < len(rows) else None

    def _show_detail(self):
        p = self._selected_pass()
        if p is None:
            self.detail.configure(text="")
            return
        bits = [f"{p.name} (CATNR {p.catnr})", f"peak {p.peak_el_deg:.1f}°",
                f"{p.freq_mhz:g} MHz @ {p.samplerate_mhz:g} MS/s"]
        v = self._visibility_for(p)
        if v is not None and v.sectors_crossed:
            bits.append("crosses " + "·".join(v.sectors_crossed))
        if v is not None and v.visible:
            bits.append(f"in view {v.fraction * 100:.0f}% for "
                        f"{_fmt_dur(v.duration_s)}, best {v.best_el_deg:.1f}° at "
                        f"{skyview.great_circle_label(v.best_az_deg)}")
        elif v is not None and not v.error:
            bits.append("never enters your sky view")
        self.detail.configure(text="   ·   ".join(bits))

    def _capture_selected(self):
        p = self._selected_pass()
        if p is None:
            self.app.error("No pass selected",
                           "Select a row first — the capture is set up from it.")
            return
        if p.source == "placeholder":
            self.app.error(
                "Placeholder pass",
                "This row is synthetic. Run the planner (or register a real "
                "provider) before capturing from this table.")
            return

        v = self._visibility_for(p)
        mask = self.mask()
        if v is not None and not v.error and not v.visible and not mask.is_full_sky():
            self.app.error(
                "Outside your sky view",
                f"{p.name} never enters {mask.describe()}.\n\n"
                f"It crosses {'·'.join(v.sectors_crossed) or 'unknown sectors'}.\n\n"
                "Open the relevant sectors on the compass if you can actually "
                "see that part of the sky.")
            return

        values = {
            "sat": p.catnr or p.name,
            "next_only": True,
            "min_elev": f"{max(0, int(p.peak_el_deg) - 2)}",
            "run_mode": "run",
            "session_mode": "for",
            "for": "6h",
        }
        # Same inheritance the Automation tab applies, so a capture started from
        # this table records with the settings this table is displaying.
        values.update(radio_settings.auto_overrides())

        # A masked pass should be captured over the part you can SEE, not
        # centred on a peak that happens behind a building.
        if v is not None and v.visible and not mask.is_full_sky():
            _start, duration = skyview.suggest_capture_window(p, v)
            if duration > 0:
                values["max_sec"] = str(int(duration))
                self.app.console.gui_note(
                    f"sky view trims this capture to the visible "
                    f"{int(duration)} s (best {v.best_el_deg:.0f}° at "
                    f"{skyview.great_circle_label(v.best_az_deg)}) instead of "
                    f"centring on the obstructed peak")

        self.app.console.gui_note(
            f"targeting {p.name} (CATNR {p.catnr}) — soop_capture.sh will wait "
            f"for its next pass and write the sidecar MAIN.py reads")
        self.app.console.gui_note(
            f"radio: {radio_settings.summary_line()}")
        self.app.run_job(jobs.autocapture_job(values))

    def _replan(self):
        self.app.run_job(jobs.planner_job({"mode": "now_plus_24h", "nice": 10}),
                         on_finish=lambda _h: self.refresh())


def _fmt_dur(seconds):
    s = int(seconds or 0)
    if s <= 0:
        return ""
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"
