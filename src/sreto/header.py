"""
header.py — the title tile: what time is it, and where is the antenna.

Both questions are permanently on screen because both have silently ruined
captures in this project. UTC and Berlin are shown TOGETHER, side by side,
because every machine-readable artefact in the repo is UTC (plan TSV, sidecars,
filename stamps) while everything a human reads is Berlin — and a single clock
means somebody eventually subtracts two hours from the wrong one.

The coordinates are shown short and copied long. Four decimal places is ~11 m,
which is the right precision to READ; pasting that into geometry.json would move
the receiver by metres and quietly bias every elevation angle. So a click copies
the full stored value and the tile says so.

LOCATION IS NOT POLLED. See location.py: a laptop fix is taken on demand, cached,
and then displayed forever with its timestamp. GNSS costs battery and the
antenna does not move, so continuous updating would be a cost with no benefit.
"""

import tkinter as tk
from tkinter import ttk

from . import location, theme

TICK_MS = 1000


class HeaderTile(ttk.Frame):
    """UTC · local · coordinates, in one bordered tile."""

    def __init__(self, master, app, **kw):
        kw.setdefault("style", "Tile.TFrame")
        kw.setdefault("padding", (12, 7))
        super().__init__(master, **kw)
        self.app = app
        self._fix = None
        self._tick_job = None
        self._flash_job = None
        self._build()
        self.refresh_location()
        self._tick()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        clocks = ttk.Frame(self, style="Tile.TFrame")
        clocks.pack(side="left")

        self.utc_label = ttk.Label(clocks, style="TileValue.TLabel",
                                   font=theme.F.clock_bold)
        self.utc_label.grid(row=0, column=1, sticky="w")
        ttk.Label(clocks, text="UTC", style="TileMuted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6))

        self.local_label = ttk.Label(clocks, style="TileValue.TLabel",
                                     font=theme.F.clock)
        self.local_label.grid(row=1, column=1, sticky="w")
        self.local_name = ttk.Label(clocks, text="LOC", style="TileMuted.TLabel")
        self.local_name.grid(row=1, column=0, sticky="w", padx=(0, 6))

        ttk.Separator(self, orient="vertical").pack(side="left", fill="y",
                                                    padx=12)

        coords = ttk.Frame(self, style="Tile.TFrame")
        coords.pack(side="left")

        self.coord_label = ttk.Label(coords, style="TileLink.TLabel",
                                     font=theme.F.clock_bold, cursor="hand2")
        self.coord_label.pack(anchor="w")
        self.coord_note = ttk.Label(coords, style="TileMuted.TLabel")
        self.coord_note.pack(anchor="w")

        for widget in (self.coord_label, self.coord_note):
            widget.bind("<Button-1>", self._copy_coordinates)

        source = ttk.Frame(self, style="Tile.TFrame")
        source.pack(side="left", padx=(12, 0))
        self.source_var = tk.StringVar(value="Plan / geometry")
        combo = ttk.Combobox(source, textvariable=self.source_var, width=13,
                             state="readonly",
                             values=["Plan / geometry", "This laptop"])
        combo.pack(anchor="w")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._on_source_changed())
        self.locate_btn = ttk.Button(source, text="Update fix", width=13,
                                     command=self.request_laptop_fix)
        self.locate_btn.pack(anchor="w", pady=(3, 0))

    # ── clock ─────────────────────────────────────────────────────────────
    def _tick(self):
        self.utc_label.configure(text=location.utc_now_label())
        self.local_label.configure(text=location.local_now_label())
        self.local_name.configure(text=location.local_tz_label())
        self._tick_job = self.after(TICK_MS, self._tick)

    def stop(self):
        for job in (self._tick_job, self._flash_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:                        # pragma: no cover
                    pass
        self._tick_job = self._flash_job = None

    # ── location ──────────────────────────────────────────────────────────
    def _prefer(self):
        return "laptop" if self.source_var.get() == "This laptop" else "plan"

    def _on_source_changed(self):
        self.refresh_location()
        if self._prefer() == "laptop" and location.load_cached() is None:
            # Selecting the laptop with no cached fix is the one moment asking
            # for one is clearly what the user meant.
            self.request_laptop_fix()

    def refresh_location(self):
        self._fix = location.resolve(self._prefer())
        if self._fix is None:
            self.coord_label.configure(text="no coordinates")
            self.coord_note.configure(
                text="no plan header and no geometry.json",
                foreground=theme.AMBER)
            return

        self.coord_label.configure(text=self._fix.short(), foreground=theme.C1)
        note = f"{location.SOURCE_LABELS.get(self._fix.source, self._fix.source)}"
        if self._fix.unix:
            note += f" · {self._fix.age_label()}"
        self.coord_note.configure(
            text=note,
            foreground=theme.AMBER if self._fix.is_stale() else theme.MUTED)

    def request_laptop_fix(self):
        """Ask macOS for one fix, on a worker thread."""
        self.locate_btn.configure(
            state="disabled", text=f"locating{theme.glyph('ellipsis')}")
        self.app.console.gui_note(
            "requesting one location fix from this machine — it is cached "
            "afterwards, so Location Services can go back off")
        location.request_laptop_fix_async(self._on_fix_thread)

    def _on_fix_thread(self, fix, reason):
        """Called from the location worker. Hands back to Tk, touches nothing."""
        self.app.post_to_ui(lambda: self._on_fix(fix, reason))

    def _on_fix(self, fix, reason):
        self.locate_btn.configure(state="normal", text="Update fix")
        if fix is None:
            self.app.console.gui_error(f"no location fix: {reason}")
            cached = location.load_cached()
            if cached is not None:
                self.app.console.gui_note(
                    f"keeping the last known fix — {cached.age_label()}")
            else:
                self.source_var.set("Plan / geometry")
            self.refresh_location()
            return
        self.app.console.gui_note(
            f"location fixed: {fix.short()} (±{fix.accuracy_m:.0f} m) — cached, "
            f"so this survives Location Services being switched off")
        self.source_var.set("This laptop")
        self.refresh_location()

    # ── clipboard ─────────────────────────────────────────────────────────
    def _copy_coordinates(self, _event=None):
        if self._fix is None:
            return
        text = self._fix.precise()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.coord_note.configure(text=f"copied  {text}", foreground=theme.OK)
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(2200, self._restore_note)

    def _restore_note(self):
        self._flash_job = None
        self.refresh_location()
