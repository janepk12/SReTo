"""
history_panel.py — the run dashboard.

Reads three logs and shows one timeline: capture.sh's master CSV, its failures
CSV, and the GUI's own journal (history.py explains the merge). Nothing here
writes to the master logs — a capture launched from the GUI is still recorded by
capture.sh, exactly once, in the same file a terminal-launched one lands in.

Colour is the point of this view: SUCCESS green, SHORT/overrun amber, failures
red, still-running blue, and pre-Capture_Status rows grey so an unknown verdict
never reads as a good one.
"""

import tkinter as tk
from tkinter import ttk

from .. import history, paths, system_open, theme

AUTO_REFRESH_MS = 20_000


class HistoryPanel(ttk.Frame):

    COLUMNS = [
        ("when", "When (Berlin)", 140, "w"),
        ("kind", "Kind", 95, "w"),
        ("title", "Target", 380, "w"),
        ("detail", "Detail", 330, "w"),
        ("duration", "Duration", 80, "e"),
        ("size", "Size", 90, "e"),
        ("status", "Status", 110, "w"),
    ]

    def __init__(self, master, app, **kw):
        super().__init__(master, padding=(14, 12), **kw)
        self.app = app
        self._rows = []
        self._after = None
        self._build()
        self.refresh()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="History", style="Submenu.TLabel").pack(side="left")
        ttk.Button(header, text="Refresh", command=self.refresh, width=9).pack(
            side="right")

        # ── headline strip ──
        self.strip = ttk.Frame(self, style="Panel.TFrame", padding=(14, 10))
        self.strip.pack(fill="x", pady=(10, 10))
        self._stat_labels = {}
        for key, title in (("captures", "CAPTURES"), ("volume", "IQ ON DISK"),
                           ("success", "VERIFIED"), ("short", "SHORT / FAILED"),
                           ("analyses", "ANALYSES"), ("last", "LAST ACTIVITY")):
            cell = ttk.Frame(self.strip, style="Panel.TFrame")
            cell.pack(side="left", padx=(0, 34))
            ttk.Label(cell, text=title, style="PanelMuted.TLabel",
                      font=theme.F.small_bold).pack(
                anchor="w")
            value = ttk.Label(cell, text="—", style="Panel.TLabel",
                              font=theme.F.mono_title_bold)
            value.pack(anchor="w")
            self._stat_labels[key] = value

        # ── filters ──
        filters = ttk.Frame(self)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="Show", style="Muted.TLabel").pack(side="left")
        self.filter_vars = {}
        for kind, label in (("capture", "Captures"), ("autocapture", "SoOp sessions"),
                            ("analysis", "Analyses"), ("planner", "Planner runs")):
            var = tk.BooleanVar(value=True)
            self.filter_vars[kind] = var
            ttk.Checkbutton(filters, text=label, variable=var,
                            command=self.refresh).pack(side="left", padx=(10, 0))

        self.search_var = tk.StringVar()
        entry = ttk.Entry(filters, textvariable=self.search_var, width=24)
        entry.pack(side="right")
        entry.bind("<KeyRelease>", lambda _e: self._fill())
        ttk.Label(filters, text="Filter", style="Muted.TLabel").pack(
            side="right", padx=(0, 6))

        # ── table ──
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("title", "detail")))
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self._reveal_selected())

        for status, color in (("ok", theme.OK), ("warn", theme.AMBER),
                              ("fail", theme.FAIL), ("running", theme.RUNNING),
                              ("unknown", theme.MUTED)):
            self.tree.tag_configure(status, foreground=color)
        self.tree.tag_configure("ok_bold",
                                font=theme.F.mono_small_bold)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Analyse selected capture",
                   command=self._analyse_selected).pack(side="left")
        ttk.Button(actions, text="Reveal in file browser",
                   command=self._reveal_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open master log",
                   command=lambda: system_open.open_paths([paths.MASTER_CSV])).pack(
            side="left", padx=(8, 0))
        self.detail_label = ttk.Label(self, style="Muted.TLabel", wraplength=980,
                                      justify="left",
                                      font=theme.F.mono_small)
        self.detail_label.pack(fill="x", pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

    # ── data ──────────────────────────────────────────────────────────────
    def refresh(self):
        kinds = {k for k, v in self.filter_vars.items() if v.get()}
        self._rows = history.merged_rows(kinds=kinds or None)
        self._fill()
        self._update_strip()
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(AUTO_REFRESH_MS, self.refresh)

    def _fill(self):
        needle = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._visible = []
        for row in self._rows:
            if needle and needle not in (row.title + " " + row.detail +
                                         " " + row.status).lower():
                continue
            self._visible.append(row)
            self.tree.insert("", "end", values=row.as_columns(),
                             tags=(_status_tag(row.status),))

    def _update_strip(self):
        caps = [r for r in self._rows if r.kind == "capture"]
        ok = sum(1 for r in caps if r.status in ("SUCCESS", "OK"))
        bad = sum(1 for r in caps if r.status in ("SHORT", "FAILED", "ERROR"))
        analyses = sum(1 for r in self._rows if r.kind == "analysis")
        stats = history.stats(self._rows)

        self._stat_labels["captures"].configure(text=str(len(caps)))
        self._stat_labels["volume"].configure(
            text=f"{history.data_volume_gb():.1f} GB")
        self._stat_labels["success"].configure(text=str(ok), foreground=theme.OK)
        self._stat_labels["short"].configure(
            text=str(bad), foreground=theme.FAIL if bad else theme.MUTED)
        self._stat_labels["analyses"].configure(text=str(analyses))
        self._stat_labels["last"].configure(text=stats["last_label"] or "never")

    def _show_detail(self):
        row = self._selected()
        if row is None:
            self.detail_label.configure(text="")
            return
        bits = [f"{row.source}", f"status {row.status}"]
        if row.output_dir:
            bits.append(paths.rel(row.output_dir))
        raw = row.raw or {}
        for key in ("Antenna", "Bitmode", "Gain_RX1", "Gain_RX2", "Channels",
                    "Capture_Start_Berlin", "Capture_End_Berlin"):
            if raw.get(key):
                bits.append(f"{key}={raw[key]}")
        for key in ("run_copy", "command", "returncode"):
            if raw.get(key):
                bits.append(f"{key}={raw[key]}")
        self.detail_label.configure(text="   ·   ".join(str(b) for b in bits))

    def _selected(self):
        selection = self.tree.selection()
        if not selection:
            return None
        index = self.tree.index(selection[0])
        return self._visible[index] if index < len(self._visible) else None

    # ── actions ───────────────────────────────────────────────────────────
    def _analyse_selected(self):
        row = self._selected()
        if row is None or row.kind != "capture":
            self.app.error("No capture selected",
                           "Select a capture row — analyses cannot be re-run "
                           "from this table, only captures can be analysed.")
            return
        self.app.analyse_capture(row.title)

    def _reveal_selected(self):
        row = self._selected()
        target = (row.output_dir if row and row.output_dir else paths.DATA_DIR)
        if row and row.kind == "capture":
            target = paths.DATA_DIR
        self.app.reveal(target)


def _status_tag(status):
    color = theme.status_color(status)
    return {theme.OK: "ok", theme.AMBER: "warn", theme.FAIL: "fail",
            theme.RUNNING: "running"}.get(color, "unknown")
