"""
system_open.py — hand results to the desktop: open figures, reveal folders.

macOS and Linux differ in exactly the place the requirement cares about:
"if a window for that directory is already open, bring it to the foreground."

  * macOS: Finder is scriptable, so we ask it directly — walk the open windows,
    compare each one's target to ours, raise the match, and only make a new
    window when there is none. Then `activate` to bring Finder forward.
  * Linux: no equivalent exists across desktops. The common file managers
    (Nautilus, Dolphin, Nemo, Thunar) already reuse a window for a directory
    they are showing, so calling them directly gets the same behaviour where
    it is achievable; xdg-open is the last resort.

Everything here is fire-and-forget and never raises into the GUI: failing to
open a folder must not take down a session that just captured 3 GB of IQ.
"""

import os
import platform
import shutil
import subprocess

IS_MAC = platform.system() == "Darwin"

# Ordered by how well each reuses an existing window for the same directory.
_LINUX_MANAGERS = ("nautilus", "dolphin", "nemo", "thunar", "caja", "pcmanfm")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".pdf", ".svg")

_APPLESCRIPT_REVEAL = '''
on run argv
    set targetPath to item 1 of argv
    tell application "Finder"
        set tgt to (POSIX file targetPath) as alias
        set foundWindow to missing value
        repeat with w in (every Finder window)
            try
                if (target of w as alias) is tgt then
                    set foundWindow to w
                    exit repeat
                end if
            end try
        end repeat
        if foundWindow is missing value then
            set newWin to make new Finder window
            set target of newWin to tgt
            set index of newWin to 1
        else
            set index of foundWindow to 1
        end if
        activate
    end tell
end run
'''


def reveal_directory(path):
    """Show `path` in the system file browser, reusing its window if open.

    Returns (ok, message) — the message is worth printing to the console so the
    user can see which mechanism was used (or why nothing happened).
    """
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return False, f"not a directory: {path}"

    if IS_MAC:
        try:
            proc = subprocess.run(["osascript", "-", path],
                                  input=_APPLESCRIPT_REVEAL, text=True,
                                  capture_output=True, timeout=15)
            if proc.returncode == 0:
                return True, f"Finder -> {path}"
            # Automation permission denied, or Finder scripting unavailable.
            subprocess.Popen(["open", path])
            return True, (f"opened {path} with `open` "
                          f"(Finder scripting said: {proc.stderr.strip()[:120]})")
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not reveal {path}: {e}"

    for manager in _LINUX_MANAGERS:
        exe = shutil.which(manager)
        if exe:
            try:
                subprocess.Popen([exe, path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"{manager} -> {path}"
            except OSError:
                continue

    exe = shutil.which("xdg-open")
    if exe:
        try:
            subprocess.Popen([exe, path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"xdg-open -> {path}"
        except OSError as e:
            return False, f"xdg-open failed: {e}"
    return False, "no file manager found (install xdg-utils, or open it yourself)"


def open_paths(file_paths):
    """Open files in whatever the desktop uses for them. Returns (n_ok, msg)."""
    existing = [os.path.abspath(p) for p in file_paths if os.path.isfile(p)]
    if not existing:
        return 0, "nothing to open"

    if IS_MAC:
        try:
            # One `open` call keeps them in a single Preview window instead of
            # scattering a dozen of them across the screen.
            subprocess.Popen(["open"] + existing)
            return len(existing), f"opened {len(existing)} file(s)"
        except OSError as e:
            return 0, f"open failed: {e}"

    exe = shutil.which("xdg-open")
    if not exe:
        return 0, "no xdg-open found — install xdg-utils to auto-open figures"
    opened = 0
    for p in existing:
        try:
            subprocess.Popen([exe, p],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened += 1
        except OSError:
            pass
    return opened, f"opened {opened} file(s)"


def figures_written_since(directory, since_unix, exts=IMAGE_EXTS):
    """Figures in `directory` modified at/after `since_unix`, oldest first.

    Mirrors MAIN.py's own stale-output guard (MAIN.py:643): SAVE_DIR uses fixed
    filenames, so mtime is the only thing that distinguishes this run's output
    from an earlier run's leftovers.
    """
    if not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        if not name.lower().endswith(exts):
            continue
        p = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if mtime >= since_unix - 1.0:      # 1 s slack for filesystem timestamps
            out.append((mtime, p))
    return [p for _, p in sorted(out)]


def newest_captures(directories, limit=200):
    """(.bin, .json) capture pairs across the data directories, newest first."""
    found = []
    for d in directories:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".bin"):
                continue
            bin_path = os.path.join(d, name)
            json_path = bin_path[:-4] + ".json"
            if not os.path.exists(json_path):
                continue
            try:
                found.append((os.path.getmtime(bin_path), bin_path, json_path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [(b, j) for _, b, j in found[:limit]]
