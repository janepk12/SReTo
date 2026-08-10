"""
theme.py — the pipeline's plotting palette, lifted into the GUI.

The colours below are the ones the analysis figures already use, so a run's
window and its output PNGs read as one instrument:

    waterfalls.py:48-52      BG PANEL BORDER TEXT MUTED, C1 C2 C3 C_EXTRACT
    iq_dashboard.py:64-65    the same five neutrals + C_DIFF C_PH C_XC C_PEAK
    iq_dashboard.py:59       font.family = monospace

They are duplicated here rather than imported because importing waterfalls
pulls in matplotlib + numpy (~0.9 s and ~120 MB) into a process whose whole
point is to stay out of the way. tests/test_theme_matches_plots.py re-parses
both plotting modules and fails if a value ever drifts apart.

CHANNEL IDENTITY is fixed by the pipeline and must stay fixed here:
    C1 (blue)   = rx1 = "RE" direct/reference antenna
    C2 (orange) = rx2 = "GR" ground-reflected antenna

UI SCALE. Every font is a NAMED Tk font (class F), not a (family, size) tuple.
A named font is shared by reference: reconfiguring its size re-lays out every
widget already using it, with no rebuild and no lost form state. That is what
makes Cmd +/- work on a window full of live widgets — see set_scale().
"""

import tkinter.font as tkfont
from tkinter import ttk

# ── neutrals (waterfalls.py:48 / iq_dashboard.py:64) ───────────────────────
BG = "#f8f9fa"
PANEL = "#ffffff"
BORDER = "#dee2e6"
TEXT = "#212529"
MUTED = "#6c757d"

# ── data colours (waterfalls.py:49-52 / iq_dashboard.py:65) ────────────────
C1 = "#1f77b4"          # rx1 / direct (RE)
C2 = "#ff7f0e"          # rx2 / reflected (GR)
C3 = "#9467bd"
C_EXTRACT = "#d62728"   # extraction-window marker
C_DIFF = "#d62728"
C_PH = "#17becf"        # phase
C_XC = "#9467bd"        # cross-correlation
C_PEAK = "#e377c2"      # correlation peak

# ── semantic status colours ───────────────────────────────────────────────
# tab10 green completes the plotting family. AMBER is deliberately NOT C2:
# orange already means "rx2 / reflected channel" everywhere in this project,
# and a warning chip in that colour would read as a channel label.
OK = "#2ca02c"
AMBER = "#e0a800"
FAIL = C_EXTRACT
RUNNING = C1
IDLE = MUTED

# ── constellation identity ────────────────────────────────────────────────
# One colour per satellite family, used by the skymap, the availability table's
# row swatches and the legend, so a target keeps the same colour everywhere it
# appears. Drawn from the same tab10/tab20 family as the plotting palette;
# C1 and C2 are deliberately ABSENT — blue and orange mean rx1/rx2 in this
# project and a satellite dot in either colour would read as a channel.
CONSTELLATION_COLORS = {
    "IRIDIUM":    "#8c564b",
    "GLOBALSTAR": "#2ca02c",
    "ORBCOMM":    "#bcbd22",
    "GPS":        "#9467bd",
    "GALILEO":    "#17becf",
    "GLONASS":    "#d62728",
    "BEIDOU":     "#e377c2",
    "STARLINK":   "#7f7f7f",
    "ONEWEB":     "#aec7e8",
    "NOAA":       "#ff9896",
    "METOP":      "#c5b0d5",
    "SAOCOM":     "#98df8a",
    "SENTINEL":   "#c49c94",
    "GEO":        "#f7b6d2",
    "OTHER":      MUTED,
}

# Terminal rendering of console.py's ANSI set (console.py:31-33). Mapped onto
# this palette so the embedded console and the figures agree on "cyan".
ANSI_COLORS = {
    "green": OK,
    "yellow": AMBER,
    "red": FAIL,
    "cyan": "#17a2b8",
    "magenta": C_PEAK,
    "blue": C1,
    "white": TEXT,
    "black": TEXT,
}

# Slightly warmer/darker surfaces for chrome that must sit behind PANEL.
SURFACE = "#eef0f2"
SURFACE_ALT = "#e4e7ea"
SELECT_BG = "#d6e4f0"
CONSOLE_BG = "#1b1f23"      # the console stays dark: it is a terminal
CONSOLE_FG = "#e6e6e6"
CONSOLE_SEL = "#2f4356"

# Same hues as above, lifted in luminance so they stay legible on CONSOLE_BG.
# console.py's palette is designed for a terminal, so this is where the ANSI
# codes it emits actually get rendered.
CONSOLE_ANSI = {
    "green": "#5fd18a",
    "yellow": "#f2c14e",
    "red": "#ff6b6c",
    "cyan": "#4fc3d9",
    "magenta": "#f08bd0",
    "blue": "#6ab0e8",
    "white": "#f2f4f6",
    "black": "#4a5158",
}

# Preferred first, degrading to families an X server has even with no
# fontconfig at all. The tail matters: a Tk built without Xft can only see the
# ~60 X11 core families, where the modern names above do not exist but
# 'helvetica' and 'courier' (URW Nimbus, scalable Type 1) always do.
_MONO_CANDIDATES = ("Menlo", "DejaVu Sans Mono", "Liberation Mono",
                    "Ubuntu Mono", "Noto Sans Mono", "Consolas", "Courier New",
                    "Nimbus Mono PS", "Nimbus Mono L", "Courier 10 Pitch",
                    "Courier")
_UI_CANDIDATES = ("SF Pro Text", "Helvetica Neue", "Inter", "Cantarell",
                  "DejaVu Sans", "Liberation Sans", "Noto Sans", "Ubuntu",
                  "Segoe UI", "Nimbus Sans", "Nimbus Sans L", "Helvetica")


def _first_available(root, candidates, named_fallback):
    """First candidate family Tk really has, else the family of a NAMED font.

    Both halves of this were wrong, and together they rendered the entire Linux
    GUI in the X11 'fixed' bitmap font — a chunky terminal face where macOS got
    SF Pro, which is the whole of "it looks nothing like the Mac version".

    1. X11 reports families lower-cased ('dejavu sans'), so a case-sensitive
       membership test against a title-cased list can never match on Linux.
       Matching folds case and returns Tk's own spelling, which is what Tk
       wants back.

    2. 'TkDefaultFont' is a NAMED FONT, not a family. Passing it as `family=`
       asks for a family that does not exist, and Tk answers with 'fixed'
       rather than an error — so the fallback that was supposed to be the safe
       one was the single worst outcome available. The right fallback is the
       family that named font actually resolves to, which is by definition
       present and is the desktop's own UI font.
    """
    families = {name.lower(): name for name in tkfont.families(root)}
    for name in candidates:
        hit = families.get(name.lower())
        if hit:
            return hit
    try:
        # Font(name=..., exists=True) rather than nametofont(..., root=...):
        # the root kwarg on nametofont is newer than the Python this supports.
        return tkfont.Font(root=root, name=named_fallback,
                           exists=True).actual("family")
    except Exception:                                      # noqa: BLE001
        return "helvetica"        # an X core alias; present wherever X11 is


# ── glyphs that not every Tk can draw ─────────────────────────────────────
# Tk gets both Unicode coverage and per-glyph fallback across the installed
# font set from Xft. Without Xft it is limited to the X11 core fonts, whose
# scalable families are ISO8859-1 only — so anything past Latin-1 is drawn as
# a hex box. Latin-1 itself is safe everywhere, which is why '·', '°', '±',
# 'µ' and even '—' are used freely in this GUI and are NOT listed here.
#
# Each entry is (preferred, ASCII stand-in). Call theme.glyph('arrow') rather
# than writing the character, and the degraded Tk gets '->' instead of a box.
_GLYPHS = {
    "arrow":    ("→", "->"),      # →  "this tab runs THIS command"
    "ellipsis": ("…", "..."),     # …  "opens a dialog" / "in progress"
    "enter":    ("⏎", "RET"),     # ⏎  the lock screen's Enter key cap
    "info":     ("ⓘ", "(i)"),     # ⓘ  the About affordance in the status bar
}

# Whether Tk can render past Latin-1. Assumed yes until apply_theme() measures
# it, so a glyph() call made before the theme exists still gets the nice form.
unicode_text = True


def _unicode_text_available(root):
    """True when this Tk can draw characters beyond Latin-1.

    Detected by CASE, not by measuring: a missing glyph still returns a width,
    so tkfont.measure cannot tell "drawn" from "drawn as a box". X11 core
    family names are lower-case by XLFD convention ('nimbus sans l'), while
    fontconfig reports them cased ('DejaVu Sans', 'Noto Sans'). A Tk that
    offers not one mixed-case family has no Xft, and therefore no Unicode.

    The usual way to end up here is a venv built on conda's bundled Tk, which
    is compiled without Xft; the same machine's system python3 is normally
    fine. prechecks.check_text_rendering() says so, with the fix.
    """
    try:
        return any(name != name.lower() for name in tkfont.families(root))
    except Exception:                                      # noqa: BLE001
        return True           # never let a probe failure downgrade the GUI


def glyph(key):
    """The nice character, or its ASCII stand-in on a Tk that cannot draw it."""
    preferred, plain = _GLYPHS[key]
    return preferred if unicode_text else plain


# True once probe_fonts() has asked a real Tk. Before that, Fonts holds
# placeholders and `unicode_text` is an assumption, and the pre-checks say so
# rather than reporting a guess as a measurement.
fonts_resolved = False

# Named here rather than in prechecks so the GUI check and `sreto --check`
# cannot drift apart — they are the same sentence about the same condition.
XFT_HINT = (
    "this Tk has no Xft, so it can only use the X11 core fonts: text is drawn "
    "with a bitmap or Latin-1 face and every character past Latin-1 would be a "
    "box. Almost always a venv built on conda's bundled Tk — rebuild it on the "
    "system python (python3 -m venv .venv, after apt install python3-tk) and "
    "the GUI gets the desktop's own antialiased fonts.")


def probe_fonts(root):
    """Resolve the font families and Unicode capability against `root`.

    Split out of apply_theme so `sreto --check` can report the same answer
    from a throwaway hidden root, with no styling and no window.
    """
    global unicode_text, fonts_resolved
    Fonts.mono = _first_available(root, _MONO_CANDIDATES, "TkFixedFont")
    Fonts.ui = _first_available(root, _UI_CANDIDATES, "TkDefaultFont")
    unicode_text = _unicode_text_available(root)
    fonts_resolved = True
    return unicode_text


def rendering_summary():
    """[(label, value)] — what Tk will actually draw with.

    Reads only what probe_fonts() already resolved on the Tk thread, so the
    pre-checks worker can call it without touching the Tcl interpreter.
    """
    return [
        ("ui font", Fonts.ui),
        ("mono font", Fonts.mono),
        ("unicode", "yes" if unicode_text else "NO — X11 core fonts only"),
    ]


class Fonts:
    """Resolved font families/sizes — populated by apply_theme().

    Kept as plain values for anything that needs a family name. The widgets
    themselves use the NAMED fonts in F, which is what makes zooming work.

    The defaults are real FAMILIES, not the 'TkDefaultFont'/'TkFixedFont' named
    fonts they used to be: a named font passed as a family silently resolves to
    the 'fixed' bitmap face — see _first_available.
    """
    mono = "courier"
    ui = "helvetica"
    size = 11
    size_small = 10
    size_mono = 11
    size_title = 13


# Point sizes at 100 %. Everything scales from here.
BASE_SIZES = {
    "ui": 11, "small": 10, "mono": 11, "mono_small": 10,
    "title": 13, "submenu": 20, "app_title": 15, "clock": 13,
    "display": 17, "lock_title": 26, "lock_logo": 40, "credits_title": 20,
}

# The zoom stops Cmd +/- steps through. 1.0 is the design size.
SCALE_STEPS = (0.75, 0.85, 1.00, 1.15, 1.30, 1.50, 1.75, 2.00)
MIN_SCALE, MAX_SCALE = SCALE_STEPS[0], SCALE_STEPS[-1]

_scale = 1.0
_root = None
_style = None
_scale_listeners = []


class F:
    """Named Tk fonts, created by apply_theme() and resized by set_scale().

    Assigned as `font=theme.F.small` rather than `font=(family, size)`: a
    tuple is copied into the widget at construction time and is frozen there,
    while a named font stays live, so one reconfigure rescales the whole GUI.
    """
    ui = ui_bold = None
    small = small_bold = small_link = None
    mono = mono_bold = mono_italic = None
    mono_small = mono_small_bold = None
    title = submenu = app_title = None
    mono_title_bold = mono_ui_bold = None
    clock = clock_bold = None
    display = None
    lock_title = lock_logo = credits_title = None


_FONT_SPECS = {
    # attribute        family  size key      weight/slant
    "ui":              ("ui",   "ui",         {}),
    "ui_bold":         ("ui",   "ui",         {"weight": "bold"}),
    "small":           ("ui",   "small",      {}),
    "small_bold":      ("ui",   "small",      {"weight": "bold"}),
    "small_link":      ("ui",   "small",      {"underline": True}),
    "mono":            ("mono", "mono",       {}),
    "mono_bold":       ("mono", "mono",       {"weight": "bold"}),
    "mono_italic":     ("mono", "mono",       {"slant": "italic"}),
    "mono_small":      ("mono", "mono_small", {}),
    "mono_small_bold": ("mono", "mono_small", {"weight": "bold"}),
    "title":           ("ui",   "title",      {"weight": "bold"}),
    "submenu":         ("ui",   "submenu",    {"weight": "bold"}),
    "app_title":       ("ui",   "app_title",  {"weight": "bold"}),
    "mono_title_bold": ("mono", "title",      {"weight": "bold"}),
    "mono_ui_bold":    ("mono", "ui",         {"weight": "bold"}),
    "clock":           ("mono", "clock",      {}),
    "clock_bold":      ("mono", "clock",      {"weight": "bold"}),
    "display":         ("ui",   "display",    {"weight": "bold"}),
    "lock_title":      ("ui",   "lock_title", {"weight": "bold"}),
    "lock_logo":       ("ui",   "lock_logo",  {"weight": "bold"}),
    "credits_title":   ("ui",   "credits_title", {"weight": "bold"}),
}


def scaled(key, scale=None):
    """Point size of a role at the current (or given) scale. Never below 7."""
    return max(7, int(round(BASE_SIZES[key] * (scale if scale is not None
                                               else _scale))))


def scale():
    return _scale


def _build_fonts(root):
    for attr, (family_key, size_key, options) in _FONT_SPECS.items():
        family = Fonts.mono if family_key == "mono" else Fonts.ui
        setattr(F, attr, tkfont.Font(root=root, name=f"Sdr_{attr}", exists=False,
                                     family=family, size=scaled(size_key),
                                     **options))


def _resize_fonts():
    for attr, (_family, size_key, _options) in _FONT_SPECS.items():
        font = getattr(F, attr)
        if font is not None:
            font.configure(size=scaled(size_key))


def snap_scale(value, direction=0):
    """Nearest zoom stop to `value`, or the next one up/down when direction±1."""
    nearest = min(range(len(SCALE_STEPS)),
                  key=lambda i: abs(SCALE_STEPS[i] - value))
    index = max(0, min(len(SCALE_STEPS) - 1, nearest + direction))
    return SCALE_STEPS[index]


def set_scale(value, notify=True):
    """Resize the whole GUI. Returns the scale actually applied.

    Only fonts and the geometry that must track them (row heights, paddings)
    change; no widget is destroyed, so a half-filled form survives a zoom.
    """
    global _scale
    new = max(MIN_SCALE, min(MAX_SCALE, float(value)))
    if _root is None:
        _scale = new
        return _scale
    _scale = new
    _resize_fonts()
    if _style is not None:
        _configure_metrics(_style)
    if notify:
        for callback in list(_scale_listeners):
            try:
                callback(_scale)
            except Exception:                              # noqa: BLE001
                pass          # a listener must never break the zoom itself
    return _scale


def zoom_in():
    return set_scale(snap_scale(_scale, +1))


def zoom_out():
    return set_scale(snap_scale(_scale, -1))


def zoom_reset():
    return set_scale(1.0)


def on_scale_change(callback):
    """Register a callback(scale) for widgets that draw their own geometry."""
    _scale_listeners.append(callback)
    return callback


def _configure_metrics(style):
    """The handful of pixel dimensions that must follow the font size."""
    s = _scale
    style.configure("TButton", padding=(int(10 * s), int(5 * s)))
    style.configure("Accent.TButton", padding=(int(10 * s), int(5 * s)))
    style.configure("Danger.TButton", padding=(int(10 * s), int(5 * s)))
    style.configure("TNotebook.Tab", padding=(int(16 * s), int(7 * s)))
    style.configure("Treeview", rowheight=int(round(22 * s)))
    style.configure("TEntry", padding=int(4 * s))
    style.configure("TCombobox", padding=int(3 * s))


def apply_theme(root, scale=1.0):
    """Style every ttk widget class the GUI uses. Returns the ttk.Style."""
    global _root, _style, _scale
    probe_fonts(root)

    _root = root
    _scale = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
    _build_fonts(root)

    style = ttk.Style(root)
    _style = style
    # 'clam' is the only built-in theme that honours background/fieldbackground
    # on BOTH macOS and Linux — the native 'aqua' theme ignores most colour
    # options, which would leave the GUI grey next to its own figures.
    if "clam" in style.theme_names():
        style.theme_use("clam")

    root.configure(background=BG)

    base = F.ui
    small = F.small
    mono = F.mono

    style.configure(".", background=BG, foreground=TEXT, font=base,
                    bordercolor=BORDER, focuscolor=C1)

    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL, relief="flat")
    style.configure("Surface.TFrame", background=SURFACE)
    # The header tile: a raised slab so the clock and coordinates read as one
    # object rather than as loose labels in the title bar.
    style.configure("Tile.TFrame", background=PANEL, relief="solid",
                    borderwidth=1, bordercolor=BORDER)
    style.configure("Tile.TLabel", background=PANEL, foreground=TEXT)
    style.configure("TileValue.TLabel", background=PANEL, foreground=TEXT)
    style.configure("TileMuted.TLabel", background=PANEL, foreground=MUTED,
                    font=F.small)
    style.configure("TileLink.TLabel", background=PANEL, foreground=C1)

    style.configure("TLabel", background=BG, foreground=TEXT, font=base)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=small)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=small)
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=F.title)
    style.configure("Mono.TLabel", background=BG, foreground=TEXT, font=mono)
    style.configure("Ch1.TLabel", background=BG, foreground=C1, font=F.ui_bold)
    style.configure("Ch2.TLabel", background=BG, foreground=C2, font=F.ui_bold)

    # ── the panel headings (Capture, Analysis, SoOp availability …) ──
    # Deliberately much larger than body text: each tab is a different tool
    # with a different failure mode, and the heading is the one thing that says
    # which one you are about to run.
    style.configure("Submenu.TLabel", background=BG, foreground=TEXT,
                    font=F.submenu)
    style.configure("SubmenuAccent.TLabel", background=BG, foreground=C1,
                    font=F.submenu)
    style.configure("AppTitle.TLabel", background=BG, foreground=TEXT,
                    font=F.app_title)

    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                    font=F.small_bold)

    style.configure("TButton", background=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, relief="flat", padding=(10, 5), font=base)
    style.map("TButton",
              background=[("pressed", SURFACE_ALT), ("active", SELECT_BG),
                          ("disabled", SURFACE)],
              foreground=[("disabled", BORDER)])

    style.configure("Accent.TButton", background=C1, foreground="#ffffff",
                    bordercolor=C1, font=F.ui_bold)
    style.map("Accent.TButton",
              background=[("pressed", "#17608f"), ("active", "#2b8fd4"),
                          ("disabled", BORDER)],
              foreground=[("disabled", MUTED)])

    style.configure("Danger.TButton", background=FAIL, foreground="#ffffff",
                    bordercolor=FAIL, font=F.ui_bold)
    style.map("Danger.TButton",
              background=[("pressed", "#a01f20"), ("active", "#e04b4c"),
                          ("disabled", BORDER)],
              foreground=[("disabled", MUTED)])

    style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT,
                    bordercolor=BORDER, insertcolor=TEXT, padding=4)
    style.map("TEntry", bordercolor=[("focus", C1)])

    style.configure("TCombobox", fieldbackground=PANEL, background=SURFACE,
                    foreground=TEXT, bordercolor=BORDER, arrowcolor=MUTED, padding=3)
    style.map("TCombobox",
              fieldbackground=[("readonly", PANEL)],
              bordercolor=[("focus", C1)])
    # The dropdown list is a Tk (not ttk) widget — styled through the option DB.
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", base)

    style.configure("TCheckbutton", background=BG, foreground=TEXT, font=base,
                    indicatorcolor=PANEL, bordercolor=BORDER, focuscolor=BG)
    style.map("TCheckbutton",
              indicatorcolor=[("selected", C1), ("pressed", SELECT_BG)],
              background=[("active", BG)])
    style.configure("Panel.TCheckbutton", background=PANEL, foreground=TEXT)
    style.map("Panel.TCheckbutton", background=[("active", PANEL)],
              indicatorcolor=[("selected", C1)])

    style.configure("TRadiobutton", background=BG, foreground=TEXT, font=base,
                    indicatorcolor=PANEL, bordercolor=BORDER)
    style.map("TRadiobutton", indicatorcolor=[("selected", C1)],
              background=[("active", BG)])

    style.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                    padding=(16, 7), bordercolor=BORDER, font=base)
    style.map("TNotebook.Tab",
              background=[("selected", PANEL), ("active", SELECT_BG)],
              foreground=[("selected", TEXT)],
              font=[("selected", F.ui_bold)])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=TEXT, bordercolor=BORDER, rowheight=22,
                    font=F.mono_small)
    style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED,
                    relief="flat", font=F.small_bold, padding=(4, 4))
    style.map("Treeview.Heading", background=[("active", SELECT_BG)])
    style.map("Treeview", background=[("selected", SELECT_BG)],
              foreground=[("selected", TEXT)])

    style.configure("TScrollbar", background=SURFACE, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED, relief="flat")
    style.map("TScrollbar", background=[("active", SURFACE_ALT)])

    style.configure("TSeparator", background=BORDER)
    style.configure("TProgressbar", background=C1, troughcolor=SURFACE,
                    bordercolor=BORDER, lightcolor=C1, darkcolor=C1)
    style.configure("TPanedwindow", background=BG)
    style.configure("Sash", sashthickness=6, gripcount=0)

    _configure_metrics(style)
    return style


# Matched against the UPPERCASED satellite name, first hit wins. Order matters:
# 'NAVSTAR' and 'GPS' are the same family, and 'SENTINEL' must not be caught by
# a looser rule later. Names come from CelesTrak via soop_planner.py.
_CONSTELLATION_PATTERNS = (
    ("IRIDIUM", ("IRIDIUM",)),
    ("GLOBALSTAR", ("GLOBALSTAR",)),
    ("ORBCOMM", ("ORBCOMM",)),
    ("GPS", ("GPS", "NAVSTAR")),
    ("GALILEO", ("GALILEO", "GSAT")),
    ("GLONASS", ("GLONASS", "COSMOS 2")),
    ("BEIDOU", ("BEIDOU", "COMPASS")),
    ("STARLINK", ("STARLINK",)),
    ("ONEWEB", ("ONEWEB", "ONE WEB")),
    ("NOAA", ("NOAA",)),
    ("METOP", ("METOP", "MET OP")),
    ("SAOCOM", ("SAOCOM",)),
    ("SENTINEL", ("SENTINEL",)),
)


def constellation_of(name, geo=False):
    """Family key for a satellite name — 'IRIDIUM', 'GPS', 'GEO', 'OTHER'.

    Purely a display grouping. It never feeds a capture decision, so an
    unrecognised name falling through to 'OTHER' costs nothing but a grey dot.
    """
    upper = str(name or "").upper()
    for key, needles in _CONSTELLATION_PATTERNS:
        if any(needle in upper for needle in needles):
            return key
    return "GEO" if geo else "OTHER"


def constellation_color(name, geo=False):
    """The colour this satellite keeps in the skymap, table and legend."""
    return CONSTELLATION_COLORS.get(constellation_of(name, geo),
                                    CONSTELLATION_COLORS["OTHER"])


def status_color(status):
    """Map a run/verdict word onto the palette. Unknown -> MUTED."""
    s = str(status or "").strip().upper()
    if s in ("SUCCESS", "OK", "DONE", "PASS", "COMPLETE", "VERIFIED"):
        return OK
    if s in ("RUNNING", "IN_PROGRESS", "CAPTURING", "STARTED"):
        return RUNNING
    if s in ("SHORT", "WARN", "WARNING", "PARTIAL", "STALE", "SKIPPED"):
        return AMBER
    if s in ("FAILED", "FAIL", "ERROR", "KILLED", "STOPPED", "ABORTED", "MISSING"):
        return FAIL
    return MUTED
