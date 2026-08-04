"""
history.py — what has been captured, what has been analysed, and how it went.

Three sources, merged into one timeline:

  1. 02_DATA/master_experiment_log.csv   every capture.sh run (capture.sh:337)
  2. 02_DATA/master_failures.csv         short/overrun captures (capture.sh:307)
  3. gui/.state/runs.jsonl               everything the GUI launched

The first two are written by the shell scripts and are READ ONLY here — the GUI
never edits the master logs, so a capture started from a terminal and one
started from the GUI produce the same single record, written by the same script.
The journal is the GUI's own addition: it is the only place analysis runs, plan
runs and stopped sessions are recorded at all.

Deliberately stdlib-only. Pulling pandas in for a 45-row CSV would cost ~0.4 s
of startup and ~100 MB of RSS in a process that is supposed to be invisible.

TIME: stored UTC/unix, displayed Europe/Berlin — the repo's convention.
"""

import csv
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import paths

BERLIN = ZoneInfo("Europe/Berlin")

KIND_LABELS = {
    "capture": "capture",
    "autocapture": "soop session",
    "analysis": "analysis",
    "planner": "planner",
    "precheck": "pre-check",
}


def fmt_berlin(unix, fmt="%Y-%m-%d %H:%M:%S"):
    """Berlin wall-clock, matching soop_planner.fmt_berlin's convention."""
    if not unix:
        return ""
    try:
        return datetime.fromtimestamp(float(unix), BERLIN).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return ""


def fmt_duration(seconds):
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    return f"{int(s // 3600)}h {int((s % 3600) // 60):02d}m"


# ── the GUI's own run journal ─────────────────────────────────────────────
def append_journal(record):
    """One JSON object per line. Append-only; corrupt lines never block a run."""
    paths.ensure_state_dirs()
    record = dict(record)
    record.setdefault("logged_unix", time.time())
    try:
        with open(paths.RUN_JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass          # a journal write must never break the run it describes
    return record


def read_journal(limit=500):
    if not os.path.isfile(paths.RUN_JOURNAL):
        return []
    out = []
    try:
        with open(paths.RUN_JOURNAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue      # a half-written line from a hard kill
    except OSError:
        return []
    return out[-limit:]


def update_journal_entry(run_id, **fields):
    """Journal entries are immutable; a completion is a second row.

    Rewriting a line in place would mean holding the file open across a capture
    that can run for hours. Appending is crash-proof, and merge_rows() folds the
    'start' and 'end' rows for one run_id back together.
    """
    return append_journal(dict(fields, run_id=run_id, event="end"))


# ── the shell scripts' logs (read only) ───────────────────────────────────
def read_master_log():
    """Rows of master_experiment_log.csv, newest first."""
    path = paths.MASTER_CSV
    if not os.path.isfile(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    rows.sort(key=lambda r: _capture_unix(r), reverse=True)
    return rows


def read_failures():
    """Filename -> failure note, from capture.sh's master_failures.csv."""
    path = paths.MASTER_FAILURES_CSV
    if not os.path.isfile(path):
        return {}
    out = {}
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 3:
                    out[row[1]] = row[2]
    except OSError:
        pass
    return out


def _capture_unix(row):
    """Best available start time for a master-log row, as a unix float.

    New rows carry Capture_Start_Unix (capture.sh:337). Older rows predate that
    column and only have the local-time filename stamp — MAIN.py:402 flags the
    same limitation. Parsed as LOCAL time, which is what it is.
    """
    raw = (row.get("Capture_Start_Unix") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    stamp = (row.get("Timestamp") or "").strip()
    if stamp:
        try:
            return datetime.strptime(stamp, "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            pass
    return 0.0


def _status_for_capture(row, failures):
    """capture.sh's verdict, reconstructed for rows written before it existed."""
    status = (row.get("Capture_Status") or "").strip().upper()
    if status:
        return status
    if row.get("Filename") in failures:
        return "SHORT"
    return "UNKNOWN"      # pre-dates the Capture_Status column


# ── the merged timeline ───────────────────────────────────────────────────
class Row:
    """One line in the history table."""

    __slots__ = ("unix", "kind", "status", "title", "detail", "duration",
                 "size", "output_dir", "source", "raw")

    def __init__(self, unix, kind, status, title, detail="", duration=None,
                 size="", output_dir="", source="", raw=None):
        self.unix = unix
        self.kind = kind
        self.status = status
        self.title = title
        self.detail = detail
        self.duration = duration
        self.size = size
        self.output_dir = output_dir
        self.source = source
        self.raw = raw or {}

    def as_columns(self):
        return (
            fmt_berlin(self.unix, "%Y-%m-%d %H:%M"),
            KIND_LABELS.get(self.kind, self.kind),
            self.title,
            self.detail,
            fmt_duration(self.duration) if self.duration else "",
            self.size,
            self.status,
        )


def capture_rows():
    failures = read_failures()
    rows = []
    for r in read_master_log():
        unix = _capture_unix(r)
        freq = (r.get("Freq_MHz") or "").strip()
        sr = (r.get("SampleRate_MHz") or "").strip()
        exp = (r.get("Experiment") or "").strip()
        detail_bits = []
        if freq:
            detail_bits.append(f"{freq} MHz")
        if sr:
            detail_bits.append(f"{sr} MS/s")
        if exp and exp != "none":
            detail_bits.append(exp)
        note = failures.get(r.get("Filename", ""))
        if note:
            detail_bits.append(note)
        rows.append(Row(
            unix=unix,
            kind="capture",
            status=_status_for_capture(r, failures),
            title=r.get("Filename", "?"),
            detail=" · ".join(detail_bits),
            duration=r.get("Duration_Sec"),
            size=(r.get("File_Size") or "").strip(),
            output_dir=paths.DATA_DIR,
            source="master_experiment_log.csv",
            raw=r,
        ))
    return rows


def journal_rows():
    """GUI runs, with each run's 'start' and 'end' entries folded together."""
    by_id = {}
    order = []
    for entry in read_journal():
        rid = entry.get("run_id")
        if rid is None:
            continue
        if rid not in by_id:
            by_id[rid] = dict(entry)
            order.append(rid)
        else:
            by_id[rid].update({k: v for k, v in entry.items() if v is not None})

    rows = []
    for rid in order:
        e = by_id[rid]
        # A run that has a start but no end, and whose process is long gone,
        # was interrupted — say so rather than showing it as still running.
        status = e.get("status") or ("RUNNING" if e.get("event") == "start" else "")
        if status == "RUNNING" and time.time() - float(e.get("started_unix") or 0) > 86400:
            status = "INTERRUPTED"
        rows.append(Row(
            unix=float(e.get("started_unix") or e.get("logged_unix") or 0),
            kind=e.get("kind", "run"),
            status=status or "UNKNOWN",
            title=e.get("summary") or e.get("name", "run"),
            detail=e.get("detail", ""),
            duration=e.get("duration_sec"),
            size="",
            output_dir=e.get("output_dir", ""),
            source="sreto journal",
            raw=e,
        ))
    return rows


def merged_rows(include_captures=True, include_runs=True, kinds=None, limit=400):
    """The whole timeline, newest first.

    A GUI-launched capture appears ONCE: capture.sh's own master-log row is the
    authoritative one, so the journal's duplicate is dropped when a master-log
    row lands inside the run's window.
    """
    rows = []
    caps = capture_rows() if include_captures else []
    rows.extend(caps)

    if include_runs:
        cap_times = sorted(r.unix for r in caps)
        for row in journal_rows():
            if row.kind == "capture" and _has_capture_near(cap_times, row):
                continue
            rows.append(row)

    if kinds:
        rows = [r for r in rows if r.kind in kinds]
    rows.sort(key=lambda r: r.unix, reverse=True)
    return rows[:limit]


def _has_capture_near(sorted_times, row, slack=900.0):
    """Did capture.sh log a capture inside this GUI run's window?"""
    end = row.unix + (float(row.duration or 0) or 0) + slack
    lo, hi = row.unix - slack, end
    for t in sorted_times:
        if lo <= t <= hi:
            return True
        if t > hi:
            break
    return False


def stats(rows):
    """Headline numbers for the dashboard strip."""
    total = len(rows)
    by_status = {}
    by_kind = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
    last = rows[0].unix if rows else 0
    return {
        "total": total,
        "by_status": by_status,
        "by_kind": by_kind,
        "last_unix": last,
        "last_label": fmt_berlin(last, "%Y-%m-%d %H:%M") if last else "never",
    }


def data_volume_gb():
    """How much IQ is sitting in the capture directories right now."""
    total = 0
    for d in paths.data_dirs():
        try:
            for name in os.listdir(d):
                if name.endswith(".bin"):
                    try:
                        total += os.path.getsize(os.path.join(d, name))
                    except OSError:
                        pass
        except OSError:
            pass
    return total / 1e9
