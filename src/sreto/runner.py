"""
runner.py — run the existing CLI tools, unmodified, and stream them live.

Design notes that matter:

PTY, NOT A PIPE.  The tools are written for a terminal and behave differently
without one:
  * console.py:30 sets _USE_COLOR = sys.stdout.isatty() -> a pipe would strip
    every colour out of MAIN.py / soop_planner.py.
  * soop_capture.sh:150 does the same test for its own colours.
  * capture.sh:250 draws its progress bar with '\r' overwrites.
  * capture.sh asks its parameters with `read -p`, which prints the prompt to
    the tty, not to stdout.
Running through a pty gives all four back, so the embedded console shows what
the terminal shows — which is the entire point of the requirement.

PROCESS GROUP.  start_new_session=True puts the child in its own group, so
Stop can signal the WHOLE tree. capture.sh runs bladeRF-cli in the background
and waits on it; killing only the shell would leave the radio streaming.

NICE.  Heavy analysis runs are launched under `nice` by default so a waterfall
pass cannot starve whatever else the machine is doing.

Nothing in this module writes to 01_CODE.
"""

import fcntl
import os
import pty
import queue
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time

# console.py hard-codes WIDTH = 104 for its rules and banners. Give the pty a
# little more than that so nothing wraps, but stay near it so the layout the
# tools were designed around is what actually renders.
PTY_COLS = 110
PTY_ROWS = 40

_READ_CHUNK = 65536


class RunHandle:
    """One running subprocess: live output queue, stdin, and a stop button.

    Output arrives as bytes in .output (a queue) — the GUI drains it on a timer
    so the Tk main loop is never blocked by a slow or chatty child.
    """

    def __init__(self, job, on_exit=None):
        self.job = job
        self.on_exit = on_exit
        self.output = queue.Queue()
        self.returncode = None
        self.started_at = None
        self.finished_at = None
        self.stopped_by_user = False
        self.error = None

        self._proc = None
        self._master_fd = None
        self._reader = None
        self._lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        argv = list(self.job.argv)
        if self.job.nice:
            nice_bin = shutil.which("nice")
            if nice_bin:
                argv = [nice_bin, "-n", str(self.job.nice)] + argv

        env = dict(os.environ)
        env.update(self.job.env or {})
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["COLUMNS"] = str(PTY_COLS)
        env["LINES"] = str(PTY_ROWS)
        env["TERM"] = "xterm-256color"
        env.pop("NO_COLOR", None)   # console.py:30 disables colour if this is set

        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, PTY_ROWS, PTY_COLS)

        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=self.job.cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,   # own process group -> stoppable as a tree
            )
        except (OSError, ValueError) as e:
            os.close(master_fd)
            os.close(slave_fd)
            self.error = f"could not start {argv[0]}: {e}"
            self.returncode = 127
            self.output.put(f"\r\n[sreto] ERROR: {self.error}\r\n".encode())
            if self.on_exit:
                self.on_exit(self)
            return self

        os.close(slave_fd)          # the child holds the only remaining copy
        self._master_fd = master_fd
        self.started_at = time.time()

        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"runner-{self.job.name}")
        self._reader.start()

        if self.job.stdin_lines:
            threading.Thread(target=self._feed_stdin, daemon=True,
                             name=f"stdin-{self.job.name}").start()
        return self

    def _feed_stdin(self):
        """Answer the script's prompts, one line at a time.

        This is capture.sh's documented prompt contract (soop_capture.sh:605)
        driven from the GUI instead of from another shell script. The small gap
        is cosmetic only — the pty's line discipline buffers regardless of
        whether the child has reached its `read` yet — it just makes the echoed
        answers appear under their prompts instead of all at once.
        """
        for line in self.job.stdin_lines:
            if not self.is_alive():
                return
            try:
                os.write(self._master_fd, (str(line) + "\n").encode())
            except OSError:
                return
            time.sleep(0.06)

    def _read_loop(self):
        while True:
            try:
                data = os.read(self._master_fd, _READ_CHUNK)
            except OSError:
                # EIO on the master is how a pty reports "the slave side is
                # gone" on both macOS and Linux — i.e. normal EOF here.
                break
            if not data:
                break
            self.output.put(data)

        rc = self._proc.wait()
        with self._lock:
            self.returncode = rc
            self.finished_at = time.time()
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        if self.on_exit:
            self.on_exit(self)

    # ── control ───────────────────────────────────────────────────────────
    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    def write(self, text):
        """Send a line to the running script (interactive answers, Ctrl-C…)."""
        if not self.is_alive() or self._master_fd is None:
            return False
        try:
            os.write(self._master_fd, text.encode())
            return True
        except OSError:
            return False

    def interrupt(self):
        """Ctrl-C the whole process group — soop_capture.sh's documented stop."""
        return self._signal_group(signal.SIGINT)

    def stop(self, grace_sec=4.0):
        """Interrupt, then escalate to TERM and KILL. Always leaves it dead."""
        if not self.is_alive():
            return
        self.stopped_by_user = True
        self._signal_group(signal.SIGINT)

        deadline = time.time() + grace_sec
        while time.time() < deadline and self.is_alive():
            time.sleep(0.1)
        if not self.is_alive():
            return

        self._signal_group(signal.SIGTERM)
        deadline = time.time() + grace_sec
        while time.time() < deadline and self.is_alive():
            time.sleep(0.1)
        if self.is_alive():
            self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig):
        if self._proc is None or self._proc.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.send_signal(sig)
                return True
            except OSError:
                return False

    @property
    def duration(self):
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at


class JobManager:
    """Serialises jobs: one radio/analysis run at a time, by design.

    Two concurrent captures would fight over the bladeRF, and two concurrent
    MAIN.py runs would fight over the fixed filenames in SAVE_DIR (MAIN.py:631
    already warns about exactly that hazard). Refusing the second start is
    cheaper than explaining the corrupted result later.
    """

    def __init__(self):
        self.current = None
        self._listeners = []

    def add_listener(self, fn):
        """fn(event, handle) with event in {'start', 'exit'}."""
        self._listeners.append(fn)

    def busy(self):
        return self.current is not None and self.current.is_alive()

    def start(self, job, on_exit=None):
        if self.busy():
            raise RuntimeError(
                f"'{self.current.job.name}' is still running — stop it first "
                f"(one radio, one analysis at a time)")

        def _exit(handle):
            for fn in self._listeners:
                fn("exit", handle)
            if on_exit:
                on_exit(handle)

        handle = RunHandle(job, on_exit=_exit)
        self.current = handle
        for fn in self._listeners:
            fn("start", handle)
        handle.start()
        return handle

    def stop(self):
        if self.current is not None:
            self.current.stop()


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, ValueError):
        pass
