"""
physics_panel.py — the physics menu: what the numbers mean, and whether to
believe them.

An IQ sample is meaningless on its own, and that is not a gap in anyone's DSP
knowledge — "meaning" here is the chain of physical arguments that connects a
complex number off a radio to a claim about water in soil. Every other panel in
this app launches a tool. This one exists to make that chain legible.

Three things are on screen, in this order, deliberately:

  1. THE CHAIN — eleven stages from raw IQ to soil moisture. Each carries its
     current value, a plain-language statement of what the quantity physically
     IS, and its epistemic status. That last part is the point: Γ is measured,
     ε_r is derived from settled electromagnetics, and θ_v comes out of a
     polynomial fitted to soil samples. Painting all three the same colour is
     how a retrieval starts lying, so status is the primary visual encoding.

  2. GROUND TRUTH — what a probe in the ground actually read. Editable here,
     because a retrieval that cannot be scored against truth is not a
     retrieval, and the fastest way to never collect truth is to make
     recording it someone else's job.

  3. THE RUN — physics.py on one capture, without waterfalling the whole .bin
     first.

The explanations are NOT written here. They live in sreto.analysis.physics
beside the code they describe (build_pipeline), so a menu that explains one
pipeline while a different one runs is structurally impossible. That module is
vendored, so this tab works on a bare `pip install sreto[analysis]` with no
sdr_r science repo configured — unlike the Analysis tab, which drives MAIN.py
in the science repo and needs one.

Figures are written to the capture's physics/ folder and opened by the app's
normal post-run handling; there is no embedded viewer in this package.
"""

import os
import threading
import tkinter as tk
from tkinter import ttk

from .. import _lazy_physics, jobs, paths, system_open, theme, widgets

# Status → colour. This is the panel's main signal, so it gets the strong
# colours: green for what was actually observed, red for a stand-in that
# invalidates everything below it.
STATUS_COLORS = {
    "MEASURED": theme.OK,
    "DERIVED": theme.C1,
    "MODELLED": theme.C2,
    "ASSUMED": theme.AMBER,
    "PLACEHOLDER": theme.FAIL,
    "NOT MODELLED": theme.MUTED,
    "UNVALIDATED": theme.AMBER,
    "VOID": theme.FAIL,
}

SUMMARY_NAME = "06_05_Physics_Summary.json"

# One line, at the top, for someone who has never met this instrument.
PRIMER = (
    "Two antennas listen to the same distant transmitter: rx1 straight at it, "
    "rx2 at the ground. Comparing the two tells you what the ground did to the "
    "wave — and because water's dielectric constant (~80) dwarfs dry soil's "
    "(~3), what the ground did is dominated by how wet it is. Everything below "
    "is that one idea, made quantitative and then checked against a probe."
)


class StageRow(ttk.Frame):
    """One link of the retrieval chain, on ONE line.

        3.  REFLECTIVITY          Γ      −5.90 dB              MEASURED

    WHY ONE LINE. The chain is eleven stages, and the point of drawing it as a
    chain is that you can see the WHOLE of it at once — where the statuses stop
    being green is the single most useful thing this panel reports, and it is
    invisible if reading stage 4 requires scrolling stage 1 off the screen.
    The previous row was five stacked labels plus a separator, so eleven stages
    came to roughly seventy rows and nobody ever saw the end of it.

    Nothing was deleted to get there. The plain-language meaning, the numeric
    detail and the 'why this step exists' paragraph now arrive on HOVER, and
    clicking pins them open underneath the row for as long as you want them —
    so the information is one gesture away instead of permanently in the way.
    """

    def __init__(self, master, index, stage, **kw):
        kw.setdefault("style", "Panel.TFrame")
        super().__init__(master, **kw)
        self.stage = stage
        self._open = False
        color = STATUS_COLORS.get(stage.status, theme.MUTED)

        head = ttk.Frame(self, style="Panel.TFrame")
        head.pack(fill="x")
        self.head = head

        # The disclosure marker doubles as the row number's companion: it is
        # what tells you the row has more behind it than it is showing.
        self._chev = ttk.Label(head, text="▸", style="PanelMuted.TLabel",
                               font=theme.F.mono_small, width=2)
        self._chev.pack(side="left")

        ttk.Label(head, text=f"{index}.", style="PanelMuted.TLabel",
                  font=theme.F.mono_small_bold, width=3).pack(side="left")

        ttk.Label(head, text=stage.title, style="Panel.TLabel",
                  font=theme.F.ui_bold).pack(side="left")

        if stage.symbol:
            ttk.Label(head, text=stage.symbol, style="PanelMuted.TLabel",
                      font=theme.F.mono_small).pack(side="left", padx=(8, 0))

        # Status on the far right, value just inside it. Both are fixed
        # positions so the eye can run straight down either column.
        badge = stage.status + ("  ⚠" if stage.warn else "")
        ttk.Label(head, text=badge, style="Panel.TLabel", foreground=color,
                  font=theme.F.small_bold).pack(side="right")
        ttk.Label(head, text=stage.value_text(), style="Panel.TLabel",
                  foreground=color, font=theme.F.mono_small_bold).pack(
            side="right", padx=(0, 18))

        # The pinned detail, built once and shown on demand.
        self.detail_box = ttk.Frame(self, style="Panel.TFrame")
        ttk.Label(self.detail_box, text=stage.plain, style="PanelMuted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w")
        if stage.detail:
            ttk.Label(self.detail_box, text=str(stage.detail),
                      style="PanelMuted.TLabel", font=theme.F.mono_small,
                      wraplength=720, justify="left").pack(anchor="w",
                                                           pady=(3, 0))
        if stage.why:
            ttk.Label(self.detail_box, text=stage.why,
                      style="PanelMuted.TLabel", wraplength=720,
                      justify="left").pack(anchor="w", pady=(5, 0))

        # Hover shows everything; click pins it. Bound on the children too,
        # because a ttk.Frame only receives <Enter> where no child covers it —
        # binding the frame alone makes the tooltip fire in the gaps between
        # the labels and nowhere else.
        tip = self._tooltip_text()
        for w in [head, self._chev] + list(head.winfo_children()):
            widgets.Tooltip(w, tip, wrap=520)
            w.bind("<Button-1>", self._toggle, add="+")
            try:
                w.configure(cursor="hand2")
            except tk.TclError:                             # pragma: no cover
                pass

    def _tooltip_text(self):
        """Everything the row is not showing, in the order it should be read."""
        parts = [self.stage.plain]
        if self.stage.detail:
            parts.append(str(self.stage.detail))
        if self.stage.why:
            parts.append(f"Why this step exists:\n{self.stage.why}")
        parts.append("(click to keep this open)")
        return "\n\n".join(p for p in parts if p)

    def _toggle(self, _event=None):
        self._open = not self._open
        if self._open:
            self.detail_box.pack(fill="x", anchor="w", padx=(31, 0),
                                 pady=(4, 2))
        else:
            self.detail_box.pack_forget()
        self._chev.configure(text="▾" if self._open else "▸")
        return "break"


class PhysicsPanel(ttk.Frame):

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._captures = []
        self._summary = None
        self._truth = None
        self._stage_rows = []
        self._build()
        self.refresh_captures()
        self._load_async()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        scroll = widgets.ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        root = scroll.inner

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="Physics", style="Submenu.TLabel").pack(side="left")
        ttk.Label(header, text="→ IQ samples to soil moisture, one step at a time",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        primer = widgets.Card(root, "What this actually measures")
        primer.pack(fill="x", pady=(8, 10))
        ttk.Label(primer.body, text=PRIMER, style="PanelMuted.TLabel",
                  wraplength=820, justify="left").pack(anchor="w")

        # ── capture selection ──
        pick = widgets.Card(root, "Capture",
                            "results are read from this capture's physics folder")
        pick.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(pick.body, style="Panel.TFrame")
        row.pack(fill="x")
        self.capture_var = tk.StringVar()
        self.capture_combo = ttk.Combobox(row, textvariable=self.capture_var,
                                          state="readonly", width=58)
        self.capture_combo.pack(side="left", fill="x", expand=True)
        self.capture_combo.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        ttk.Button(row, text="Refresh", width=9,
                   command=self.refresh_captures).pack(side="left", padx=(8, 0))
        self.source_label = ttk.Label(pick.body, style="PanelMuted.TLabel",
                                      font=theme.F.mono_small, justify="left")
        self.source_label.pack(anchor="w", pady=(8, 0))

        # ── ground truth ──
        gt = widgets.Card(root, "In-situ ground truth",
                          "measured in the field — the only line that can "
                          "falsify the retrieval")
        gt.pack(fill="x", pady=(0, 10))
        self._build_ground_truth(gt.body)

        # ── the chain ──
        chain = widgets.Card(root, "Retrieval chain",
                             "hover a stage for why it exists")
        chain.pack(fill="x", pady=(0, 10))
        self.legend = ttk.Frame(chain.body, style="Panel.TFrame")
        self.legend.pack(fill="x", pady=(0, 8))
        self.chain_body = ttk.Frame(chain.body, style="Panel.TFrame")
        self.chain_body.pack(fill="both", expand=True)
        self.chain_status = ttk.Label(self.chain_body, style="PanelMuted.TLabel",
                                      text="loading the retrieval chain…")
        self.chain_status.pack(anchor="w")

        # ── run ──
        runcard = widgets.Card(root, "Run the retrieval",
                               "physics.py on this capture only — seconds, not "
                               "minutes")
        runcard.pack(fill="x", pady=(0, 10))
        self.run_form = widgets.Form(runcard.body, columns=3,
                                     style="Panel.TFrame")
        self.run_form.pack(fill="x")
        self.run_form.add("elevation", "Elevation (deg)", placeholder="from geometry",
                          help_text="Transmitter elevation at the specular point. "
                                    "Blank uses physics.py's own default, which "
                                    "falls back to a loud 45° placeholder.")
        self.run_form.add("polarization", "Polarization", kind="choice",
                          choices=["", "V", "H"],
                          help_text="The LINEAR orientation of both antennas. "
                                    "The transmitter is circularly polarised, "
                                    "but the 3 dB CP→LP mismatch is identical "
                                    "in both chains and cancels in "
                                    "Γ = P_r/P_d, so there is no "
                                    "polarisation-loss term to apply. At V the "
                                    "ratio inverts |R_vv|²; at H, |R_hh|².\n\n"
                                    "V has a Brewster branch: below θe_B, "
                                    "|R_vv| is two-valued in ε′ and the "
                                    "inversion has no unique answer.")
        self.run_form.add("soil_model", "Soil model", kind="choice",
                          choices=["mironov", "hallikainen", "topp"],
                          default="mironov",
                          help_text="mironov (PRIMARY): the generalized "
                                    "refractive mixing model SMOS and SMAP run "
                                    "at L-band, and the only one of the three "
                                    "that separates BOUND from FREE water. "
                                    "Takes CLAY only; sand is not an input.\n\n"
                                    "hallikainen: sand AND clay, published "
                                    "1.4-18 GHz. Cross-check.\n\n"
                                    "topp: no texture, fitted below 1 GHz, and "
                                    "carries no imaginary part — so it cannot "
                                    "report a sensing depth.")
        self.run_form.add("bw_khz", "Analysis bandwidth (kHz)", placeholder="100",
                          help_text="Width of the extraction window. Same "
                                    "convention the waterfalls shade.")
        self.run_form.add("block_sec", "Coherent block (s)", placeholder="0.1",
                          help_text="Averaging window per retrieval point.")
        self.run_form.add("offset_mhz", "Centre offset (MHz)", placeholder="0",
                          help_text="Beacon offset from the tuned centre frequency.")

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(4, 6))
        ttk.Button(actions, text="Run physics extraction", style="Accent.TButton",
                   command=self._start).pack(side="left")
        ttk.Button(actions, text="Reload results",
                   command=self.reload).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open physics folder",
                   command=lambda: self.app.reveal(self._save_dir())).pack(
            side="left", padx=(8, 0))

    def _build_ground_truth(self, parent):
        self.gt_form = widgets.Form(parent, columns=3, style="Panel.TFrame")
        self.gt_form.pack(fill="x")
        self.gt_form.add("site_name", "Site name", width=22)
        self.gt_form.add("texture_class", "Soil class", kind="choice",
                         choices=["sandy", "sandy loam", "loam", "silt loam",
                                  "clay loam", "clay"],
                         help_text="Picking a class fills sand/clay with its "
                                   "USDA centroid. Replace those with a lab "
                                   "particle-size analysis when you have one.")
        self.gt_form.add("vegetation", "Vegetation cover", width=16,
                         help_text="Recorded, not yet corrected for. Vegetation "
                                   "makes soil look DRIER than it is.")
        self.gt_form.add("sand_pct", "Sand (% weight)",
                         help_text="Input to the Hallikainen dielectric model.")
        self.gt_form.add("clay_pct", "Clay (% weight)",
                         help_text="Input to the Hallikainen dielectric model.")
        self.gt_form.add("rx_height_m", "Antenna height (m)",
                         help_text="Height above the surface. Enables removal of "
                                   "the predicted geometric phase for a moving "
                                   "transmitter.")
        self.gt_form.fields["texture_class"].widget.bind(
            "<<ComboboxSelected>>", self._on_texture_class, add="+")

        ttk.Label(parent, text="Measurements — one row per field reading",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(10, 3))
        self.gt_tree = ttk.Treeview(
            parent, columns=("when", "vwc", "depth", "method"),
            show="headings", height=4)
        for key, label, width in (("when", "timestamp", 170),
                                  ("vwc", "θv (m³/m³)", 100),
                                  ("depth", "depth (m)", 90),
                                  ("method", "method", 220)):
            self.gt_tree.heading(key, text=label)
            self.gt_tree.column(key, width=width, anchor="w")
        self.gt_tree.pack(fill="x")

        add = ttk.Frame(parent, style="Panel.TFrame")
        add.pack(fill="x", pady=(8, 0))
        self.new_form = widgets.Form(add, columns=4, style="Panel.TFrame")
        self.new_form.pack(fill="x")
        self.new_form.add("timestamp", "When", width=20,
                          placeholder="YYYY-MM-DDTHH:MM:SS")
        self.new_form.add("vwc_m3m3", "θv (m³/m³)", placeholder="0.20")
        self.new_form.add("depth_m", "Depth (m)", placeholder="0.05")
        self.new_form.add("method", "Method", width=18, placeholder="in-situ probe")

        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Add measurement",
                   command=self._add_measurement).pack(side="left")
        ttk.Button(buttons, text="Remove selected",
                   command=self._remove_measurement).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Save ground truth", style="Accent.TButton",
                   command=self._save_truth).pack(side="left", padx=(8, 0))
        self.gt_status = ttk.Label(buttons, style="PanelMuted.TLabel")
        self.gt_status.pack(side="left", padx=(12, 0))

    # ── loading ───────────────────────────────────────────────────────────
    def _load_async(self):
        """Import physics off the Tk thread — it costs ~1.5 s of numpy/scipy.

        Doing it inline would freeze the window on the first click of this tab,
        which is exactly the impression the app is trying not to give.
        """
        def work():
            try:
                ok, reason = _lazy_physics.available()
                truth = _lazy_physics.load_truth() if ok else None
                error = "" if ok else reason
            except (OSError, ValueError) as e:
                truth, error = None, str(e)
            self.app.post_to_ui(lambda: self._on_loaded(truth, error))

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, truth, error):
        if error:
            self.chain_status.configure(text=error, foreground=theme.FAIL)
            return
        self._truth = truth or _lazy_physics.default_truth()
        self._fill_ground_truth()
        self.reload()

    def refresh_captures(self):
        self._captures = system_open.newest_captures(paths.data_dirs())
        names = [os.path.basename(b) for b, _j in self._captures]
        current = self.capture_var.get()
        self.capture_combo.configure(values=names)
        if current in names:
            self.capture_var.set(current)
        elif names:
            self.capture_var.set(names[0])
        if _lazy_physics.is_loaded():
            self.reload()

    def _save_dir(self):
        name = self.capture_var.get().strip()
        if not name:
            return paths.ANALYSIS_DIR
        return str(paths.analysis_dir(name, paths.FIG_PHYSICS))

    def _summary_path(self):
        return os.path.join(self._save_dir(), SUMMARY_NAME)

    def reload(self):
        """Re-read this capture's summary and redraw the chain."""
        if not _lazy_physics.is_loaded():
            return
        name = self.capture_var.get().strip()

        path = self._summary_path()
        self._summary = None
        if os.path.exists(path):
            try:
                import json
                with open(path, encoding="utf-8") as f:
                    self._summary = json.load(f)
            except (OSError, ValueError) as e:
                self.source_label.configure(
                    text=f"{paths.rel(path)} is unreadable: {e}",
                    foreground=theme.FAIL)
        if self._summary:
            self.source_label.configure(
                text=f"values from {paths.rel(path)}", foreground=theme.MUTED)
        else:
            self.source_label.configure(
                text=("no physics results for this capture yet — the chain below "
                      "still explains every step. Run the retrieval to fill in "
                      "the numbers."),
                foreground=theme.AMBER)
        self._render_chain()

    # ── the chain ─────────────────────────────────────────────────────────
    def _render_chain(self):
        for row in self._stage_rows:
            row.destroy()
        self._stage_rows = []
        self.chain_status.pack_forget()

        for child in self.legend.winfo_children():
            child.destroy()
        meanings = _lazy_physics.status_meaning()
        ttk.Label(self.legend, text="status:", style="PanelMuted.TLabel",
                  font=theme.F.small_bold).pack(side="left", padx=(0, 6))
        for status in reversed(_lazy_physics.status_order()):
            chip = ttk.Label(self.legend, text=status, style="Panel.TLabel",
                             foreground=STATUS_COLORS.get(status, theme.MUTED),
                             font=theme.F.small_bold)
            chip.pack(side="left", padx=(0, 12))
            widgets.Tooltip(chip, meanings.get(status, ""))

        stages = _lazy_physics.build_pipeline(self._summary, self._truth)
        for i, stage in enumerate(stages, start=1):
            row = StageRow(self.chain_body, i, stage)
            # 2 px, and no separator between rows. The separator was carrying
            # 20 px of padding per stage — 200 px over the chain — to divide
            # rows that a single line each already separates perfectly well.
            row.pack(fill="x", pady=(0, 2))
            self._stage_rows.append(row)

    # ── ground truth ──────────────────────────────────────────────────────
    def _on_texture_class(self, _e=None):
        """Picking a class fills sand/clay with that class's centroid."""
        name = self.gt_form.fields["texture_class"].get()
        preset = _lazy_physics.texture_classes().get(name)
        if preset:
            self.gt_form.fields["sand_pct"].set(preset["sand_pct"])
            self.gt_form.fields["clay_pct"].set(preset["clay_pct"])

    def _fill_ground_truth(self):
        truth = self._truth or {}
        site = truth.get("site") or {}
        texture = site.get("soil_texture") or {}
        veg = site.get("vegetation") or {}
        instrument = truth.get("instrument") or {}
        self.gt_form.set_values({
            "site_name": site.get("name", ""),
            "texture_class": texture.get("class", ""),
            "sand_pct": texture.get("sand_pct", ""),
            "clay_pct": texture.get("clay_pct", ""),
            "vegetation": veg.get("cover", ""),
            "rx_height_m": instrument.get("rx_height_m", ""),
        })
        self._fill_measurements()

    def _fill_measurements(self):
        self.gt_tree.delete(*self.gt_tree.get_children())
        for i, m in enumerate((self._truth or {}).get("measurements", [])):
            self.gt_tree.insert(
                "", "end", iid=str(i),
                values=(m.get("timestamp", ""), m.get("vwc_m3m3", ""),
                        m.get("depth_m", ""), m.get("method", "")))

    def _add_measurement(self):
        values = self.new_form.values()
        raw = str(values.get("vwc_m3m3", "")).strip()
        if not raw:
            self.app.error("No value",
                           "Enter the measured volumetric water content "
                           "(m³/m³) before adding a reading.")
            return
        try:
            vwc = float(raw)
        except ValueError:
            self.app.error("Not a number",
                           f"'{raw}' is not a volumetric water content. "
                           f"Use a fraction such as 0.20, not a percentage.")
            return
        if not 0.0 <= vwc <= 1.0:
            self.app.error(
                "Out of range",
                f"{vwc} m³/m³ is outside 0-1. Volumetric water content is a "
                f"fraction of volume — 20% moisture is 0.20, not 20.")
            return

        depth = str(values.get("depth_m", "")).strip()
        row = {
            "timestamp": str(values.get("timestamp", "")).strip()
                         or _now_iso(),
            "vwc_m3m3": vwc,
            "depth_m": float(depth) if depth else None,
            "method": str(values.get("method", "")).strip() or "in-situ probe",
            "note": "",
        }
        self._truth.setdefault("measurements", []).append(row)
        self._fill_measurements()
        self.gt_status.configure(text="added — not saved yet",
                                 foreground=theme.AMBER)

    def _remove_measurement(self):
        selected = self.gt_tree.selection()
        if not selected:
            return
        keep = [m for i, m in enumerate(self._truth.get("measurements", []))
                if str(i) not in selected]
        self._truth["measurements"] = keep
        self._fill_measurements()
        self.gt_status.configure(text="removed — not saved yet",
                                 foreground=theme.AMBER)

    def _save_truth(self):
        """Write the ground-truth file, then re-score the chain against it."""
        values = self.gt_form.values()
        site = self._truth.setdefault("site", {})
        site["name"] = values.get("site_name", "")
        texture = site.setdefault("soil_texture", {})
        texture["class"] = values.get("texture_class", "")
        for key in ("sand_pct", "clay_pct"):
            raw = str(values.get(key, "")).strip()
            if raw:
                try:
                    texture[key] = float(raw)
                except ValueError:
                    self.app.error("Not a number",
                                   f"{key} must be a percentage by weight, "
                                   f"got '{raw}'.")
                    return
        site.setdefault("vegetation", {})["cover"] = values.get("vegetation", "")
        height = str(values.get("rx_height_m", "")).strip()
        instrument = self._truth.setdefault("instrument", {})
        if height:
            try:
                instrument["rx_height_m"] = float(height)
            except ValueError:
                self.app.error("Not a number",
                               f"Antenna height must be metres, got '{height}'.")
                return

        try:
            path = _lazy_physics.save_truth(self._truth)
        except OSError as e:
            self.app.error("Could not save ground truth", str(e))
            return
        self.gt_status.configure(text=f"saved to {paths.rel(path)}",
                                 foreground=theme.OK)
        self.app.console.gui_note(
            f"ground truth saved: {paths.rel(path)} — "
            f"{len(self._truth.get('measurements', []))} measurement(s)")
        self._render_chain()

    # ── running ───────────────────────────────────────────────────────────
    def _start(self):
        name = self.capture_var.get().strip()
        if not name:
            self.app.error("No capture selected",
                           "Pick a capture before running the retrieval.")
            return
        found = paths.find_capture(name, ".json")
        if found is None:
            self.app.error(
                "Capture not found",
                f"No .json for '{name}' in:\n\n"
                + "\n".join(f"  • {d}" for d in paths.data_dirs()))
            return

        values = dict(self.run_form.values())
        values["json_path"] = str(found)
        values["save_dir"] = self._save_dir()
        if os.path.exists(paths.GROUND_TRUTH_JSON):
            values["ground_truth"] = paths.GROUND_TRUTH_JSON

        job = jobs.physics_job(values)
        self.app.run_job(job, on_finish=lambda _h: self.reload(),
                         open_artifacts=True, reveal_output=False)


def _now_iso():
    """Local time, seconds precision — the format the truth file uses."""
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S")
