"""
ansi_console.py — the embedded terminal.

Not a terminal emulator: a faithful renderer for exactly what THIS pipeline
emits, which is a small and well-known set.

  * SGR colour/attribute codes — console.py:31-33 uses bold, dim, green,
    yellow, red, cyan, magenta, reset; soop_capture.sh:151-154 the same.
  * '\r' line rewrites — capture.sh:250 draws its progress bar this way, and
    soop_capture.sh:559 its countdown.
  * '\x1b[K' erase-to-end-of-line.

Two details that are easy to get wrong and would ruin the output:

  1. The pty's line discipline turns every '\n' into '\r\n' (ONLCR). Treating
     that '\r' as a progress-bar rewrite would erase every completed line, so
     a CR is held back until the next byte proves whether it is a real rewrite
     or just part of a newline. The hold-back survives across read chunks.
  2. Escape sequences get split across reads. Partial tails are buffered, not
     printed as garbage.

Cost control: output is drained on a timer in batches, inserted in one pass,
and the buffer is capped at MAX_LINES, so a chatty 20-minute session cannot
grow the GUI's memory without bound.
"""

import re
import time
import tkinter as tk
from tkinter import filedialog, ttk

from . import theme

MAX_LINES = 6000          # ring buffer; ~1.5 MB of text at typical line length
TRIM_TO = 5000            # trim in chunks so trimming is rare, not per-line
POLL_MS = 60              # 16 fps is plenty for a log and costs almost nothing
MAX_BYTES_PER_POLL = 400_000   # keep a burst from freezing the event loop

_CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([@-~])")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Anything that could still be the start of an unfinished escape sequence.
_PARTIAL_ESC_RE = re.compile(r"\x1b(\[[0-9;?]*|\][^\x07\x1b]*|)$")

_SGR_TAGS = {
    1: "bold", 2: "dim", 3: "italic", 4: "underline",
    30: "black", 31: "red", 32: "green", 33: "yellow",
    34: "blue", 35: "magenta", 36: "cyan", 37: "white",
    90: "black", 91: "red", 92: "green", 93: "yellow",
    94: "blue", 95: "magenta", 96: "cyan", 97: "white",
}
_COLOR_TAGS = {"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"}


class AnsiConsole(ttk.Frame):
    """Read-only ANSI-aware log view with a toolbar."""

    def __init__(self, master, title="CONSOLE", **kw):
        super().__init__(master, **kw)
        self._title = title
        self._queues = []            # (queue, label) currently being drained
        self._pending_text = ""      # partial escape sequence carry-over
        self._pending_cr = False     # see the ONLCR note in the module docstring
        self._active_tags = ()
        self._autoscroll = tk.BooleanVar(value=True)
        self._poll_job = None
        self._on_input = None        # set by the app when a job accepts stdin

        self._build()
        self._schedule_poll()

    # ── construction ──────────────────────────────────────────────────────
    def _build(self):
        bar = ttk.Frame(self, style="Surface.TFrame", padding=(8, 4))
        bar.pack(side="top", fill="x")

        ttk.Label(bar, text=self._title, background=theme.SURFACE,
                  foreground=theme.MUTED,
                  font=theme.F.small_bold).pack(side="left")

        self.status = ttk.Label(bar, text="idle", background=theme.SURFACE,
                                foreground=theme.MUTED,
                                font=theme.F.mono_small)
        self.status.pack(side="left", padx=(12, 0))

        ttk.Button(bar, text="Clear", command=self.clear, width=7).pack(side="right")
        ttk.Button(bar, text="Save log…", command=self.save_to_file,
                   width=10).pack(side="right", padx=(0, 6))
        ttk.Checkbutton(bar, text="Follow", variable=self._autoscroll,
                        style="TCheckbutton").pack(side="right", padx=(0, 10))

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)

        self.text = tk.Text(
            body, wrap="none", undo=False, autoseparators=False,
            background=theme.CONSOLE_BG, foreground=theme.CONSOLE_FG,
            insertbackground=theme.CONSOLE_FG,
            selectbackground=theme.CONSOLE_SEL, selectforeground=theme.CONSOLE_FG,
            font=theme.F.mono,
            relief="flat", borderwidth=0, highlightthickness=0,
            padx=10, pady=6, height=14, state="disabled",
        )
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self._configure_tags()

        # Input line: capture.sh asks questions, so the console must be able to
        # answer them even when the GUI form did not anticipate one.
        self._input_row = ttk.Frame(self, style="Surface.TFrame", padding=(8, 4))
        ttk.Label(self._input_row, text="send:", background=theme.SURFACE,
                  foreground=theme.MUTED,
                  font=theme.F.mono_small).pack(side="left")
        self._entry = ttk.Entry(self._input_row, font=theme.F.mono)
        self._entry.pack(side="left", fill="x", expand=True, padx=6)
        self._entry.bind("<Return>", self._send_input)
        ttk.Button(self._input_row, text="Send", width=6,
                   command=self._send_input).pack(side="left")

    def _configure_tags(self):
        for name, color in theme.CONSOLE_ANSI.items():
            self.text.tag_configure(name, foreground=color)
        # Named fonts, so zooming the GUI zooms the terminal with it.
        self.text.tag_configure("bold", font=theme.F.mono_bold)
        self.text.tag_configure("italic", font=theme.F.mono_italic)
        self.text.tag_configure("underline", underline=True)
        self.text.tag_configure("dim", foreground="#8b949e")
        # GUI's own annotations — visually distinct from the tools' output.
        self.text.tag_configure("gui", foreground="#8fa6bb",
                                font=theme.F.mono_italic)
        self.text.tag_configure("gui_head", foreground=theme.CONSOLE_ANSI["cyan"],
                                font=theme.F.mono_bold)
        self.text.tag_configure("gui_err", foreground=theme.CONSOLE_ANSI["red"],
                                font=theme.F.mono_bold)

    # ── public API ────────────────────────────────────────────────────────
    def attach(self, output_queue, label=""):
        """Start draining a RunHandle.output queue into the view."""
        self._queues.append(output_queue)
        self.set_status(label or "running", theme.RUNNING)

    def detach(self, output_queue):
        """Drain whatever is left, then stop watching the queue."""
        self._drain(output_queue, budget=10_000_000)
        if output_queue in self._queues:
            self._queues.remove(output_queue)

    def set_status(self, text, color=None):
        self.status.configure(text=text, foreground=color or theme.MUTED)

    def enable_input(self, callback):
        """Show the send-line row; callback(str) forwards it to the process."""
        self._on_input = callback
        if not self._input_row.winfo_ismapped():
            self._input_row.pack(side="bottom", fill="x")

    def disable_input(self):
        self._on_input = None
        if self._input_row.winfo_ismapped():
            self._input_row.pack_forget()

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._pending_text = ""
        self._pending_cr = False
        self._active_tags = ()

    def save_to_file(self, path=None):
        if path is None:
            path = filedialog.asksaveasfilename(
                title="Save console log",
                defaultextension=".log",
                initialfile=time.strftime("gui_console_%Y%m%d_%H%M%S.log"),
                filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if not path:
            return None
        with open(path, "w") as f:
            f.write(self.text.get("1.0", "end-1c"))
        self.gui_note(f"console log written to {path}")
        return path

    def gui_banner(self, text):
        """The GUI's own section header, in the tools' banner style."""
        self._write("\n" + "═" * 96 + "\n", ("gui_head",))
        self._write(f" {text}\n", ("gui_head",))
        self._write("═" * 96 + "\n", ("gui_head",))

    def gui_note(self, text):
        self._write(f"[sreto] {text}\n", ("gui",))

    def gui_error(self, text):
        self._write(f"[sreto] {text}\n", ("gui_err",))

    def contents(self):
        return self.text.get("1.0", "end-1c")

    # ── pumping ───────────────────────────────────────────────────────────
    def _schedule_poll(self):
        self._poll_job = self.after(POLL_MS, self._poll)

    def _poll(self):
        for q in list(self._queues):
            self._drain(q, budget=MAX_BYTES_PER_POLL)
        self._schedule_poll()

    def _drain(self, q, budget):
        chunks = []
        total = 0
        while total < budget:
            try:
                data = q.get_nowait()
            except Exception:
                break
            chunks.append(data)
            total += len(data)
        if chunks:
            blob = b"".join(chunks)
            self.feed(blob.decode("utf-8", errors="replace"))

    # ── the parser ────────────────────────────────────────────────────────
    def feed(self, text):
        """Render a chunk of terminal output. Safe to call with partial data."""
        if not text:
            return
        text = self._pending_text + text
        self._pending_text = ""

        # Hold back a trailing partial escape sequence for the next chunk.
        # The pattern is anchored to the end AND excludes the final byte of a
        # CSI/OSC sequence, so a COMPLETE sequence at the end never matches —
        # only genuinely unfinished ones are buffered.
        m = _PARTIAL_ESC_RE.search(text)
        if m:
            self._pending_text = text[m.start():]
            text = text[:m.start()]
        if not text:
            return

        text = _OSC_RE.sub("", text)            # window titles etc — not rendered

        self.text.configure(state="normal")
        pos = 0
        for m in _CSI_RE.finditer(text):
            if m.start() > pos:
                self._emit(text[pos:m.start()])
            self._apply_csi(m.group(1), m.group(2))
            pos = m.end()
        if pos < len(text):
            self._emit(text[pos:])
        self._trim()
        self.text.configure(state="disabled")

        if self._autoscroll.get():
            self.text.see("end")

    def _apply_csi(self, params, final):
        if final == "m":
            self._apply_sgr(params)
        elif final == "K":
            # Erase in line: 0/absent = to end, 1 = to start, 2 = whole line.
            arg = params or "0"
            if arg in ("0", ""):
                self.text.delete("insert", "insert lineend")
            elif arg == "1":
                self.text.delete("insert linestart", "insert")
            elif arg == "2":
                self.text.delete("insert linestart", "insert lineend")
        # Cursor motion (A/B/C/D/H/J…) is not emitted by this pipeline; ignoring
        # it is better than half-implementing a screen buffer.

    def _apply_sgr(self, params):
        codes = [int(p) for p in params.split(";") if p.isdigit()] or [0]
        tags = set(self._active_tags)
        for code in codes:
            if code == 0:
                tags.clear()
            elif code in (22,):
                tags.discard("bold")
                tags.discard("dim")
            elif code == 39:
                tags -= _COLOR_TAGS
            elif code in _SGR_TAGS:
                tag = _SGR_TAGS[code]
                if tag in _COLOR_TAGS:
                    tags -= _COLOR_TAGS
                tags.add(tag)
        self._active_tags = tuple(sorted(tags))

    def _emit(self, run):
        """Insert a plain-text run, honouring CR rewrites and newlines."""
        for part in re.split(r"([\r\n])", run):
            if part == "":
                continue
            if part == "\r":
                # Might be a bare CR (progress bar) or the CR of a CRLF pair.
                self._pending_cr = True
                continue
            if part == "\n":
                self._pending_cr = False      # it was a CRLF: just a newline
                self._insert("\n")
                continue
            if self._pending_cr:
                # A real carriage return: the tool is rewriting this line.
                self.text.delete("insert linestart", "insert lineend")
                self.text.mark_set("insert", "insert linestart")
                self._pending_cr = False
            self._insert(part)

    def _insert(self, chunk):
        self.text.insert("insert", chunk, self._active_tags or ())

    def _write(self, text, tags):
        """GUI-originated text (never contains escapes)."""
        self.text.configure(state="normal")
        if self._pending_cr:
            self.text.delete("insert linestart", "insert lineend")
            self.text.mark_set("insert", "insert linestart")
            self._pending_cr = False
        self.text.insert("insert", text, tags)
        self._trim()
        self.text.configure(state="disabled")
        if self._autoscroll.get():
            self.text.see("end")

    def _trim(self):
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            self.text.delete("1.0", f"{lines - TRIM_TO}.0")

    def _send_input(self, _event=None):
        if self._on_input is None:
            return
        value = self._entry.get()
        self._entry.delete(0, "end")
        self._on_input(value + "\n")


def strip_ansi(text):
    """Plain-text copy of terminal output (used for the saved run logs)."""
    return _CSI_RE.sub("", _OSC_RE.sub("", text)).replace("\r", "")
