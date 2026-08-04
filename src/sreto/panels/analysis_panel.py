"""
analysis_panel.py — waterfalls, IQ dashboard, cross-correlation, physics.

Drives MAIN.py's pipeline. Every control on this panel maps to exactly one
assignment in MAIN.py's USER PARAMETERS block (see main_params.PARAM_SPECS),
and the form is populated FROM that block on load — so what you see when the
GUI opens is what `python MAIN.py` would do right now.

The memory readout mirrors MAIN.py's own pre-flight estimate (MAIN.py:281-296).
It exists because being OOM-killed mid-run gives no diagnosis at all — just
'zsh: killed' after twenty minutes — and the two knobs that prevent it
(PROCESS_PERCENTAGE, GLOBAL_DECIMATION_FACTOR) are right next to it.
"""

import json
import os
import tkinter as tk
from tkinter import filedialog, ttk

from .. import jobs, main_params, paths, system_open, theme, widgets

# MAIN.py:284-291 — IQ bytes/frame and the waterfall+phase array cost per cell.
_IQ_BYTES_PER_FRAME = 16
_WF_BYTES_PER_CELL = 44
_WF_BINS = 2048

# Stages that hard-require two RX chains, with the line that raises. The whole
# method is interferometric: rx1 (direct/RE) against rx2 (reflected/GR).
DUAL_CHANNEL_STAGES = [
    ("phase waterfalls + 1D", "waterfalls.py:334", None),
    ("IQ dashboard", "iq_dashboard.py:121", "RUN_IQ_DASHBOARD_CALCULATIONS"),
    ("band cross-correlation", "band_correlator.py:137", "RUN_SIGNAL_XCORR"),
    ("physics extraction", "physics.py:608", "RUN_PHYSICS_EXTRACTION"),
]


def is_dual_channel(meta):
    """MAIN.py:266's own test, so the GUI and MAIN.py never disagree."""
    return "2" in str(meta.get("channels", "1"))


def _json_is_dual_channel(json_path):
    """Same test, straight off disk. Unreadable metadata is not filtered out —
    hiding a capture because its .json glitched would be worse than listing it."""
    try:
        with open(json_path) as f:
            return is_dual_channel(json.load(f))
    except (OSError, ValueError):
        return True


class AnalysisPanel(ttk.Frame):

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self.defaults = {}
        self.forms = {}
        self._captures = []
        self._build()
        # interactive=False: this runs inside App._build, before the window has
        # ever been mapped. A modal dialog here has no window to be modal to and
        # blocks construction forever — which is how "the app opens without a
        # science repository" quietly stopped being true.
        self.load_defaults(interactive=False)
        self.refresh_captures()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        scroll = widgets.ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        root = scroll.inner

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="Analysis", style="Submenu.TLabel").pack(side="left")
        ttk.Label(header, text=f"→ python {paths.rel(paths.MAIN_PY)} "
                               f"(parameterised copy — MAIN.py is not modified)",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        # ── capture selection ──
        pick = widgets.Card(root, "Capture", "must have a .json next to the .bin")
        pick.pack(fill="x", pady=(8, 10))

        row = ttk.Frame(pick.body, style="Panel.TFrame")
        row.pack(fill="x")
        self.capture_var = tk.StringVar()
        self.capture_combo = ttk.Combobox(row, textvariable=self.capture_var,
                                          state="readonly", width=64)
        self.capture_combo.pack(side="left", fill="x", expand=True)
        self.capture_combo.bind("<<ComboboxSelected>>", self._on_capture_selected)
        ttk.Button(row, text="Browse…", command=self._browse, width=10).pack(
            side="left", padx=(8, 0))
        ttk.Button(row, text="Refresh", command=self.refresh_captures, width=9).pack(
            side="left", padx=(6, 0))

        self.dual_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            pick.body, style="Panel.TCheckbutton", variable=self.dual_only,
            command=self.refresh_captures,
            text="Only dual-channel captures — single-channel ones cannot run "
                 "this pipeline at all").pack(anchor="w", pady=(6, 0))

        self.capture_info = ttk.Label(pick.body, style="PanelMuted.TLabel",
                                      font=theme.F.mono_small,
                                      justify="left")
        self.capture_info.pack(anchor="w", pady=(8, 0))

        # ── grouped parameter cards ──
        for group in ("speed", "band", "stages", "geometry", "window", "xcorr",
                      "physics", "output"):
            specs = [s for s in main_params.PARAM_SPECS
                     if s[3] == group and s[0] != "file_name"]
            if not specs:
                continue
            card = widgets.Card(root, main_params.GROUP_TITLES.get(group, group))
            card.pack(fill="x", pady=(0, 10))
            cols = 1 if group == "output" else (3 if group == "stages" else 2)
            form = widgets.Form(card.body, columns=cols, style="Panel.TFrame")
            form.pack(fill="x")
            for name, kind, label, _grp, help_text in specs:
                form.add(name, label, kind=kind,
                         choices=main_params.CHOICES.get(name),
                         help_text=help_text,
                         width=48 if kind == "path" else 14)
            self.forms[group] = form

            if group == "speed":
                self.memory_label = ttk.Label(
                    card.body, style="PanelMuted.TLabel",
                    font=theme.F.mono_small)
                self.memory_label.pack(anchor="w", pady=(8, 0))
                for field in form.fields.values():
                    field.widget.bind("<KeyRelease>", self._refresh_memory, add="+")

        # ── run options ──
        runopts = widgets.Card(root, "Run options")
        runopts.pack(fill="x", pady=(0, 10))
        self.low_priority = tk.BooleanVar(value=True)
        self.auto_open = tk.BooleanVar(value=True)
        self.auto_reveal = tk.BooleanVar(value=True)
        ttk.Checkbutton(runopts.body, style="Panel.TCheckbutton",
                        variable=self.low_priority,
                        text="Run at low CPU priority (nice 10) — keeps the "
                             "machine responsive for other work").pack(anchor="w")
        ttk.Checkbutton(runopts.body, style="Panel.TCheckbutton",
                        variable=self.auto_open,
                        text="Open the figures this run produced when it "
                             "finishes").pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(runopts.body, style="Panel.TCheckbutton",
                        variable=self.auto_reveal,
                        text="Reveal the output folder when it finishes").pack(
            anchor="w", pady=(4, 0))

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(4, 6))
        ttk.Button(actions, text="Run analysis", style="Accent.TButton",
                   command=self._start).pack(side="left")
        ttk.Button(actions, text="Reload MAIN.py defaults",
                   command=self.load_defaults).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Preview overrides",
                   command=self._preview).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open figures folder",
                   command=lambda: self.app.reveal(self._save_dir())).pack(
            side="left", padx=(8, 0))

    # ── data ──────────────────────────────────────────────────────────────
    def load_defaults(self, interactive=True):
        """Populate every field from MAIN.py's current USER PARAMETERS block.

        `interactive` is False when this is called during construction. A
        missing science repository is the EXPECTED state then — the app is
        designed to open and explain itself without one — so it is reported on
        the console and the panel builds empty, rather than raised as a modal
        the user never asked for. Pressing 'Reload MAIN.py defaults' is a
        question, and a question deserves a dialog; opening the window is not.
        """
        try:
            self.defaults = main_params.read_defaults()
        except main_params.MainParamError as e:
            if interactive:
                self.app.error("MAIN.py", str(e))
            else:
                self.app.console.gui_error(f"MAIN.py: {str(e).splitlines()[0]}")
            return
        for form in self.forms.values():
            form.reset(self.defaults)
        default_file = self.defaults.get("file_name", "")
        if default_file and not self.capture_var.get():
            self.capture_var.set(default_file)
        self._refresh_memory()
        self._describe_capture(self.capture_var.get())

    def refresh_captures(self):
        self._captures = system_open.newest_captures(paths.data_dirs())
        if self.dual_only.get():
            self._captures = [(b, j) for b, j in self._captures
                              if _json_is_dual_channel(j)]
        names = [os.path.basename(b) for b, _j in self._captures]
        current = self.capture_var.get()
        self.capture_combo.configure(values=names)
        if current in names:
            self.capture_var.set(current)
        elif names:
            self.capture_var.set(names[0])
        self._describe_capture(self.capture_var.get())

    def _browse(self):
        initial = paths.DATA_DIR if os.path.isdir(paths.DATA_DIR) else paths.REPO_ROOT
        path = filedialog.askopenfilename(
            title="Select a capture", initialdir=initial,
            filetypes=[("Capture data", "*.bin"), ("Capture metadata", "*.json"),
                       ("All files", "*.*")])
        if not path:
            return
        name = os.path.basename(path)
        if name.endswith(".json"):
            name = name[:-5] + ".bin"
        directory = os.path.dirname(os.path.abspath(path))
        if directory not in paths.data_dirs():
            # MAIN.py resolves file_name against ITS DATA_DIRS list; a file from
            # anywhere else would be reported as missing three seconds in.
            self.app.error(
                "Capture outside MAIN.py's search path",
                "MAIN.py looks for captures in:\n\n"
                + "\n".join(f"  • {d}" for d in paths.data_dirs())
                + f"\n\nThe file you picked is in:\n  {directory}\n\n"
                  "Move or symlink it into one of those directories, or add the "
                  "directory to DATA_DIRS in MAIN.py.")
            return
        values = list(self.capture_combo.cget("values"))
        if name not in values:
            self.capture_combo.configure(values=[name] + values)
        self.capture_var.set(name)
        self._describe_capture(name)

    def _on_capture_selected(self, _e=None):
        self._describe_capture(self.capture_var.get())
        self._refresh_memory()

    def _capture_json(self, name):
        if not name:
            return None
        for d in paths.data_dirs():
            candidate = os.path.join(d, name.replace(".bin", ".json"))
            if os.path.exists(candidate):
                return candidate
        return None

    def _describe_capture(self, name):
        path = self._capture_json(name)
        if path is None:
            self.capture_info.configure(
                text="no .json found for this name in "
                     + ", ".join(paths.rel(d) for d in paths.data_dirs()),
                foreground=theme.AMBER)
            return
        try:
            with open(path) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            self.capture_info.configure(text=f"unreadable metadata: {e}",
                                        foreground=theme.FAIL)
            return

        bin_path = path[:-5] + ".bin"
        size = os.path.getsize(bin_path) if os.path.exists(bin_path) else 0
        status = str(meta.get("capture_status", "")).upper()
        bits = [
            f"{meta.get('frequency_MHz', '?')} MHz",
            f"{meta.get('samplerate_MHz', '?')} MS/s",
            f"bw {meta.get('bandwidth_MHz', '?')} MHz",
            f"ch {meta.get('channels', '?')}",
            f"{meta.get('bitmode', '?')}",
            f"{meta.get('duration_sec', '?')} s",
            jobs.human_bytes(size) if size else "no .bin on disk",
        ]
        sidecar = bin_path[:-4] + ".soop.json"
        if os.path.exists(sidecar):
            bits.append("has .soop.json sidecar")
        exp = str(meta.get("experiment_info", "")).strip()
        text = "  ·  ".join(bits)
        if exp and exp != "none":
            text += f"\n{exp}"
        color = theme.MUTED
        if status in ("SHORT", "IN_PROGRESS"):
            color = theme.AMBER
            text += f"\ncapture_status = {status}"
        elif not os.path.exists(bin_path):
            color = theme.FAIL

        # The single most consequential property of a capture for THIS pipeline.
        # Say it here, in red, before the run — not in a traceback after it.
        if not is_dual_channel(meta):
            color = theme.FAIL
            text += ("\nSINGLE-CHANNEL capture — the reflectometry pipeline "
                     "compares rx1 (direct/RE) against rx2 (reflected/GR) and "
                     "cannot run on this. Only the power waterfall would work.")

        self.capture_info.configure(text=text, foreground=color)
        self._meta = meta
        self._bin_size = size

    def _refresh_memory(self, _e=None):
        """MAIN.py's own pre-flight estimate, before the run instead of during."""
        if not hasattr(self, "memory_label"):
            return
        size = getattr(self, "_bin_size", 0)
        if not size:
            self.memory_label.configure(text="", foreground=theme.MUTED)
            return
        form = self.forms.get("speed")
        try:
            pct = float(form.fields["PROCESS_PERCENTAGE"].get() or
                        self.defaults.get("PROCESS_PERCENTAGE", 1.0))
            dec = int(float(form.fields["GLOBAL_DECIMATION_FACTOR"].get() or
                            self.defaults.get("GLOBAL_DECIMATION_FACTOR", 10)))
        except (ValueError, KeyError):
            self.memory_label.configure(text="estimate needs numeric values",
                                        foreground=theme.AMBER)
            return
        if pct <= 0 or dec <= 0:
            self.memory_label.configure(text="fraction and decimation must be > 0",
                                        foreground=theme.AMBER)
            return

        meta = getattr(self, "_meta", {})
        vals_per_frame = 4 if "2" in str(meta.get("channels", "1")) else 2
        bytes_per_val = 1 if "8" in str(meta.get("bitmode", "16bit")) else 2
        total_frames = size // (vals_per_frame * bytes_per_val)
        frames = int(total_frames * pct)

        iq_gb = frames * _IQ_BYTES_PER_FRAME / 1e9
        rows = max(1, (frames // _WF_BINS) // dec)
        wf_gb = rows * _WF_BINS * _WF_BYTES_PER_CELL / 1e9
        total_gb = iq_gb + wf_gb

        try:
            ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        except (ValueError, OSError, AttributeError):
            ram_gb = 0.0

        text = (f"estimate: IQ {iq_gb:.2f} GB + waterfalls {wf_gb:.2f} GB "
                f"= {total_gb:.2f} GB")
        color = theme.MUTED
        if ram_gb:
            text += f" of {ram_gb:.1f} GB RAM  ({rows:,} waterfall rows)"
            if total_gb > 0.7 * ram_gb:
                color = theme.FAIL
                text += ("\nover 70% of RAM — the OS may kill the run. Lower the "
                         "process fraction or raise the decimation factor.")
            elif total_gb > 0.4 * ram_gb:
                color = theme.AMBER
        self.memory_label.configure(text=text, foreground=color)

    # ── running ───────────────────────────────────────────────────────────
    def _save_dir(self):
        form = self.forms.get("output")
        if form and form.fields["SAVE_DIR"].get():
            return form.fields["SAVE_DIR"].get()
        return self.defaults.get("SAVE_DIR") or paths.ANALYSIS_DIR

    def overrides(self):
        """Only fields the user actually filled in become overrides.

        A blank field means "whatever MAIN.py says", which keeps the generated
        copy as close to the original as possible and makes the diff readable.
        """
        out = {"file_name": self.capture_var.get().strip()}
        for form in self.forms.values():
            for name, field in form.fields.items():
                value = field.get()
                if field.kind == "bool":
                    out[name] = bool(value)
                elif str(value).strip() != "":
                    out[name] = value
        return out

    def _check_dual_channel(self, name):
        """Refuse a run MAIN.py cannot finish. Returns True when it is safe.

        This is a hard block rather than a warning because there is no way
        through it: MAIN.py calls generate_phase_waterfalls_and_1d
        UNCONDITIONALLY (MAIN.py:507), so no combination of stage toggles lets a
        single-channel capture complete. Offering "run anyway" would only spend
        the read time to arrive at the same ValueError.
        """
        meta = getattr(self, "_meta", None) or {}
        if is_dual_channel(meta):
            return True

        stages = "\n".join(
            f"  • {label:<26s} raises at {where}"
            + ("" if toggle else "   ← NOT toggleable")
            for label, where, toggle in DUAL_CHANNEL_STAGES)

        self.app.error(
            "Single-channel capture",
            f"'{name}' was recorded with channels = "
            f"{str(meta.get('channels', '?'))!r}.\n\n"
            "Reflectometry is interferometric: it compares rx1 (direct / RE) "
            "against rx2 (reflected / GR). These stages need both:\n\n"
            f"{stages}\n\n"
            "The phase stage is called unconditionally at MAIN.py:507, so "
            "unticking boxes cannot get past it — the run would fail after "
            "reading the file.\n\n"
            "Use a capture whose name ends in _ch1_2, or re-capture with "
            "channels '1,2' on the Capture tab.")
        return False

    def _preview(self):
        try:
            overrides = self.overrides()
            source = main_params.build_overridden_source(overrides)
        except main_params.MainParamError as e:
            self.app.error("MAIN.py parameters", str(e))
            return
        self.app.console.gui_banner("ANALYSIS — PARAMETER OVERRIDES (nothing has run)")
        for name in sorted(overrides):
            current = self.defaults.get(name)
            new = overrides[name]
            marker = " " if str(current) == str(new) else "*"
            self.app.console.gui_note(
                f" {marker} {name:<32s} {str(current):<28s} -> {new}")
        self.app.console.gui_note(
            f"{len(source.splitlines())} line copy would be written to "
            f"{paths.rel(paths.TMP_DIR)}")

    def _start(self):
        name = self.capture_var.get().strip()
        if not name:
            self.app.error("No capture selected",
                           "Pick a capture file before running the analysis.")
            return
        if self._capture_json(name) is None:
            self.app.error(
                "Capture not found",
                f"MAIN.py needs '{name.replace('.bin', '.json')}' in one of:\n\n"
                + "\n".join(f"  • {d}" for d in paths.data_dirs()))
            return
        if not self._check_dual_channel(name):
            return
        try:
            job = jobs.analysis_job(self.overrides(),
                                    nice=10 if self.low_priority.get() else 0)
        except main_params.MainParamError as e:
            self.app.error("MAIN.py parameters", str(e))
            return
        self.app.run_job(job,
                         open_artifacts=self.auto_open.get(),
                         reveal_output=self.auto_reveal.get())
