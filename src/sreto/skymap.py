"""
skymap.py — where the satellites are right now, on a Tk canvas.

DELIBERATELY LIGHTWEIGHT. No matplotlib, no figure, no image: a couple of dozen
canvas items redrawn on demand. The point is a glance — "three IRIDIUMs in the
north-east, the GLOBALSTAR is behind the building" — not a publication figure.
The pipeline already produces the real skymaps (soop_planner.py writes them into
03_FIGURES/SOOP_AVAILABILITY) and this does not compete with them.

NO NEW ORBITAL MECHANICS, same rule as skyview.py. Positions come from the
az/el tracks skyview.VisibilityEngine already computed and cached for the
visibility mask, sampled at the requested time. So opening this view costs
nothing the availability table has not already paid for, and a satellite plotted
here is at the position the mask was evaluated against — the two cannot disagree.

PROJECTION: polar, zenith at the centre, horizon at the rim, north up and east
right — compass convention, the same as widgets.SkyCompass and the same as the
azimuths orbits.propagate_satellite returns. Radius is linear in elevation
(90° at the centre, 0° at the rim), which keeps low passes — the ones a horizon
mask actually decides — readable instead of crushing them into the rim.
"""

import math
import tkinter as tk

from . import theme

LABEL_MIN_EL = 12.0        # below this the rim is crowded; dots only
DOT_R = 4.5                # at scale 1.0, in pixels


def interpolate_track(times, values, when):
    """Value at `when` from a sampled track. None outside the sampled span.

    Linear between samples: skyview samples a pass ~48 times over a few minutes,
    so the error against a re-propagation is far below the size of the dot.
    """
    if not times or len(times) != len(values):
        return None
    if when < times[0] or when > times[-1]:
        return None
    for i in range(1, len(times)):
        if when <= times[i]:
            t0, t1 = times[i - 1], times[i]
            v0, v1 = values[i - 1], values[i]
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (when - t0) / (t1 - t0)
    return values[-1]


def interpolate_azimuth(times, az_values, when):
    """Azimuth at `when`, unwrapped so a pass crossing north does not sweep
    the long way round the compass (359° -> 1° must be +2°, not -358°)."""
    if not times or len(times) != len(az_values):
        return None
    unwrapped = [az_values[0]]
    for a in az_values[1:]:
        previous = unwrapped[-1]
        delta = (a - previous + 180.0) % 360.0 - 180.0
        unwrapped.append(previous + delta)
    value = interpolate_track(times, unwrapped, when)
    return None if value is None else value % 360.0


class SkyPosition:
    """One satellite, placed."""

    __slots__ = ("name", "catnr", "az_deg", "el_deg", "color", "constellation",
                 "geo", "pass_obj")

    def __init__(self, name, catnr, az_deg, el_deg, geo=False, pass_obj=None):
        self.name = name
        self.catnr = catnr
        self.az_deg = az_deg
        self.el_deg = el_deg
        self.geo = geo
        self.pass_obj = pass_obj
        self.constellation = theme.constellation_of(name, geo)
        self.color = theme.constellation_color(name, geo)

    def short_name(self):
        """'IRIDIUM 113' -> 'IR 113'. The canvas has no room for full names."""
        parts = str(self.name).replace("[PLACEHOLDER]", "").split()
        if not parts:
            return "?"
        head = parts[0][:2].upper()
        tail = next((p for p in parts[1:] if any(c.isdigit() for c in p)), "")
        return f"{head} {tail}".strip() if tail else parts[0][:8]


def positions_at(passes, engine, when, mask=None, limit=40):
    """[SkyPosition] for everything above the horizon at `when`.

    Uses only tracks the engine ALREADY has cached — never propagates. A pass
    whose track has not been computed yet is skipped rather than blocking the
    Tk thread on an SGP4 run; it appears as soon as the mask evaluation that
    the availability panel runs anyway has filled the cache.
    """
    out = []
    for p in passes:
        if not p.geo and not (p.rise_unix <= when <= p.set_unix):
            continue
        track = engine.cached_track(p)
        if track is None:
            continue
        times, el, az = track
        el_now = interpolate_track(times, el, when)
        az_now = interpolate_azimuth(times, az, when)
        if el_now is None or az_now is None or el_now < 0:
            continue
        # Masked satellites are kept and drawn dimmed rather than dropped: "it
        # is up but you cannot see it" is the answer this view exists to give.
        out.append(SkyPosition(p.name, p.catnr, az_now, el_now, p.geo, p))
    out.sort(key=lambda s: s.el_deg, reverse=True)
    return out[:limit]


class SkyMap(tk.Canvas):
    """Polar az/el view. Click a satellite to select its pass."""

    def __init__(self, master, size=250, on_select=None, background=None, **kw):
        self.base_size = size
        self._bg = background or theme.PANEL
        self.size = self._scaled()
        super().__init__(master, width=self.size, height=self.size,
                         highlightthickness=0, background=self._bg,
                         borderwidth=0, **kw)
        self.on_select = on_select
        self._positions = []
        self._mask = None
        self._hit = {}                 # canvas item -> SkyPosition
        self._subtitle = ""
        self.bind("<Button-1>", self._on_click)
        theme.on_scale_change(self._on_scale)
        self.redraw()

    def _scaled(self):
        return max(140, int(round(self.base_size * theme.scale())))

    def _on_scale(self, _scale):
        if not self.winfo_exists():
            return
        self.size = self._scaled()
        self.configure(width=self.size, height=self.size)
        self.redraw()

    # ── data ──────────────────────────────────────────────────────────────
    def set_positions(self, positions, mask=None, subtitle=""):
        self._positions = list(positions or [])
        self._mask = mask
        self._subtitle = subtitle
        self.redraw()

    # ── geometry ──────────────────────────────────────────────────────────
    def _radius(self):
        return (self.size - 2 * self._pad()) / 2.0

    def _pad(self):
        return max(14.0, self.size * 0.085)

    def _to_xy(self, az_deg, el_deg):
        """Compass polar -> canvas pixels. North up, east right, zenith centre."""
        centre = self.size / 2.0
        r = self._radius() * (1.0 - max(0.0, min(90.0, el_deg)) / 90.0)
        angle = math.radians(az_deg)
        return centre + r * math.sin(angle), centre - r * math.cos(angle)

    # ── drawing ───────────────────────────────────────────────────────────
    def redraw(self):
        self.delete("all")
        self._hit = {}
        centre = self.size / 2.0
        radius = self._radius()

        self._draw_mask(centre, radius)
        self._draw_grid(centre, radius)
        self._draw_satellites()
        if self._subtitle:
            self.create_text(centre, self.size - self._pad() * 0.35,
                             text=self._subtitle, fill=theme.MUTED,
                             font=theme.F.small)

    def _draw_mask(self, centre, radius):
        """Shade the sectors you canNOT see, so the plot answers the same
        question the compass does."""
        mask = self._mask
        if mask is None or mask.is_full_sky():
            return
        from . import skyview
        for name in skyview.SECTOR_NAMES:
            if name in mask.open_sectors:
                continue
            centre_az = skyview.SECTOR_CENTER[name]
            # Tk arcs run counterclockwise from east; compass runs clockwise
            # from north — the same conversion widgets.SkyCompass makes.
            start = 90.0 - (centre_az + skyview.SECTOR_WIDTH / 2.0)
            self.create_arc(centre - radius, centre - radius,
                            centre + radius, centre + radius,
                            start=start, extent=skyview.SECTOR_WIDTH,
                            style="pieslice", fill=theme.SURFACE_ALT,
                            outline="")

    def _draw_grid(self, centre, radius):
        # Elevation rings at 30° and 60°, plus the horizon.
        for el, label in ((0, ""), (30, "30°"), (60, "60°")):
            r = radius * (1.0 - el / 90.0)
            self.create_oval(centre - r, centre - r, centre + r, centre + r,
                             outline=theme.BORDER, width=1)
            if label:
                self.create_text(centre + 2, centre - r, text=label,
                                 fill=theme.BORDER, anchor="w",
                                 font=theme.F.small)
        for az in (0, 90, 180, 270):
            x, y = self._to_xy(az, 0)
            self.create_line(centre, centre, x, y, fill=theme.BORDER, width=1)

        for name, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            x, y = self._to_xy(az, 0)
            dx = (x - centre) * 0.1
            dy = (y - centre) * 0.1
            self.create_text(x + dx, y + dy, text=name, fill=theme.MUTED,
                             font=theme.F.small_bold)

    def _draw_satellites(self):
        mask = self._mask
        r = max(3.0, DOT_R * theme.scale())
        for sat in self._positions:
            x, y = self._to_xy(sat.az_deg, sat.el_deg)
            visible = (mask is None or mask.is_full_sky()
                       or mask.contains(sat.az_deg, sat.el_deg))
            fill = sat.color if visible else theme.SURFACE_ALT
            outline = theme.PANEL if visible else theme.BORDER

            if sat.geo:
                # A square for geostationary: it never rises or sets, so it is
                # not the same kind of thing as a pass and should not look it.
                item = self.create_rectangle(x - r, y - r, x + r, y + r,
                                             fill=fill, outline=outline, width=1)
            else:
                item = self.create_oval(x - r, y - r, x + r, y + r,
                                        fill=fill, outline=outline, width=1)
            self._hit[item] = sat

            if sat.el_deg >= LABEL_MIN_EL:
                # Flip the label to the left of the dot when it would run off
                # the canvas — a name clipped at the edge is worse than none.
                text = sat.short_name()
                width = theme.F.small.measure(text)
                if x + r + 3 + width > self.size:
                    anchor, tx = "e", x - r - 3
                else:
                    anchor, tx = "w", x + r + 3
                label = self.create_text(
                    tx, y, text=text, anchor=anchor,
                    fill=theme.TEXT if visible else theme.MUTED,
                    font=theme.F.small)
                self._hit[label] = sat

    # ── interaction ───────────────────────────────────────────────────────
    def _on_click(self, event):
        if self.on_select is None:
            return
        item = self.find_closest(event.x, event.y)
        if not item:
            return
        sat = self._hit.get(item[0])
        # find_closest always returns something, so require a real hit near the
        # pointer rather than selecting whatever grid line happened to be near.
        if sat is None:
            return
        x, y = self._to_xy(sat.az_deg, sat.el_deg)
        if math.hypot(event.x - x, event.y - y) > max(14.0, 14.0 * theme.scale()):
            return
        self.on_select(sat)


class Legend(tk.Frame):
    """Constellation colour key. Only the families actually on screen."""

    def __init__(self, master, background=None, **kw):
        self._bg = background or theme.PANEL
        super().__init__(master, background=self._bg, **kw)
        self._items = []

    def set_constellations(self, names):
        for widget in self._items:
            widget.destroy()
        self._items = []
        for name in names:
            color = theme.CONSTELLATION_COLORS.get(
                name, theme.CONSTELLATION_COLORS["OTHER"])
            cell = tk.Frame(self, background=self._bg)
            cell.pack(side="left", padx=(0, 10))
            dot = tk.Canvas(cell, width=9, height=9, highlightthickness=0,
                            background=self._bg, borderwidth=0)
            dot.create_oval(1, 1, 8, 8, fill=color, outline="")
            dot.pack(side="left")
            tk.Label(cell, text=name.title(), background=self._bg,
                     foreground=theme.MUTED,
                     font=theme.F.small).pack(side="left", padx=(4, 0))
            self._items.append(cell)
