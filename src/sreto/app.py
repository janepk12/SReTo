"""
app.py — the window, and the one place a job's whole lifecycle is handled.

Layout: a header strip (state at a glance), a notebook of panels, and the
console docked underneath in a draggable split. The console is always visible
because it is the actual output of the tools — the GUI is a way to launch them,
not a replacement for reading what they say.

Lifecycle of every run, in one place (_start_job / _on_job_exit):
    journal 'start'  ->  console banner + command line  ->  live stream
    ->  journal 'end' with the exit code  ->  save the console log
    ->  open the figures this run wrote  ->  reveal the output folder

Responsiveness: nothing heavy happens on the Tk thread. Subprocesses run under
runner.py's reader threads, pre-checks under their own, and the console drains
its queue on a 60 ms timer. Idle cost is a handful of `after` callbacks.
"""

import json
import os
import queue
import time
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from . import (
    ansi_console,
    branding,
    config,
    credits,
    diagnostics,
    header,
    history,
    jobs,
    lockscreen,
    paths,
    runner,
    status,
    system_open,
    theme,
    widgets,
)
from .panels.analysis_panel import AnalysisPanel
from .panels.automation_panel import AutomationPanel
from .panels.availability_panel import AvailabilityPanel
from .panels.capture_panel import CapturePanel
from .panels.history_panel import HistoryPanel
from .panels.physics_panel import PhysicsPanel
from .panels.precheck_panel import PrecheckPanel

APP_TITLE = "SReTo — SDR Reflectometry Toolkit"
MIN_SIZE = (1180, 760)
CONSOLE_LABEL_POPOUT = "Pop out console"
CONSOLE_LABEL_DOCK = "Dock console"
STRAIN_POLL_MS = 2000       # how often the load average is re-read while busy


class App:

    def __init__(self):
        paths.ensure_state_dirs()

        self.root = tk.Tk()
        self.root.minsize(*MIN_SIZE)
        self.root.geometry("1420x900")
        theme.apply_theme(self.root, scale=self._load_scale())

        self.manager = runner.JobManager()
        self.status = status.StatusModel()
        self._current_handle = None
        self._current_record = None
        self._current_callbacks = {}
        self._timer_job = None
        self._strain_job = None
        self._exit_queue = queue.Queue()   # reader thread -> Tk thread handover
        self._ui_queue = queue.Queue()     # any thread -> Tk thread callables
        self._credits_window = None

        self._build()
        self._build_menu()
        self._bind_popout_key()
        self._bind_zoom_keys()
        theme.on_scale_change(self._on_scale_changed)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._greet()
        self._poll_exits()
        self._apply_status()
        self._poll_strain()

        # Built last, over a fully-constructed window: the backdrop is a blur of
        # the real layout, so the layout has to exist first.
        self.lock = lockscreen.LockScreen(self)
        self.root.after(60, self.lock.lock)

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        self._build_header()

        # Kept on self: the console pop-out needs to reach both panes after
        # _build returns.
        self.split = split = ttk.PanedWindow(self.root, orient="vertical")
        split.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.notebook = ttk.Notebook(split)
        split.add(self.notebook, weight=3)

        # The console lives inside a CLASSIC tk.Frame, not directly in the
        # split. That host is what `wm manage` turns into a real window when
        # the console is popped out — Tk refuses to manage a ttk widget
        # ("must be a frame, labelframe or toplevel"), and a widget cannot be
        # reparented across toplevels in Tk at all, so the host is the only
        # way to move the console between the split and its own window WITHOUT
        # rebuilding it and losing its scrollback.
        self.console_host = tk.Frame(split, background=theme.PANEL,
                                     highlightthickness=0, bd=0)
        self.console = ansi_console.AnsiConsole(self.console_host,
                                                title="CONSOLE")
        self.console.pack(fill="both", expand=True)
        split.add(self.console_host, weight=2)

        self.capture_panel = CapturePanel(self.notebook, self)
        self.automation_panel = AutomationPanel(self.notebook, self)
        self.analysis_panel = AnalysisPanel(self.notebook, self)
        self.physics_panel = PhysicsPanel(self.notebook, self)
        self.availability_panel = AvailabilityPanel(self.notebook, self)
        self.history_panel = HistoryPanel(self.notebook, self)
        self.precheck_panel = PrecheckPanel(self.notebook, self)

        self.notebook.add(self.capture_panel, text="Capture")
        self.notebook.add(self.automation_panel, text="Automation")
        self.notebook.add(self.analysis_panel, text="Analysis")
        # Physics sits after Analysis because it reads what Analysis produces —
        # the tab order is the data flow.
        self.notebook.add(self.physics_panel, text="Physics")
        self.notebook.add(self.availability_panel, text="SoOp availability")
        self.notebook.add(self.history_panel, text="History")
        self.notebook.add(self.precheck_panel, text="Pre-checks")

        self._build_statusbar()

    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(14, 10, 14, 8))
        bar.pack(fill="x")

        left = ttk.Frame(bar)
        left.pack(side="left")
        ttk.Label(left, text="SDR REFLECTOMETRY TOOLKIT",
                  style="AppTitle.TLabel").pack(anchor="w")
        where = (paths.rel(paths.CODE_DIR) if paths.have_science_repo()
                 else "no science repo configured")
        ttk.Label(left, text=f"{where} · rx1 = direct, rx2 = reflected",
                  style="Muted.TLabel").pack(anchor="w")

        # The right-hand indicators are packed BEFORE the tile so that a
        # narrow window (or a high zoom) squeezes the tile, which degrades
        # gracefully, rather than clipping the status words, which do not.
        right = ttk.Frame(bar)
        right.pack(side="right")

        # The title tile — clocks and coordinates, always on screen.
        self.tile = header.HeaderTile(bar, self)
        self.tile.pack(side="left", padx=(18, 0))

        self.stop_btn = ttk.Button(right, text="Stop", style="Danger.TButton",
                                   command=self.stop_job, state="disabled", width=8)
        self.stop_btn.pack(side="right", padx=(10, 0))

        # ── the state indicator: spinner, traffic light, words ──
        state_box = ttk.Frame(right)
        state_box.pack(side="right", padx=(10, 12))

        lamp_row = ttk.Frame(state_box)
        lamp_row.pack(anchor="e")
        self.spinner = status.Spinner(lamp_row, size=16)
        self.spinner.pack(side="left", padx=(0, 7))
        self.traffic_light = status.TrafficLight(lamp_row, height=20)
        self.traffic_light.pack(side="left")
        self.state_label = ttk.Label(lamp_row, text="idle", style="TLabel",
                                     font=theme.F.ui_bold)
        self.state_label.pack(side="left", padx=(9, 0))

        # Which job, under the state word. Not a second status indicator — the
        # traffic light above owns the state, this only names what it refers to.
        self.job_label = ttk.Label(state_box, text="", style="Muted.TLabel",
                                   anchor="e")
        self.job_label.pack(anchor="e", pady=(2, 0))

        self.precheck_chip = widgets.StatusChip(right, "pre-checks", theme.IDLE)
        self.precheck_chip.pack(side="right")

        widgets.Tooltip(self.traffic_light,
                        "green idle · amber running · amber+red the machine is "
                        "struggling · red the last run failed")

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=10)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, style="Surface.TFrame", padding=(12, 5))
        bar.pack(fill="x", side="bottom")
        self.status_label = tk.Label(
            bar, text="ready", background=theme.SURFACE, foreground=theme.MUTED,
            font=theme.F.mono_small, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

        for label, target in (("02_DATA", paths.DATA_DIR),
                              ("Figures", paths.ANALYSIS_DIR),
                              ("SOOP", paths.SOOP_DIR)):
            ttk.Button(bar, text=label, width=8,
                       command=lambda t=target: self.reveal(t)).pack(side="right",
                                                                     padx=(6, 0))

        # ── zoom, where it can be found without knowing the shortcut ──
        zoom = ttk.Frame(bar, style="Surface.TFrame")
        zoom.pack(side="right", padx=(0, 16))
        ttk.Button(zoom, text="−", width=2,
                   command=self.zoom_out).pack(side="left")
        self.zoom_label = tk.Label(zoom, text="100%", background=theme.SURFACE,
                                   foreground=theme.MUTED, width=5,
                                   cursor="hand2", font=theme.F.mono_small)
        self.zoom_label.pack(side="left", padx=2)
        self.zoom_label.bind("<Button-1>", lambda _e: self.zoom_reset())
        widgets.Tooltip(self.zoom_label,
                        "Cmd/Ctrl + and Cmd/Ctrl − to zoom, Cmd/Ctrl 0 to "
                        "reset. Click to reset.")
        ttk.Button(zoom, text="+", width=2,
                   command=self.zoom_in).pack(side="left")

        # Small, permanent, and out of the way — the lock screen's credits are
        # gone once you unlock, and this is where they live afterwards.
        about_text = "ⓘ " + " · ".join(
            filter(None, (branding.author(), branding.institution())))
        about = tk.Label(bar, text=about_text,
                         background=theme.SURFACE, foreground=theme.MUTED,
                         cursor="hand2",
                         font=theme.F.small)
        about.pack(side="right", padx=(0, 14))
        about.bind("<Button-1>", lambda _e: self.show_credits())
        about.bind("<Enter>", lambda _e: about.configure(foreground=theme.C1))
        about.bind("<Leave>", lambda _e: about.configure(foreground=theme.MUTED))

    # ── zoom ──────────────────────────────────────────────────────────────
    # ── console pop-out ───────────────────────────────────────────────────
    # `wm manage` promotes the host frame to a real top-level window and
    # `wm forget` puts it back, which is the only way to move a live widget
    # out of a paned window in Tk without destroying and rebuilding it.
    # Rebuilding would drop the scrollback, and the scrollback is the reason
    # anybody pops the console out in the first place.
    def _bind_popout_key(self):
        for modifier in ("Command", "Control"):
            self.root.bind_all(f"<{modifier}-J>", self._on_popout_console)
            self.root.bind_all(f"<{modifier}-Shift-j>", self._on_popout_console)

    def _on_popout_console(self, _event=None):
        self.toggle_console_popout()
        return "break"

    def console_popped_out(self):
        return self.console_host.winfo_manager() == "wm"

    def popout_label(self):
        return (CONSOLE_LABEL_DOCK if self.console_popped_out()
                else CONSOLE_LABEL_POPOUT)

    def toggle_console_popout(self):
        if self.console_popped_out():
            self.dock_console()
        else:
            self.popout_console()
        if getattr(self, "_popout_menu", None) is not None:
            self._popout_menu.entryconfigure(self._popout_menu_index,
                                             label=self.popout_label())

    def popout_console(self):
        """Detach the console into its own resizable window."""
        if self.console_popped_out():
            return
        if self.console_host.winfo_manager():
            self.split.forget(self.console_host)
        host, tkw = self.console_host, self.console_host.tk
        tkw.call("wm", "manage", host._w)
        tkw.call("wm", "title", host._w, "SReTo — Console")
        tkw.call("wm", "geometry", host._w, self._popout_geometry())
        # Closing the window docks the console rather than destroying it;
        # destroying the host would take the scrollback with it and leave the
        # app with nowhere to print.
        tkw.call("wm", "protocol", host._w, "WM_DELETE_WINDOW",
                 host.register(self.dock_console))

    def dock_console(self):
        """Put a popped-out console back into the split."""
        if not self.console_popped_out():
            return
        host = self.console_host
        try:
            self._popout_geom = host.tk.call("wm", "geometry", host._w)
        except tk.TclError:                                 # pragma: no cover
            pass
        host.tk.call("wm", "forget", host._w)
        self.split.add(host, weight=2)

    def _popout_geometry(self):
        """Where the pop-out opens: where it was last, else beside the main
        window at a size that can actually show a run log."""
        if getattr(self, "_popout_geom", None):
            return self._popout_geom
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + 40
        y = self.root.winfo_rooty() + max(self.root.winfo_height() - 320, 60)
        return f"960x360+{x}+{y}"

    def _build_menu(self):
        """A real menu bar, so the zoom levels are discoverable and named."""
        menubar = tk.Menu(self.root)

        view = tk.Menu(menubar, tearoff=0)
        view.add_command(label="Zoom in", accelerator="Cmd/Ctrl +",
                         command=self.zoom_in)
        view.add_command(label="Zoom out", accelerator="Cmd/Ctrl −",
                         command=self.zoom_out)
        view.add_command(label="Actual size", accelerator="Cmd/Ctrl 0",
                         command=self.zoom_reset)
        view.add_separator()
        for step in theme.SCALE_STEPS:
            view.add_command(label=f"{step * 100:.0f}%",
                             command=lambda s=step: self.set_zoom(s))
        view.add_separator()
        self._popout_menu = view
        self._popout_menu_index = view.index("end") + 1
        view.add_command(label=CONSOLE_LABEL_POPOUT,
                         accelerator="Cmd/Ctrl+Shift+J",
                         command=self.toggle_console_popout)
        menubar.add_cascade(label="View", menu=view)

        tools = tk.Menu(menubar, tearoff=0)
        tools.add_command(label="Pre-checks",
                          command=lambda: self.notebook.select(self.precheck_panel))
        tools.add_command(label="Update location fix",
                          command=lambda: self.tile.request_laptop_fix())
        tools.add_separator()
        tools.add_command(label=f"About {branding.APP_NAME}",
                          command=self.show_credits)
        menubar.add_cascade(label="Tools", menu=tools)

        try:
            self.root.configure(menu=menubar)
        except tk.TclError:                                # pragma: no cover
            pass          # a window manager without menu support — not fatal
        self._menubar = menubar

    def _bind_zoom_keys(self):
        # Cmd on macOS, Ctrl elsewhere — bound together because the same repo
        # gets used from a Mac laptop and a Linux workstation. '<Command-plus>'
        # alone would not fire: on most layouts the key that is pressed is
        # '=' with Command held, so 'equal' has to be bound as well.
        for modifier in ("Command", "Control"):
            for key in ("plus", "equal", "KP_Add"):
                self.root.bind_all(f"<{modifier}-{key}>", self._on_zoom_in)
            for key in ("minus", "underscore", "KP_Subtract"):
                self.root.bind_all(f"<{modifier}-{key}>", self._on_zoom_out)
            for key in ("0", "KP_0"):
                self.root.bind_all(f"<{modifier}-{key}>", self._on_zoom_reset)

    def _on_zoom_in(self, _event=None):
        self.zoom_in()
        return "break"

    def _on_zoom_out(self, _event=None):
        self.zoom_out()
        return "break"

    def _on_zoom_reset(self, _event=None):
        self.zoom_reset()
        return "break"

    def zoom_in(self):
        return self.set_zoom(theme.snap_scale(theme.scale(), +1))

    def zoom_out(self):
        return self.set_zoom(theme.snap_scale(theme.scale(), -1))

    def zoom_reset(self):
        return self.set_zoom(1.0)

    def set_zoom(self, value):
        applied = theme.set_scale(value)
        self._save_scale(applied)
        return applied

    def _on_scale_changed(self, scale):
        """theme calls this after every resize — update the readout only.

        The widgets themselves need no help: they hold NAMED fonts, which have
        already been reconfigured by the time this runs.
        """
        if getattr(self, "zoom_label", None) is not None:
            self.zoom_label.configure(text=f"{scale * 100:.0f}%")

    def _load_scale(self):
        try:
            with open(paths.PRESETS_JSON, encoding="utf-8") as f:
                return float(json.load(f).get("ui_scale", 1.0))
        except (OSError, ValueError, TypeError):
            return 1.0

    def _save_scale(self, scale):
        paths.ensure_state_dirs()
        try:
            with open(paths.PRESETS_JSON, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["ui_scale"] = scale
        try:
            with open(paths.PRESETS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass          # a preference that fails to save is not worth a dialog

    # ── state ─────────────────────────────────────────────────────────────
    def post_to_ui(self, callback):
        """Run `callback` on the Tk thread. Safe to call from any thread.

        Same discipline as _on_job_exit: a worker thread must never touch the
        Tcl interpreter, so it leaves a callable here and the poller runs it.
        """
        self._ui_queue.put(callback)

    def _apply_status(self):
        """Push the one status model out to every place that displays it.

        Including the chip: two indicators in the same header that can disagree
        is worse than one, and this is the only writer of both.
        """
        state = self.status.state
        label = self.status.label()
        color = self.status.color()

        self.root.title(f"{APP_TITLE}  —  {self.status.title_suffix()}")
        self.traffic_light.set_state(state)
        self.state_label.configure(text=label, foreground=color)
        self.job_label.configure(text=self.status.detail or self.status.job_name,
                                 foreground=color)

        if self.status.is_running():
            self.spinner.start(color)
        else:
            self.spinner.stop()

    def _set_state(self, state_setter, *args):
        state_setter(*args)
        self._apply_status()

    def _poll_strain(self):
        """Re-read the load average while a job runs, and only then.

        An idle GUI must not be waking up to measure a machine it is not using,
        so the poll reschedules at a slow rate when nothing is running.
        """
        before = self.status.state
        if self.status.is_running():
            if self.status.refresh_strain() != before:
                self._apply_status()
                if self.status.state == status.STRAINED:
                    self.console.gui_error(
                        f"the machine is at {status.load_factor():.2f} load per "
                        f"core — a streaming capture can start dropping samples "
                        f"here, and capture.sh would log the result as SHORT")
        delay = STRAIN_POLL_MS if self.status.is_running() else STRAIN_POLL_MS * 4
        self._strain_job = self.root.after(delay, self._poll_strain)

    def _greet(self):
        self.console.gui_banner("SReTo — SDR REFLECTOMETRY TOOLKIT")
        self.console.gui_note(
            "front-end only — every run below is the unmodified CLI tool in "
            "the science repo's 01_CODE, launched in a pty so its colours, "
            "progress bars and prompts behave exactly as in a terminal.")
        if not paths.have_science_repo():
            self.console.gui_error(
                "NO SCIENCE REPOSITORY — nothing can be captured, planned or "
                "analysed until one is configured.")
            for line in config.missing_repo_message().splitlines():
                self.console.gui_error(f"    {line}" if line.strip() else "")
            return

        self.console.gui_note(f"repo      : {paths.REPO_ROOT} "
                              f"(from {config.repo_source()})")
        self.console.gui_note(f"python    : {paths.python_executable()}")
        self.console.gui_note(f"state     : {paths.GUI_STATE_DIR}")
        self.console.gui_note(f"captures  : {paths.rel(paths.DATA_DIR)}")
        self.console.gui_note(f"figures   : {paths.rel(paths.ANALYSIS_DIR)}")
        self.console.gui_note("run the Pre-checks tab before a session.")

    # ── job lifecycle ─────────────────────────────────────────────────────
    def run_job(self, job, on_finish=None, open_artifacts=False,
                reveal_output=False):
        """Launch `job`, stream it, and handle everything that follows."""
        # The lock overlay covers the buttons, but a panel method called from
        # anywhere else would sail straight past it. The refusal belongs where
        # jobs actually start, which is here.
        if getattr(self, "lock", None) is not None and self.lock.active:
            self.console.gui_error(
                f"refused to start '{job.name}' — the app is locked "
                f"(press {lockscreen.UNLOCK_KEY.upper()} then Enter)")
            return None

        if not paths.have_science_repo():
            self.error(
                "No science repository",
                config.missing_repo_message())
            return None

        if self.manager.busy():
            self.error(
                "Already running",
                f"'{self.manager.current.job.name}' is still running.\n\n"
                "Stop it first — two captures would fight over the radio, and "
                "two analyses would overwrite each other's figures in the same "
                "output directory.")
            return None

        run_id = uuid.uuid4().hex[:12]
        started = time.time()
        record = {
            "run_id": run_id,
            "event": "start",
            "kind": job.kind,
            "name": job.name,
            "summary": job.summary or job.name,
            "command": jobs.describe(job),
            "started_unix": started,
            "output_dir": job.output_dir,
            "status": "RUNNING",
        }
        record.update({k: v for k, v in (job.meta or {}).items()
                       if isinstance(v, (str, int, float, bool, type(None)))})
        history.append_journal(record)

        self._current_record = record
        self._current_callbacks = {
            "on_finish": on_finish,
            "open_artifacts": open_artifacts,
            "reveal_output": reveal_output,
            "artifact_dir": job.artifact_dir,
            "started": started,
        }

        self.console.gui_banner(f"{job.name.upper()} — {job.summary or 'run'}")
        self.console.gui_note(jobs.describe(job))
        self.console.gui_note(f"cwd {job.cwd}")
        if job.stdin_lines:
            self.console.gui_note(
                f"{len(job.stdin_lines)} prompt answer(s) will be typed in "
                f"(blank = the script's own default)")

        try:
            handle = self.manager.start(job, on_exit=self._on_job_exit)
        except RuntimeError as e:
            self.error("Cannot start", str(e))
            return None

        self._current_handle = handle
        self.console.attach(handle.output, label=job.name)
        self.console.enable_input(lambda text: handle.write(text))
        self.stop_btn.configure(state="normal")
        self._set_state(self.status.set_busy, job.name,
                        f"{job.name} running")
        self._tick_timer()
        return handle

    def _on_job_exit(self, handle):
        """Called from the runner's READER THREAD. Touches no Tk.

        The obvious version of this was `self.root.after(0, ...)`, which is
        cross-thread access to the Tcl interpreter. It happens to work while
        mainloop() is spinning and raises 'main thread is not in main loop'
        when it is not — and the raise lands in the reader thread, where it
        kills the completion handler silently: the job never finishes, Stop
        stays armed, and the journal keeps a RUNNING row forever.

        So the thread only hands the finished handle to a queue. The Tk side
        drains it from its own timer, where touching widgets is legal.
        """
        self._exit_queue.put(handle)

    def _poll_exits(self):
        """Main-thread half of the handover. Runs for the app's whole life.

        Also drains the general-purpose UI queue, so any worker thread (the
        location lookup, for one) has a legal way back onto the Tk thread.
        """
        while True:
            try:
                handle = self._exit_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._finish_job(handle)
            except Exception as e:                     # noqa: BLE001
                # A crash here would leave the GUI wedged mid-run, so report it
                # and reset rather than let it propagate into the timer.
                self.console.gui_error(f"post-run handling failed: {e}")
                self._reset_after_run()

        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as e:                     # noqa: BLE001
                self.console.gui_error(f"UI callback failed: {e}")

        self.root.after(80, self._poll_exits)

    def _finish_job(self, handle):
        cb = self._current_callbacks or {}
        record = self._current_record or {}
        self.console.detach(handle.output)
        self.console.disable_input()

        rc = handle.returncode
        if handle.stopped_by_user:
            verdict, color = "STOPPED", theme.AMBER
        elif rc == 0:
            verdict, color = "SUCCESS", theme.OK
        else:
            verdict, color = "FAILED", theme.FAIL

        duration = handle.duration
        self.console.gui_note(
            f"{handle.job.name} exited with code {rc} after "
            f"{history.fmt_duration(duration)} — {verdict}")
        if handle.error:
            self.console.gui_error(handle.error)

        # Errors get an explanation read OFF THE OUTPUT, not guessed from the
        # exit code — see diagnostics.py for why that distinction was earned.
        if verdict == "FAILED":
            headline, guidance = diagnostics.diagnose(
                diagnostics.tail(ansi_console.strip_ansi(self.console.contents())),
                returncode=rc, kind=handle.job.kind)
            self.console.gui_error(headline)
            for line in (guidance or "").splitlines():
                if line.strip():
                    self.console.gui_error(f"    {line}")

        log_path = self._save_run_log(record.get("run_id", "run"), handle)

        history.update_journal_entry(
            record.get("run_id", "unknown"),
            kind=handle.job.kind,
            name=handle.job.name,
            summary=handle.job.summary,
            started_unix=cb.get("started"),
            duration_sec=duration,
            returncode=rc,
            status=verdict,
            output_dir=handle.job.output_dir,
            log_path=log_path,
        )

        self.stop_btn.configure(state="disabled")
        self.status_label.configure(
            text=f"{handle.job.name}: {verdict} in {history.fmt_duration(duration)}",
            foreground=color)
        # FAILED is sticky, STOPPED is not: you stopped it, so you know.
        if verdict == "FAILED":
            self._set_state(self.status.set_failed, handle.job.name,
                            f"{handle.job.name} failed (exit {rc})")
        else:
            self._set_state(self.status.set_idle,
                            f"{handle.job.name} {verdict.lower()}")
        self._stop_timer()

        # ── results ──
        self._deliver_results(handle, cb, verdict)

        self._reset_after_run()

        self.history_panel.refresh()
        if handle.job.kind == "capture":
            self.analysis_panel.refresh_captures()
            self.physics_panel.refresh_captures()
        if handle.job.kind == "analysis":
            # MAIN.py writes the physics summary too when the stage is enabled,
            # so the chain on screen is stale the moment an analysis finishes.
            self.physics_panel.reload()
        if handle.job.kind == "planner":
            self.availability_panel.refresh()
            self.automation_panel.refresh_status()

        finish_cb = cb.get("on_finish")
        if finish_cb:
            try:
                finish_cb(handle)
            except Exception as e:                     # noqa: BLE001
                self.console.gui_error(f"post-run callback failed: {e}")

    def _reset_after_run(self):
        """Return the GUI to idle. Safe to call twice, and from the error path."""
        self._current_handle = None
        self._current_record = None
        self._current_callbacks = {}
        self._stop_timer()
        self.stop_btn.configure(state="disabled")
        self.console.disable_input()

    def _deliver_results(self, handle, cb, verdict):
        """Open figures and reveal the output — but only for a run that finished.

        A failed run still leaves whatever it managed to write, and opening
        those on screen presents a partial pipeline as a result. MAIN.py itself
        treats "which files did THIS run write" as a correctness question
        (MAIN.py:631, the stale-output guard), so the GUI names the partial
        output instead of displaying it.
        """
        artifact_dir = cb.get("artifact_dir")
        since = cb.get("started", 0)

        if verdict != "SUCCESS":
            if artifact_dir:
                partial = system_open.figures_written_since(artifact_dir, since)
                if partial:
                    self.console.gui_note(
                        f"the run wrote {len(partial)} figure(s) before it "
                        f"{verdict.lower()} — NOT opened, they are a partial "
                        f"pipeline: "
                        + ", ".join(os.path.basename(f) for f in partial))
            return

        if cb.get("open_artifacts") and artifact_dir:
            figures = system_open.figures_written_since(artifact_dir, since)
            if figures:
                _n, msg = system_open.open_paths(figures)
                self.console.gui_note(
                    f"{msg}: " + ", ".join(os.path.basename(f) for f in figures))
            else:
                self.console.gui_note(
                    f"no new figures in {paths.rel(artifact_dir)} — nothing to open")

        if cb.get("reveal_output") and handle.job.output_dir:
            self.reveal(handle.job.output_dir)

    def _save_run_log(self, run_id, handle):
        """Plain-text copy of this run's console output, ANSI stripped."""
        try:
            paths.ensure_state_dirs()
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = f"{stamp}_{handle.job.kind}_{run_id}.log"
            path = os.path.join(paths.GUI_LOG_DIR, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(ansi_console.strip_ansi(self.console.contents()))
            return path
        except OSError as e:
            self.console.gui_error(f"could not write the run log: {e}")
            return ""

    def stop_job(self):
        handle = self._current_handle
        if handle is None or not handle.is_alive():
            return
        if handle.job.kind == "capture" and not messagebox.askyesno(
                "Stop the capture?",
                "The radio is streaming. Stopping now leaves a short .bin — "
                "capture.sh will mark it SHORT in the master log.\n\nStop it?",
                parent=self.root):
            return
        self.console.gui_note("stopping — SIGINT to the process group, then "
                              "TERM, then KILL")
        self.status.detail = "stopping…"
        self._apply_status()
        handle.stop()

    def _tick_timer(self):
        handle = self._current_handle
        if handle is None or not handle.is_alive():
            return
        self.status_label.configure(
            text=f"{handle.job.name} running — "
                 f"{history.fmt_duration(handle.duration)} elapsed",
            foreground=theme.RUNNING)
        self._timer_job = self.root.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_job is not None:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None

    # ── services the panels use ───────────────────────────────────────────
    def reveal(self, directory):
        ok, msg = system_open.reveal_directory(directory)
        (self.console.gui_note if ok else self.console.gui_error)(msg)

    def error(self, title, message):
        self.console.gui_error(f"{title}: {message.splitlines()[0]}")
        messagebox.showerror(title, message, parent=self.root)

    def show_credits(self):
        return credits.show(self)

    def on_unlocked(self):
        """Called once, by the lock screen, when the user lets the app run."""
        self.console.gui_note(
            f"unlocked by {branding.author()} — captures and analyses are now "
            f"permitted")
        self.status_label.configure(text="unlocked — ready", foreground=theme.OK)
        self.precheck_panel.probe_var.set(True)

    def set_precheck_status(self, worst, text):
        color = {"OK": theme.OK, "WARN": theme.AMBER, "FAIL": theme.FAIL}.get(
            worst, theme.IDLE)
        self.precheck_chip.set(f"pre-checks: {text}", color)

    def analyse_capture(self, filename):
        """Jump to the Analysis tab with `filename` selected."""
        self.notebook.select(self.analysis_panel)
        self.analysis_panel.refresh_captures()
        self.analysis_panel.capture_var.set(filename)
        self.analysis_panel._describe_capture(filename)
        self.analysis_panel._refresh_memory()

    # ── shutdown ──────────────────────────────────────────────────────────
    def _on_close(self):
        if self.manager.busy():
            handle = self.manager.current
            if not messagebox.askyesno(
                    "Quit while a run is active?",
                    f"'{handle.job.name}' is still running.\n\n"
                    "Quitting stops it (SIGINT, then TERM, then KILL). A capture "
                    "in progress will be cut short.\n\nQuit anyway?",
                    parent=self.root):
                return
            handle.stop()
        # Stop the animated widgets before the interpreter goes away, or their
        # `after` callbacks fire into a destroyed window on the way out.
        for job in (self._strain_job, self._timer_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:                        # pragma: no cover
                    pass
        self.spinner.stop()
        self.tile.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
