"""
Shared skip conditions.

Two things a test can need that a machine may not have: the science repository
(most of the suite re-derives its expectations from the repository's own
scripts) and a Tk display (a handful of font/geometry tests).

Both are expressed as decorators rather than as `if` blocks inside the tests,
so a skipped test is REPORTED as skipped. A test that silently returns early
reads as a pass, which is exactly the "green by omission" failure this suite
exists to prevent.

Modules where *everything* needs the repository use a module-level
``setUpModule`` guard instead — see test_capture_contract.py.
"""

import unittest

from sreto import paths

SCIENCE_REPO_REASON = (
    "no science repository configured — set $SRETO_REPO_ROOT "
    "(see README: Roadmap to standalone)")

DISPLAY_REASON = "no Tk display available"


def have_science_repo():
    return paths.have_science_repo()


def have_display():
    """True when a Tk interpreter can actually be created."""
    try:
        import tkinter as tk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except Exception:                                    # noqa: BLE001
        return False
    root.destroy()
    return True


def requires_science_repo(obj):
    """Skip a test or TestCase unless a science repository is configured."""
    return unittest.skipUnless(have_science_repo(), SCIENCE_REPO_REASON)(obj)


def requires_display(obj):
    """Skip a test or TestCase unless a Tk display is available."""
    return unittest.skipUnless(have_display(), DISPLAY_REASON)(obj)
