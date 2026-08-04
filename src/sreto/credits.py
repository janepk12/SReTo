"""
credits.py — the small "About" the unlocked GUI keeps in its corner.

Same information as the lock screen, plus the parts that are only interesting
once you are working: which interpreter is running the analysis, where the
outputs go, and the non-destructive guarantee with the command that verifies it.
"""

import platform
import sys
import tkinter as tk
from tkinter import ttk

from . import branding, config, paths, theme, widgets


def show(app):
    """Open the credits window. One at a time."""
    existing = getattr(app, "_credits_window", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_force()
        return existing

    win = tk.Toplevel(app.root)
    app._credits_window = win
    win.title(f"About {branding.APP_NAME}")
    win.configure(background=theme.BG)
    win.transient(app.root)
    win.geometry("660x620")
    win.minsize(520, 420)

    header = tk.Frame(win, background=theme.PANEL)
    header.pack(fill="x")
    inner = tk.Frame(header, background=theme.PANEL)
    inner.pack(padx=24, pady=20, anchor="w")

    tk.Label(inner, text=branding.APP_LONG_NAME, background=theme.PANEL,
             foreground=theme.TEXT,
             font=theme.F.credits_title).pack(anchor="w")
    tk.Label(inner, text=branding.APP_TAGLINE, background=theme.PANEL,
             foreground=theme.MUTED,
             font=theme.F.ui).pack(anchor="w")
    tk.Label(inner, text=f"{branding.author()} · {branding.institution()}",
             background=theme.PANEL, foreground=theme.C1,
             font=theme.F.ui_bold).pack(anchor="w",
                                                                    pady=(10, 0))

    scroll = widgets.ScrollFrame(win)
    scroll.pack(fill="both", expand=True, padx=18, pady=14)
    body = scroll.inner

    for heading, text in branding.credit_rows() + _runtime_rows():
        if not heading and not text:
            tk.Frame(body, background=theme.BORDER, height=1).pack(
                fill="x", pady=9)
            continue
        row = ttk.Frame(body)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=heading, style="Muted.TLabel", width=15,
                  anchor="nw").pack(side="left", anchor="n")
        ttk.Label(row, text=text, style="TLabel", wraplength=430,
                  justify="left").pack(side="left", fill="x", expand=True)

    footer = ttk.Frame(win, padding=(18, 10))
    footer.pack(fill="x")
    ttk.Label(footer,
              text="SReTo never modifies the science repository — "
                   "verify with  sreto --check",
              style="Muted.TLabel").pack(side="left")
    ttk.Button(footer, text="Close", command=win.destroy).pack(side="right")

    win.bind("<Escape>", lambda _e: win.destroy())
    return win


def _runtime_rows():
    return [
        ("", ""),
        ("Interpreter", f"{sys.version.split()[0]} — {paths.python_executable()}"),
        ("Platform", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        ("Science repo", paths.REPO_ROOT if paths.have_science_repo()
                         else "NOT CONFIGURED — see $SRETO_REPO_ROOT"),
        ("Resolved from", config.repo_source()),
        ("Captures", paths.rel(paths.DATA_DIR)),
        ("Figures", paths.rel(paths.ANALYSIS_DIR)),
        ("SoOp plans", paths.rel(paths.SOOP_DIR)),
        ("SReTo state", paths.GUI_STATE_DIR),
        ("Assets", _assets_note()),
    ]


def _assets_note():
    where = branding.USER_ASSETS_DIR
    logo = ("gfz_logo.png present" if branding.logo_path()
            else f"drop gfz_logo.png into {where} to replace the placeholder")
    icon = ("app_icon.png present" if branding.app_icon_path()
            else f"drop app_icon.png into {where} for the desktop shortcut")
    return f"{logo}\n{icon}"
