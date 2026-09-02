"""
widgets.py — the small reusable pieces every panel is built from.

The one idea worth stating: PlaceholderEntry. capture.sh gives every prompt a
default (`freq=${freq:-433}`), and the requirement is that an untouched form
still produces a valid default capture. So a blank field is not an error state
here — it is "use the script's default", and the field SHOWS that default in
muted text so the user can see what will happen without typing anything.
value() returns "" for an untouched field, which is exactly the empty line the
prompt contract needs.
"""

import math
import tkinter as tk
from tkinter import ttk

from . import theme


class Tooltip:
    """Hover help. Used for the long parameter explanations from MAIN.py."""

    def __init__(self, widget, text, wrap=460, delay=450):
        self.widget = widget
        self.text = text
        self.wrap = wrap
        self.delay = delay
        self._after = None
        self._win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self._win or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._win = tk.Toplevel(self.widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(self._win, background=theme.BORDER, borderwidth=0)
        frame.pack()
        tk.Label(frame, text=self.text, justify="left", wraplength=self.wrap,
                 background="#fffef0", foreground=theme.TEXT,
                 font=theme.F.small,
                 padx=9, pady=6, borderwidth=0).pack(padx=1, pady=1)

    def _hide(self, _e=None):
        self._cancel()
        if self._win is not None:
            self._win.destroy()
            self._win = None


class PlaceholderEntry(ttk.Entry):
    """Entry that shows its default in muted text while empty."""

    def __init__(self, master, placeholder="", width=16, **kw):
        super().__init__(master, width=width, **kw)
        self.placeholder = str(placeholder)
        self._showing = False
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self._show_placeholder()

    def _show_placeholder(self):
        if self.placeholder and not super().get():
            self._showing = True
            self.configure(foreground=theme.MUTED)
            super().insert(0, self.placeholder)

    def _clear_placeholder(self):
        if self._showing:
            self._showing = False
            super().delete(0, "end")
            self.configure(foreground=theme.TEXT)

    def _on_focus_in(self, _e=None):
        self._clear_placeholder()

    def _on_focus_out(self, _e=None):
        if not super().get():
            self._show_placeholder()

    def value(self):
        """User text, or '' when untouched (= 'use the script's default')."""
        return "" if self._showing else super().get().strip()

    def set_value(self, text):
        self._clear_placeholder()
        super().delete(0, "end")
        if text is None or str(text) == "":
            self._show_placeholder()
        else:
            self.configure(foreground=theme.TEXT)
            super().insert(0, str(text))


class Field:
    """One labelled control inside a Form."""

    def __init__(self, key, widget, kind, var=None, unit=""):
        self.key = key
        self.widget = widget
        self.kind = kind
        self.var = var
        self.unit = unit

    def get(self):
        if self.kind == "bool":
            return bool(self.var.get())
        if self.kind == "choice":
            return self.var.get()
        if isinstance(self.widget, PlaceholderEntry):
            return self.widget.value()
        return self.widget.get().strip()

    def set(self, value):
        if self.kind == "bool":
            self.var.set(bool(value))
        elif self.kind == "choice":
            self.var.set("" if value is None else str(value))
        elif isinstance(self.widget, PlaceholderEntry):
            self.widget.set_value(value)
        else:
            self.widget.delete(0, "end")
            if value is not None:
                self.widget.insert(0, str(value))


class Form(ttk.Frame):
    """A two-column grid of labelled fields with hover help."""

    def __init__(self, master, columns=2, **kw):
        super().__init__(master, **kw)
        self.fields = {}
        self._columns = columns
        self._row = 0
        self._col = 0
        for c in range(columns):
            self.columnconfigure(c * 3 + 1, weight=1, minsize=110)
            self.columnconfigure(c * 3 + 2, weight=0)

    def _next_cell(self):
        r, c = self._row, self._col
        self._col += 1
        if self._col >= self._columns:
            self._col = 0
            self._row += 1
        return r, c

    def add(self, key, label, kind="str", default="", placeholder="",
            help_text="", choices=None, width=16, unit=""):
        r, c = self._next_cell()
        base = c * 3

        lbl = ttk.Label(self, text=label, style="TLabel")
        lbl.grid(row=r, column=base, sticky="w", padx=(0, 8), pady=4)

        if kind == "bool":
            var = tk.BooleanVar(value=bool(default))
            w = ttk.Checkbutton(self, variable=var, text="")
            w.grid(row=r, column=base + 1, sticky="w", pady=4)
            field = Field(key, w, kind, var)
        elif kind == "choice":
            var = tk.StringVar(value="" if default is None else str(default))
            w = ttk.Combobox(self, textvariable=var, values=list(choices or []),
                             state="readonly", width=width)
            w.grid(row=r, column=base + 1, sticky="ew", pady=4)
            field = Field(key, w, kind, var)
        else:
            w = PlaceholderEntry(self, placeholder=str(placeholder or ""),
                                 width=width)
            if default not in (None, ""):
                w.set_value(default)
            w.grid(row=r, column=base + 1, sticky="ew", pady=4)
            field = Field(key, w, kind)

        if unit:
            ttk.Label(self, text=unit, style="Muted.TLabel").grid(
                row=r, column=base + 2, sticky="w", padx=(5, 14))

        if help_text:
            Tooltip(lbl, help_text)
            Tooltip(w, help_text)
            lbl.configure(cursor="question_arrow")

        self.fields[key] = field
        return field

    def row_break(self):
        """Finish the current row so the next field starts a fresh one."""
        if self._col != 0:
            self._col = 0
            self._row += 1

    def values(self):
        return {k: f.get() for k, f in self.fields.items()}

    def set_values(self, mapping):
        for k, v in (mapping or {}).items():
            if k in self.fields:
                self.fields[k].set(v)

    def reset(self, defaults):
        for k, f in self.fields.items():
            f.set(defaults.get(k, "" if f.kind != "bool" else False))


class StatusChip(ttk.Frame):
    """Coloured dot + text. The one status indicator used everywhere."""

    def __init__(self, master, text="idle", color=None, background=None, **kw):
        bg = background or theme.BG
        super().__init__(master, **kw)
        self._bg = bg
        self.canvas = tk.Canvas(self, width=11, height=11, highlightthickness=0,
                                background=bg, borderwidth=0)
        self.canvas.pack(side="left", pady=1)
        self._dot = self.canvas.create_oval(2, 2, 10, 10,
                                            fill=color or theme.IDLE, outline="")
        self.label = tk.Label(self, text=text, background=bg,
                              foreground=theme.TEXT, font=theme.F.small)
        self.label.pack(side="left", padx=(6, 0))

    def set(self, text, color=None):
        self.label.configure(text=text)
        if color:
            self.canvas.itemconfigure(self._dot, fill=color)


class Card(ttk.Frame):
    """A titled white panel — the basic layout unit.

    `collapsible=True` puts a disclosure chevron on the title and makes the
    whole header row a click target. Use it for cards that are REFERENCE
    material — a table restating settings that came from somewhere else, a
    long explanation — rather than for the controls a panel exists to offer.
    A collapsed card still tells you it is there and how to open it; hiding
    the fact would just be a shorter way of losing it.

    `expanded=False` starts it closed, which is the right default for a card
    that merely restates values the user already set elsewhere.
    """

    CHEVRON_OPEN = "▾"
    CHEVRON_SHUT = "▸"

    def __init__(self, master, title, subtitle="", collapsible=False,
                 expanded=True, on_toggle=None, **kw):
        super().__init__(master, style="Panel.TFrame", padding=(14, 10, 14, 12), **kw)
        head = ttk.Frame(self, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 8))
        self.head = head
        self._collapsible = bool(collapsible)
        self._expanded = bool(expanded) or not self._collapsible
        self._on_toggle = on_toggle
        self._title_text = title.upper()

        self._chevron = None
        if self._collapsible:
            self._chevron = ttk.Label(
                head, style="Panel.TLabel", font=theme.F.small_bold,
                foreground=theme.MUTED, width=2,
                text=self.CHEVRON_OPEN if self._expanded else self.CHEVRON_SHUT)
            self._chevron.pack(side="left")

        self._title = ttk.Label(head, text=self._title_text, style="Panel.TLabel",
                                font=theme.F.small_bold,
                                foreground=theme.MUTED)
        self._title.pack(side="left")

        self._subtitle = None
        if subtitle:
            self._subtitle = ttk.Label(head, text=subtitle,
                                       style="PanelMuted.TLabel")
            self._subtitle.pack(side="left", padx=(10, 0))

        self.body = ttk.Frame(self, style="Panel.TFrame")
        if self._expanded:
            self.body.pack(fill="both", expand=True)

        if self._collapsible:
            # The whole header is the hit target, not just the chevron — a
            # 12 px glyph is a poor click target and the title is the thing
            # the eye is already on. `add="+"` throughout so a caller that
            # binds its own handler to the head keeps it.
            for w in (head, self._chevron, self._title, self._subtitle):
                if w is not None:
                    w.bind("<Button-1>", self._on_click, add="+")
                    try:
                        w.configure(cursor="hand2")
                    except tk.TclError:                     # pragma: no cover
                        pass

    # ── collapsing ────────────────────────────────────────────────────────
    def _on_click(self, _event=None):
        self.toggle()
        return "break"

    def toggle(self):
        self.set_expanded(not self._expanded)

    def expanded(self):
        return self._expanded

    def set_expanded(self, value):
        """Show or hide the body. No-op on a card that is not collapsible."""
        value = bool(value)
        if not self._collapsible or value == self._expanded:
            return
        self._expanded = value
        if value:
            self.body.pack(fill="both", expand=True)
        else:
            self.body.pack_forget()
        if self._chevron is not None:
            self._chevron.configure(
                text=self.CHEVRON_OPEN if value else self.CHEVRON_SHUT)
        if callable(self._on_toggle):
            self._on_toggle(value)

    def set_subtitle(self, text):
        """Retitle the subtitle line — a collapsed card's only status surface.

        A card the user has closed still needs to be able to say that what is
        inside it changed, otherwise collapsing it means not being told.
        """
        if self._subtitle is None:
            self._subtitle = ttk.Label(self.head, style="PanelMuted.TLabel")
            self._subtitle.pack(side="left", padx=(10, 0))
            if self._collapsible:
                self._subtitle.bind("<Button-1>", self._on_click, add="+")
        self._subtitle.configure(text=text)


class SettingsTable(ttk.Frame):
    """label · value · where-it-came-from, for radio_settings.effective().

    The origin column is the whole point of the widget: a bandwidth with no
    provenance is what made "why did that capture run at 4 MHz?" unanswerable.
    Colour encodes the source — your input, the plan, or a constant inside
    soop_capture.sh that the GUI cannot change.
    """

    KIND_COLORS = {
        "shared": theme.C1,          # you set this, on the Capture tab
        "plan": theme.C3,            # per satellite, from the plan TSV
        "script": theme.MUTED,       # fixed inside soop_capture.sh
        "conflict": theme.AMBER,     # you set it, but it cannot be forwarded
    }

    def __init__(self, master, columns=2, **kw):
        kw.setdefault("style", "Panel.TFrame")
        super().__init__(master, **kw)
        self._columns = columns
        self._rows = []
        for c in range(columns):
            self.columnconfigure(c * 3 + 1, weight=0)
            self.columnconfigure(c * 3 + 2, weight=1, minsize=140)

    def set_rows(self, settings):
        for widget in self._rows:
            widget.destroy()
        self._rows = []

        for i, s in enumerate(settings):
            row, col = divmod(i, self._columns)
            base = col * 3
            color = self.KIND_COLORS.get(s.kind, theme.MUTED)

            label = ttk.Label(self, text=s.label, style="PanelMuted.TLabel")
            label.grid(row=row, column=base, sticky="w", padx=(0, 8), pady=2)

            value = ttk.Label(self, text=s.display(), style="Panel.TLabel",
                              foreground=color, font=theme.F.mono_small_bold)
            value.grid(row=row, column=base + 1, sticky="w", padx=(0, 8), pady=2)

            origin = ttk.Label(self, text=s.origin, style="PanelMuted.TLabel",
                               font=theme.F.mono_small)
            origin.grid(row=row, column=base + 2, sticky="w", padx=(0, 18), pady=2)

            self._rows.extend((label, value, origin))
            if s.kind == "conflict":
                Tooltip(value, s.origin)
                Tooltip(origin, s.origin)


class ScrollFrame(ttk.Frame):
    """Vertically scrollable container — parameter panels outgrow small windows."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, background=theme.BG, highlightthickness=0,
                                borderwidth=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        # Tk delivers wheel events differently on macOS (<MouseWheel>, small
        # deltas) and X11 (Button-4/5); bind both so scrolling just works.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._on_wheel, add="+")

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_wheel(self, event):
        if not self._pointer_inside():
            return
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")
        else:
            step = -1 * int(event.delta)
            if abs(step) >= 120:            # Windows/X11 report multiples of 120
                step //= 120
            self.canvas.yview_scroll(step, "units")

    def _pointer_inside(self):
        try:
            x, y = self.canvas.winfo_pointerxy()
            widget = self.canvas.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return False
        while widget is not None:
            if widget is self.canvas or widget is self.inner:
                return True
            widget = getattr(widget, "master", None)
        return False


class SkyCompass(tk.Canvas):
    """Click-a-wedge compass for choosing which sky sectors you can see.

    Drawn in compass convention (N up, E right, clockwise), which is the
    convention orbits.propagate_satellite returns azimuth in — so what you
    click is literally where you are pointing.

    Tk's arc angles run counterclockwise from East, so a sector centred on
    compass bearing `c` starts at 90 - (c + 22.5) and extends 45°.
    """

    def __init__(self, master, size=168, on_change=None, background=None, **kw):
        bg = background or theme.PANEL
        self.base_size = size
        self.size = int(round(size * theme.scale()))
        super().__init__(master, width=self.size, height=self.size,
                         highlightthickness=0, background=bg, borderwidth=0, **kw)
        from . import skyview
        self._sv = skyview
        self.on_change = on_change
        self.open_sectors = set(skyview.SECTOR_NAMES)
        self._items = {}
        self.bind("<Button-1>", self._on_click)
        # Canvas geometry is in pixels, so it cannot ride on a font resize —
        # the compass has to be told when the GUI is zoomed.
        theme.on_scale_change(self._on_scale)
        self.redraw()

    def _on_scale(self, scale):
        if not self.winfo_exists():
            return
        self.size = int(round(self.base_size * scale))
        self.configure(width=self.size, height=self.size)
        self.redraw()

    def set_sectors(self, sectors):
        self.open_sectors = set(sectors)
        self.redraw()

    def redraw(self):
        self.delete("all")
        s = self.size
        pad = 20
        x0, y0, x1, y1 = pad, pad, s - pad, s - pad
        cx = cy = s / 2.0

        for name in self._sv.SECTOR_NAMES:
            center = self._sv.SECTOR_CENTER[name]
            start = 90.0 - (center + self._sv.SECTOR_WIDTH / 2.0)
            is_open = name in self.open_sectors
            item = self.create_arc(
                x0, y0, x1, y1, start=start, extent=self._sv.SECTOR_WIDTH,
                style="pieslice",
                fill=theme.C1 if is_open else theme.SURFACE_ALT,
                outline=theme.PANEL, width=2)
            self._items[item] = name

        # Elevation rings — a reminder that the mask has a floor as well.
        for frac in (0.34, 0.67):
            r = (x1 - x0) / 2.0 * frac
            self.create_oval(cx - r, cy - r, cx + r, cy + r,
                             outline=theme.PANEL, width=1)

        for name, dx, dy, anchor in (("N", 0, -1, "s"), ("E", 1, 0, "w"),
                                     ("S", 0, 1, "n"), ("W", -1, 0, "e")):
            r = (x1 - x0) / 2.0 + 4
            self.create_text(cx + dx * r, cy + dy * r, text=name, anchor=anchor,
                             fill=theme.MUTED, font=theme.F.small_bold)

    def _on_click(self, event):
        cx = cy = self.size / 2.0
        dx, dy = event.x - cx, event.y - cy
        r = math.hypot(dx, dy)
        if r > self.size / 2.0 - 16 or r < 4:
            return
        # North is up (-y), east is +x, clockwise — compass convention.
        az = math.degrees(math.atan2(dx, -dy)) % 360.0
        name = self._sv.sector_of(az)
        if name in self.open_sectors:
            self.open_sectors.discard(name)
        else:
            self.open_sectors.add(name)
        self.redraw()
        if self.on_change:
            self.on_change(frozenset(self.open_sectors))


def separator(master, pady=8):
    s = ttk.Separator(master, orient="horizontal")
    s.pack(fill="x", pady=pady)
    return s
