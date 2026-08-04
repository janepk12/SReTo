"""
precheck_panel.py — is this machine ready to capture?

Runs prechecks.run_all on a worker thread and shows one row per check with the
FIX, not just the complaint. The radio probe talks to USB and can block for a
second or two, hence the thread; the GUI stays responsive throughout and results
stream in section by section.

The overall verdict is mirrored into the app's status bar, so the answer to
"can I press Start?" is visible from every tab.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk

from .. import prechecks, theme, widgets

_TAGS = {prechecks.OK: "ok", prechecks.WARN: "warn",
         prechecks.FAIL: "fail", prechecks.INFO: "info"}


class PrecheckPanel(ttk.Frame):

    COLUMNS = [
        ("status", "", 60, "center"),
        ("name", "Check", 210, "w"),
        ("detail", "Result", 360, "w"),
        ("hint", "If this is a problem", 520, "w"),
    ]

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._queue = queue.Queue()
        self._running = False
        self._results = []
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="Pre-flight checks", style="Submenu.TLabel").pack(side="left")
        self.chip = widgets.StatusChip(header, "not run yet")
        self.chip.pack(side="right")

        ttk.Label(self, style="Muted.TLabel", wraplength=980, justify="left",
                  text="Radio, disk, directories, Python environment, geometry "
                       "and plan freshness — the things that have ended a run "
                       "before it started. Thresholds match the ones the scripts "
                       "use themselves.").pack(anchor="w", pady=(8, 10))

        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(0, 10))
        self.run_btn = ttk.Button(controls, text="Run pre-checks",
                                  style="Accent.TButton", command=self.run)
        self.run_btn.pack(side="left")
        self.probe_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Probe the bladeRF over USB "
                                       "(a few seconds)",
                        variable=self.probe_var).pack(side="left", padx=(12, 0))
        self.progress = ttk.Progressbar(controls, mode="indeterminate", length=150)

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "hint"))
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("ok", foreground=theme.OK)
        self.tree.tag_configure("warn", foreground=theme.AMBER)
        self.tree.tag_configure("fail", foreground=theme.FAIL,
                                font=theme.F.mono_small_bold)
        self.tree.tag_configure("info", foreground=theme.MUTED)

    # ── running ───────────────────────────────────────────────────────────
    def run(self):
        if self._running:
            return
        self._running = True
        self._results = []
        self.tree.delete(*self.tree.get_children())
        self.run_btn.configure(state="disabled")
        self.progress.pack(side="left", padx=(12, 0))
        self.progress.start(12)
        self.chip.set("running…", theme.RUNNING)

        probe = self.probe_var.get()
        threading.Thread(target=self._worker, args=(probe,), daemon=True,
                         name="prechecks").start()
        self.after(80, self._pump)

    def _worker(self, probe):
        def on_result(section, checks):
            self._queue.put(("section", section, checks))
        try:
            prechecks.run_all(probe_radio=probe, on_result=on_result)
        finally:
            self._queue.put(("done", None, None))

    def _pump(self):
        done = False
        while True:
            try:
                kind, section, checks = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                done = True
                continue
            self._add_section(section, checks)

        if done:
            self._finish()
        elif self._running:
            self.after(80, self._pump)

    def _add_section(self, section, checks):
        for c in checks:
            self._results.append(c)
            self.tree.insert("", "end",
                             values=(c.status, c.name, c.detail, c.hint),
                             tags=(_TAGS.get(c.status, "info"),))

    def _finish(self):
        self._running = False
        self.progress.stop()
        self.progress.pack_forget()
        self.run_btn.configure(state="normal")

        worst, counts = prechecks.summarise(self._results)
        text = (f"{counts.get(prechecks.OK, 0)} ok · "
                f"{counts.get(prechecks.WARN, 0)} warn · "
                f"{counts.get(prechecks.FAIL, 0)} fail")
        color = {prechecks.OK: theme.OK, prechecks.WARN: theme.AMBER,
                 prechecks.FAIL: theme.FAIL}[worst]
        self.chip.set(text, color)
        self.app.set_precheck_status(worst, text)

        self.app.console.gui_banner("PRE-FLIGHT CHECKS")
        for c in self._results:
            line = f"  {c.status:<5s} {c.name:<26s} {c.detail}"
            self.app.console.gui_note(line)
            if c.hint and c.status in (prechecks.WARN, prechecks.FAIL):
                self.app.console.gui_note(f"        → {c.hint}")
