"""
status.py — one honest answer to "is this thing working right now?".

FOUR STATES, and they are deliberately not the same as "a process is alive":

    idle                 nothing running
    busy                 a job is running and the machine is coping
    barely holding on    a job is running and the machine is NOT coping
    failed               the last run ended badly, and you have not moved on yet

The third one exists because a capture that is losing samples and a capture that
is fine look identical in a progress bar. bladeRF streaming is real-time — if
the machine cannot keep up, the radio drops samples and capture.sh writes a
SHORT file. Load average is a genuine, cheap early warning for exactly that, so
it is measured (see strain()) rather than guessed from the elapsed time.

The traffic light is a display of this model, not a second copy of it. Every
place the state is shown — window title, header lamp, console status — reads the
same StatusModel, so they cannot disagree.
"""

import os
import time
import tkinter as tk

from . import theme

IDLE = "idle"
BUSY = "busy"
STRAINED = "strained"
FAILED = "failed"

STATE_LABELS = {
    IDLE: "idle",
    BUSY: "busy",
    STRAINED: "barely holding on",
    FAILED: "failed",
}

# Which lamp of the three lights up. 'strained' lights amber AND red: the run
# is still going (amber) but it is in trouble (red), and that pair is the whole
# message.
STATE_LAMPS = {
    IDLE: ("ok",),
    BUSY: ("warn",),
    STRAINED: ("warn", "fail"),
    FAILED: ("fail",),
}

STATE_COLORS = {
    IDLE: theme.OK,
    BUSY: theme.AMBER,
    STRAINED: theme.AMBER,
    FAILED: theme.FAIL,
}

LAMP_COLORS = {"ok": theme.OK, "warn": theme.AMBER, "fail": theme.FAIL}
LAMP_ORDER = ("ok", "warn", "fail")

# Load average per core above which a streaming capture is at risk. 0.9 is
# below saturation on purpose: by the time the 1-minute average reaches 1.0 the
# samples have already been dropped.
STRAIN_THRESHOLD = 0.9
STRAIN_SAMPLE_S = 2.0


# ── the machine ───────────────────────────────────────────────────────────
_load_cache = {"unix": 0.0, "value": 0.0}


def load_factor():
    """1-minute load average per core. 0.0 when the platform has no loadavg."""
    now = time.time()
    if now - _load_cache["unix"] < STRAIN_SAMPLE_S:
        return _load_cache["value"]
    try:
        value = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (OSError, AttributeError):                      # pragma: no cover
        value = 0.0
    _load_cache.update({"unix": now, "value": value})
    return value


def strain():
    """(is_strained, load_per_core)."""
    factor = load_factor()
    return factor >= STRAIN_THRESHOLD, factor


# ── the model ─────────────────────────────────────────────────────────────
class StatusModel:
    """The app's current state, and the words for it.

    `failed` is sticky: a run that failed keeps the light red until something
    else starts. A red lamp that clears itself after two seconds is a lamp
    nobody ever sees.
    """

    def __init__(self):
        self.state = IDLE
        self.job_name = ""
        self.detail = ""
        self._load = 0.0

    # ── transitions ───────────────────────────────────────────────────────
    def set_idle(self, detail=""):
        self.state = IDLE
        self.job_name = ""
        self.detail = detail

    def set_busy(self, job_name, detail=""):
        self.state = BUSY
        self.job_name = job_name
        self.detail = detail

    def set_failed(self, job_name, detail=""):
        self.state = FAILED
        self.job_name = job_name
        self.detail = detail

    def refresh_strain(self):
        """Promote busy -> strained (and back) from the current load.

        Only ever moves between those two: a failure must not be masked by the
        machine recovering, and an idle app is not 'barely holding on' no
        matter what else is running on the laptop.
        """
        if self.state not in (BUSY, STRAINED):
            return self.state
        strained, factor = strain()
        self._load = factor
        self.state = STRAINED if strained else BUSY
        return self.state

    # ── words ─────────────────────────────────────────────────────────────
    def label(self):
        return STATE_LABELS.get(self.state, self.state)

    def color(self):
        return STATE_COLORS.get(self.state, theme.IDLE)

    def title_suffix(self):
        """What the window title says after the app name."""
        if self.state == IDLE:
            return STATE_LABELS[IDLE]
        if self.state == FAILED:
            return f"FAILED — {self.job_name}" if self.job_name else "FAILED"
        if self.state == STRAINED:
            return (f"barely holding on — {self.job_name} "
                    f"(load {self._load:.2f}/core)"
                    if self.job_name else STATE_LABELS[STRAINED])
        return f"busy — {self.job_name}" if self.job_name else STATE_LABELS[BUSY]

    def tooltip(self):
        if self.state == STRAINED:
            return (f"{self.job_name or 'a job'} is running at "
                    f"{self._load:.2f} load per core. A streaming capture can "
                    f"drop samples at this point — capture.sh would mark the "
                    f"result SHORT. Close what you can.")
        if self.state == FAILED:
            return self.detail or f"{self.job_name} failed — see the console."
        if self.state == BUSY:
            return f"{self.job_name} is running ({self._load:.2f} load/core)."
        return self.detail or "Nothing is running."

    def is_running(self):
        return self.state in (BUSY, STRAINED)


# ── the indicators ────────────────────────────────────────────────────────
def _mix(color, other, t):
    """Blend two #rrggbb colours; t=0 keeps `color`, t=1 gives `other`."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    r, g, bl = (round(x + (y - x) * t) for x, y in zip(a, b))
    return f"#{r:02x}{g:02x}{bl:02x}"


class TrafficLight(tk.Canvas):
    """Three lamps in a horizontal housing: green, amber, red.

    Horizontal because it lives in a header strip, and because the vertical
    arrangement everyone recognises would waste the one dimension the strip is
    short of. Unlit lamps are not hidden — a light with only one visible lamp
    does not read as a traffic light, and the dark lamps are what make the lit
    one legible at a glance.
    """

    def __init__(self, master, height=22, background=None, **kw):
        self.base_height = height
        self._bg = background or theme.BG
        self._state = IDLE
        self._pulse_on = True
        self._pulse_job = None
        w, h = self._dimensions()
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         background=self._bg, borderwidth=0, **kw)
        theme.on_scale_change(self._on_scale)
        self.redraw()

    def _dimensions(self):
        h = max(14, int(round(self.base_height * theme.scale())))
        return h * 3 - int(h * 0.35), h

    def _on_scale(self, _scale):
        if not self.winfo_exists():
            return
        w, h = self._dimensions()
        self.configure(width=w, height=h)
        self.redraw()

    def set_state(self, state):
        if state == self._state:
            return
        self._state = state
        # Only the strained state pulses. A blinking light for a healthy run
        # would train you to ignore the one that matters.
        if state == STRAINED:
            self._start_pulse()
        else:
            self._stop_pulse()
        self.redraw()

    def _start_pulse(self):
        if self._pulse_job is None:
            self._pulse()

    def _stop_pulse(self):
        if self._pulse_job is not None:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
        self._pulse_on = True

    def _pulse(self):
        self._pulse_on = not self._pulse_on
        self.redraw()
        self._pulse_job = self.after(620, self._pulse)

    def redraw(self):
        self.delete("all")
        w, h = self._dimensions()
        lit = set(STATE_LAMPS.get(self._state, ()))

        # Housing: a dark rounded slab, drawn as a rectangle between two arcs
        # because Tk's canvas has no rounded rectangle.
        pad = max(1.0, h * 0.08)
        r = (h - 2 * pad) / 2.0
        cy = h / 2.0
        housing = "#2b3036"
        self.create_oval(pad, pad, pad + 2 * r, h - pad, fill=housing, outline="")
        self.create_oval(w - pad - 2 * r, pad, w - pad, h - pad,
                         fill=housing, outline="")
        self.create_rectangle(pad + r, pad, w - pad - r, h - pad,
                              fill=housing, outline="")

        lamp_r = r * 0.62
        gap = (w - 2 * pad - 2 * r) / 2.0 if w > 2 * pad + 2 * r else r
        first = pad + r
        for i, lamp in enumerate(LAMP_ORDER):
            cx = first + i * gap
            color = LAMP_COLORS[lamp]
            on = lamp in lit and (self._pulse_on or lamp != "fail")
            if on:
                # A soft halo sells "lit" far better than a brighter fill.
                self.create_oval(cx - lamp_r * 1.45, cy - lamp_r * 1.45,
                                 cx + lamp_r * 1.45, cy + lamp_r * 1.45,
                                 fill=_mix(housing, color, 0.28), outline="")
                fill, outline = color, _mix(color, "#ffffff", 0.45)
            else:
                fill, outline = _mix(housing, color, 0.16), ""
            self.create_oval(cx - lamp_r, cy - lamp_r, cx + lamp_r, cy + lamp_r,
                             fill=fill, outline=outline,
                             width=max(1, int(lamp_r * 0.25)))

    def destroy(self):                                     # pragma: no cover
        self._stop_pulse()
        super().destroy()


class Spinner(tk.Canvas):
    """A rotating arc, shown only while something is actually running.

    Costs one `after` callback at 12 fps while visible and nothing at all when
    stopped — the animation is torn down rather than left spinning invisibly,
    because an idle GUI waking the CPU 12 times a second is exactly the kind of
    thing this toolkit is supposed to avoid.
    """

    FPS_MS = 80
    EXTENT = 105          # degrees of arc drawn

    def __init__(self, master, size=16, background=None, color=None, **kw):
        self.base_size = size
        self._bg = background or theme.BG
        self._color = color or theme.RUNNING
        self._angle = 0
        self._job = None
        s = self._size()
        super().__init__(master, width=s, height=s, highlightthickness=0,
                         background=self._bg, borderwidth=0, **kw)
        theme.on_scale_change(self._on_scale)

    def _size(self):
        return max(10, int(round(self.base_size * theme.scale())))

    def _on_scale(self, _scale):
        if not self.winfo_exists():
            return
        s = self._size()
        self.configure(width=s, height=s)
        if self._job is not None:
            self._draw()

    @property
    def running(self):
        return self._job is not None

    def start(self, color=None):
        if color:
            self._color = color
        if self._job is None:
            self._tick()

    def stop(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        self.delete("all")

    def _tick(self):
        self._angle = (self._angle - 30) % 360
        self._draw()
        self._job = self.after(self.FPS_MS, self._tick)

    def _draw(self):
        self.delete("all")
        s = self._size()
        pad = max(2.0, s * 0.14)
        width = max(2, int(s * 0.16))
        self.create_oval(pad, pad, s - pad, s - pad, outline=_mix(
            self._bg, self._color, 0.18), width=width)
        self.create_arc(pad, pad, s - pad, s - pad, start=self._angle,
                        extent=self.EXTENT, style="arc", outline=self._color,
                        width=width)

    def destroy(self):                                     # pragma: no cover
        self.stop()
        super().destroy()
