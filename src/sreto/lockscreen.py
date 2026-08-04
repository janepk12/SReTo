"""
lockscreen.py — the app opens locked, and stays locked until you say so.

The point is consent, not security: this window can start a capture that drives
real hardware and fills a disk, and an unattended or accidentally-clicked
window should not be able to. So the GUI builds itself fully, then covers
itself and refuses to run anything until it is deliberately unlocked.

App.run_job() checks the lock too. The overlay alone would be theatre — a panel
method called from a script would sail straight past it — so the refusal lives
where jobs actually start.

ABOUT THE BLUR
The backdrop is a real Gaussian blur, not a flat scrim. There is no portable
way to screenshot Tk's own widgets, so instead the widget TREE is walked for
its live geometry and background colours, those rectangles are painted into a
Pillow image, and THAT is blurred. The result is a genuine blurred likeness of
the actual layout underneath — it changes when the layout does — rather than a
stock texture. Without Pillow it degrades to a plain scrim and says nothing
about it, because a missing blur is cosmetic.

When assets/loading_screen.* exists it becomes the base layer of that backdrop,
cover-fitted and blurred with everything else, with the widget likeness left
faintly on top. Cover-fit rather than stretched: a squashed photograph looks
like a bug, and the crop costs nothing.

UNLOCK: press L, then Enter.
"""

import tkinter as tk
from tkinter import ttk

from . import branding, theme

UNLOCK_KEY = "l"            # the letter that arms the unlock
BLUR_RADIUS = 17
BACKDROP_SCALE = 0.5        # render at half size, then upscale — blur is cheap
                            # and the result is softer for free

# How much of the widget likeness survives on top of the photograph, and how
# much of the whole backdrop is washed out towards the theme background. The
# photo case washes less: the card sits on a dark blur and stays readable, and
# washing a photo to 45 % leaves grey mush.
PHOTO_LIKENESS_ALPHA = 0.22
WASH_ALPHA = 0.45
WASH_ALPHA_PHOTO = 0.24

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    _HAVE_PIL = True
except ImportError:                                    # pragma: no cover
    _HAVE_PIL = False


# ── the blurred backdrop ──────────────────────────────────────────────────
def _widget_fill(widget, style):
    """Background colour of a widget, tk or ttk."""
    try:
        value = widget.cget("background")
        if value:
            return str(value)
    except tk.TclError:
        pass
    try:
        name = widget.cget("style") or widget.winfo_class()
        value = style.lookup(name, "background")
        if value:
            return str(value)
    except tk.TclError:
        pass
    return None


def _collect_rects(root, style):
    """[(x, y, w, h, colour)] for every mapped widget, parents before children."""
    rects = []
    ox, oy = root.winfo_rootx(), root.winfo_rooty()

    def walk(widget, depth=0):
        if depth > 12:
            return
        try:
            if not widget.winfo_ismapped():
                return
            x = widget.winfo_rootx() - ox
            y = widget.winfo_rooty() - oy
            w, h = widget.winfo_width(), widget.winfo_height()
        except tk.TclError:
            return
        if w > 1 and h > 1:
            colour = _widget_fill(widget, style)
            if colour:
                rects.append((x, y, w, h, colour))
        for child in widget.winfo_children():
            walk(child, depth + 1)

    for child in root.winfo_children():
        walk(child)
    return rects


def _cover_fit(image, size):
    """Scale-and-crop `image` to exactly `size`, preserving its aspect ratio."""
    target_w, target_h = size
    src_w, src_h = image.size
    if not src_w or not src_h:
        return None
    factor = max(target_w / src_w, target_h / src_h)
    scaled = image.resize((max(1, int(src_w * factor + 0.5)),
                           max(1, int(src_h * factor + 0.5))), Image.LANCZOS)
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def _load_photo_layer(size):
    """The loading-screen photograph, cover-fitted to `size`, or None."""
    path = branding.loading_image_path()
    if not path:
        return None
    try:
        with Image.open(path) as source:
            return _cover_fit(source.convert("RGB"), size)
    except (OSError, ValueError):
        return None       # an unreadable asset must not stop the app locking


def render_backdrop(root, width, height):
    """A blurred PhotoImage of the current layout, or None without Pillow."""
    if not _HAVE_PIL or width < 8 or height < 8:
        return None

    scale = BACKDROP_SCALE
    sw, sh = max(8, int(width * scale)), max(8, int(height * scale))
    likeness = Image.new("RGB", (sw, sh), theme.BG)
    draw = ImageDraw.Draw(likeness)

    style = ttk.Style(root)
    for x, y, w, h, colour in _collect_rects(root, style):
        try:
            draw.rectangle(
                [x * scale, y * scale, (x + w) * scale, (y + h) * scale],
                fill=colour)
        except (ValueError, TypeError):
            continue          # a colour name Pillow does not know — skip it

    photo = _load_photo_layer((sw, sh))
    if photo is not None:
        # Photo underneath, layout likeness faintly over it: the blur then
        # applies to both at once, so they read as one image rather than as a
        # picture with a screenshot pasted on top.
        image = Image.blend(photo, likeness, PHOTO_LIKENESS_ALPHA)
        wash_alpha = WASH_ALPHA_PHOTO
    else:
        image = likeness
        wash_alpha = WASH_ALPHA

    image = image.filter(ImageFilter.GaussianBlur(BLUR_RADIUS * scale))
    image = image.resize((width, height), Image.LANCZOS)

    # Frost it: a light wash so the card on top stays readable over any layout.
    wash = Image.new("RGB", image.size, theme.BG)
    image = Image.blend(image, wash, wash_alpha)
    return ImageTk.PhotoImage(image)


# ── the overlay ───────────────────────────────────────────────────────────
class LockScreen:
    """Full-window lock. Call .lock() to arm, .unlock() to dismiss."""

    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.active = False
        self._frame = None
        self._canvas = None
        self._photo = None
        self._buffer = ""
        self._resize_job = None
        self._bindings = []

    # ── lifecycle ─────────────────────────────────────────────────────────
    def lock(self):
        if self.active:
            return
        self.root.update_idletasks()
        self.active = True
        self._buffer = ""
        self._build()
        self._bind_keys()

    def unlock(self):
        if not self.active:
            return
        self.active = False
        self._unbind_keys()
        if self._frame is not None:
            self._frame.destroy()
            self._frame = None
        self._photo = None
        self.app.on_unlocked()

    # ── construction ──────────────────────────────────────────────────────
    def _build(self):
        self._frame = tk.Frame(self.root, background=theme.BG,
                               highlightthickness=0, borderwidth=0)
        self._frame.place(x=0, y=0, relwidth=1, relheight=1)
        self._frame.lift()

        w = max(self.root.winfo_width(), 400)
        h = max(self.root.winfo_height(), 300)

        self._canvas = tk.Canvas(self._frame, highlightthickness=0, borderwidth=0,
                                 background=theme.BG, width=w, height=h)
        self._canvas.pack(fill="both", expand=True)
        self._paint_backdrop(w, h)

        card = tk.Frame(self._frame, background=theme.PANEL,
                        highlightbackground=theme.BORDER, highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center")
        self._card = card
        self._fill_card(card)

        self.root.bind("<Configure>", self._on_resize, add="+")

    def _paint_backdrop(self, w, h):
        self._canvas.delete("backdrop")
        self._photo = render_backdrop(self.root, w, h)
        if self._photo is not None:
            self._canvas.create_image(0, 0, image=self._photo, anchor="nw",
                                      tags="backdrop")
        else:
            self._canvas.create_rectangle(0, 0, w, h, fill=theme.BG, outline="",
                                          tags="backdrop")

    def _fill_card(self, card):
        pad = tk.Frame(card, background=theme.PANEL)
        pad.pack(padx=46, pady=38)

        self._logo_photo = None
        logo = branding.logo_path()
        if logo and _HAVE_PIL:
            try:
                image = Image.open(logo)
                image.thumbnail((300, 96), Image.LANCZOS)
                self._logo_photo = ImageTk.PhotoImage(image)
                tk.Label(pad, image=self._logo_photo,
                         background=theme.PANEL).pack(pady=(0, 18))
            except (OSError, ValueError):
                self._logo_photo = None
        if self._logo_photo is None:
            self._draw_logo_placeholder(pad)

        tk.Label(pad, text=branding.APP_LONG_NAME, background=theme.PANEL,
                 foreground=theme.TEXT,
                 font=theme.F.lock_title).pack()
        tk.Label(pad, text=branding.APP_TAGLINE, background=theme.PANEL,
                 foreground=theme.MUTED,
                 font=theme.F.ui).pack(pady=(2, 0))

        tk.Frame(pad, background=theme.BORDER, height=1).pack(fill="x", pady=20)

        tk.Label(pad, text=branding.author(), background=theme.PANEL,
                 foreground=theme.C1,
                 font=theme.F.title).pack()
        tk.Label(pad, text=branding.INFO["role"], background=theme.PANEL,
                 foreground=theme.MUTED,
                 font=theme.F.small).pack()
        tk.Label(pad, text=branding.institution(), background=theme.PANEL,
                 foreground=theme.TEXT,
                 font=theme.F.ui).pack(pady=(10, 0))
        tk.Label(pad, text=branding.INFO["location"], background=theme.PANEL,
                 foreground=theme.MUTED,
                 font=theme.F.small).pack()

        tk.Frame(pad, background=theme.BORDER, height=1).pack(fill="x", pady=20)

        # ── the lock itself ──
        self._status = tk.Label(
            pad, text="LOCKED", background=theme.PANEL, foreground=theme.FAIL,
            font=theme.F.mono_ui_bold)
        self._status.pack()

        tk.Label(pad,
                 text="No capture or analysis can start while locked.",
                 background=theme.PANEL, foreground=theme.MUTED,
                 font=theme.F.small).pack(pady=(4, 14))

        keys = tk.Frame(pad, background=theme.PANEL)
        keys.pack()
        self._key_boxes = []
        for label in (UNLOCK_KEY.upper(), "⏎"):
            box = tk.Label(keys, text=label, width=3, height=1,
                           background=theme.SURFACE, foreground=theme.MUTED,
                           highlightbackground=theme.BORDER, highlightthickness=1,
                           font=theme.F.mono_title_bold)
            box.pack(side="left", padx=5, pady=2, ipady=4)
            self._key_boxes.append(box)

        tk.Label(pad, text=f"press  {UNLOCK_KEY.upper()}  then  Enter  to unlock",
                 background=theme.PANEL, foreground=theme.TEXT,
                 font=theme.F.ui).pack(pady=(12, 0))

        credits = tk.Label(pad, text="credits & acknowledgements",
                           background=theme.PANEL, foreground=theme.C1,
                           cursor="hand2",
                           font=theme.F.small_link)
        credits.pack(pady=(18, 0))
        credits.bind("<Button-1>", lambda _e: self.app.show_credits())

    def _draw_logo_placeholder(self, parent):
        """Typographic stand-in until the official asset is dropped in.

        Deliberately plain: this does not imitate GFZ's mark. Put the real file
        at the package assets dir as gfz_logo.png and it replaces this automatically.
        """
        holder = tk.Frame(parent, background=theme.PANEL)
        holder.pack(pady=(0, 16))
        tk.Label(holder, text="GFZ", background=theme.PANEL,
                 foreground=theme.C1,
                 font=theme.F.lock_logo).pack()
        tk.Label(holder, text="Helmholtz Centre for Geosciences",
                 background=theme.PANEL, foreground=theme.MUTED,
                 font=theme.F.small).pack()

    # ── keys ──────────────────────────────────────────────────────────────
    def _bind_keys(self):
        self.root.focus_force()
        for sequence, handler in (("<Key>", self._on_key),
                                  ("<Return>", self._on_return),
                                  ("<KP_Enter>", self._on_return),
                                  ("<Escape>", self._on_escape)):
            self._bindings.append((sequence, self.root.bind(sequence, handler,
                                                            add="+")))

    def _unbind_keys(self):
        for sequence, funcid in self._bindings:
            try:
                self.root.unbind(sequence, funcid)
            except tk.TclError:
                pass
        self._bindings = []
        try:
            self.root.unbind("<Configure>")
        except tk.TclError:
            pass

    def _on_key(self, event):
        if not self.active or event.keysym in ("Return", "KP_Enter", "Escape"):
            return None
        if (event.char or "").lower() == UNLOCK_KEY:
            self._buffer = UNLOCK_KEY
            self._set_armed(True)
        else:
            self._buffer = ""
            self._set_armed(False)
        return "break"

    def _on_return(self, _event=None):
        if not self.active:
            return None
        if self._buffer == UNLOCK_KEY:
            self.unlock()
        else:
            self._flash_wrong()
        return "break"

    def _on_escape(self, _event=None):
        if not self.active:
            return None
        self._buffer = ""
        self._set_armed(False)
        return "break"

    def _set_armed(self, armed):
        self._key_boxes[0].configure(
            background=theme.C1 if armed else theme.SURFACE,
            foreground="#ffffff" if armed else theme.MUTED)
        self._status.configure(
            text="press Enter to confirm" if armed else "LOCKED",
            foreground=theme.C1 if armed else theme.FAIL)

    def _flash_wrong(self):
        self._status.configure(text=f"press {UNLOCK_KEY.upper()} first",
                               foreground=theme.AMBER)
        self.root.after(1100, lambda: self.active and self._set_armed(False))

    # ── resize ────────────────────────────────────────────────────────────
    def _on_resize(self, event):
        if not self.active or event.widget is not self.root:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        # Re-blurring on every configure event during a drag would be wasteful;
        # one render once the drag settles is enough.
        self._resize_job = self.root.after(180, self._rerender)

    def _rerender(self):
        self._resize_job = None
        if not self.active or self._canvas is None:
            return
        w = max(self.root.winfo_width(), 400)
        h = max(self.root.winfo_height(), 300)
        self._canvas.configure(width=w, height=h)
        self._paint_backdrop(w, h)
        self._card.lift()
