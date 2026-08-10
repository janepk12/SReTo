"""
Entry point:  sreto   /   python -m sreto

The GUI needs no arguments. The flags exist because two things have to be
answerable *without* opening a window: where the science repository is, and
whether this installation is intact.

    sreto                          open the window
    sreto --check                  run the validation suite, no window, no radio
    sreto --where                  print the resolved paths and exit
    sreto --radios                 which SDRs are supported, and what is coming
    sreto --set-repo PATH          persist the science repository location
    sreto --version
"""

import argparse
import os
import sys

# The macOS .app bundle launches an interpreter COPY inside Contents/MacOS —
# that is what gives the process a bundle identity, and so a Location Services
# permission (see location.py) — and points it at the real environment's stdlib
# with PYTHONHOME. This interpreter read that variable at startup and will never
# need it again, but left in the environment it follows every subprocess out:
# `conda run`, soop_capture.sh's `python3` fallback, any system python — all of
# which would then be forced onto the WRONG stdlib. Drop it before SReTo spawns
# anything. paths.python_executable() hands out the real interpreter instead.
os.environ.pop("PYTHONHOME", None)


def _parser():
    p = argparse.ArgumentParser(
        prog="sreto",
        description="SReTo — SDR Reflectometry Toolkit (plan, capture, analyse).")
    p.add_argument("--check", action="store_true",
                   help="run the validation suite headless and exit")
    p.add_argument("--where", action="store_true",
                   help="print the resolved science repo and state paths")
    p.add_argument("--radios", action="store_true",
                   help="list the supported SDRs, the ones coming soon, and "
                        "the host command each one is driven with")
    p.add_argument("--set-repo", metavar="PATH",
                   help="persist the science repository location and exit")
    p.add_argument("--version", action="store_true", help="print the version")
    return p


def _print_summary():
    from . import config
    width = max(len(label) for label, _ in config.summary())
    for label, value in config.summary():
        print(f"  {label:<{width}} : {value}")
    if not config.is_configured():
        print()
        print(config.missing_repo_message())


def _report_fonts():
    """Which fonts the window would actually use, from a hidden throwaway root.

    Worth a line in --check because a Tk without Xft produces a GUI that looks
    broken while working perfectly, and no error is raised anywhere. Silent
    when there is no display: --check is meant to be safe over SSH.

    The root is withdrawn before anything is drawn, so no window flashes and
    macOS gets no Dock icon. Any failure at all is caught, not just TclError:
    "no display" reaches Python as TclError on X11 but as several different
    things on macOS depending on how the process was launched, and a --check
    that crashes while REPORTING on the install is worse than one that skips
    a cosmetic line.
    """
    import tkinter as tk

    from . import theme

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        theme.probe_fonts(root)
    except Exception:                                     # noqa: BLE001
        print("fonts    : no display — cannot measure (this is fine headless)")
        return
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:                             # noqa: BLE001
                pass

    for label, value in theme.rendering_summary():
        print(f"{label:<9}: {value}")
    if not theme.unicode_text:
        import textwrap
        print()
        for line in textwrap.wrap(f"WARNING: {theme.XFT_HINT}", 76):
            print(f"  {line}")


def _self_check():
    """Import every module, report the environment, and run the suite if present.

    The test suite is NOT installed with the package (tests/ is a top-level
    directory, not a subpackage), so an installed copy can only self-check by
    importing. From a source checkout the real suite is available and is run.
    """
    import importlib

    modules = [
        "config", "paths", "theme", "widgets", "history", "jobs", "prechecks",
        "main_params", "radio_settings", "radios", "soop_availability", "skyview",
        "skymap", "location", "system_open", "diagnostics", "runner",
        "ansi_console", "status", "branding", "credits", "header",
        "lockscreen", "make_icon", "_lazy_orbits",
        "panels.analysis_panel", "panels.automation_panel",
        "panels.availability_panel", "panels.capture_panel",
        "panels.history_panel", "panels.precheck_panel",
    ]
    failed = []
    for name in modules:
        try:
            importlib.import_module(f"sreto.{name}")
        except Exception as e:                        # noqa: BLE001
            failed.append(f"  sreto.{name}: {type(e).__name__}: {e}")

    print(f"imports  : {len(modules) - len(failed)}/{len(modules)} ok")
    for line in failed:
        print(line)

    try:
        importlib.import_module("tkinter")
        print("tkinter  : available")
    except ImportError:
        print("tkinter  : MISSING — the GUI cannot open. See the README.")
        failed.append("  tkinter")
    else:
        _report_fonts()

    print()
    _print_summary()

    try:
        suite = importlib.import_module("tests.run_tests")
    except ImportError:
        print("\nsuite    : not installed (run `pytest` from a source checkout)")
        return 1 if failed else 0

    print()
    return suite.main() or (1 if failed else 0)


def main(argv=None):
    args = _parser().parse_args(argv)

    if args.version:
        from . import __version__
        print(f"sreto {__version__}")
        return 0

    if args.set_repo:
        from . import config, paths
        root = config.set_repo_root(args.set_repo)
        paths.refresh()
        print(f"science repo set to: {root}")
        print(f"written to         : {config.config_path()}")
        if not config.is_configured():
            print()
            print(config.missing_repo_message())
            return 1
        return 0

    if args.radios:
        from . import radios
        print(radios.format_table())
        return 0

    if args.where:
        from . import config
        _print_summary()
        return 0 if config.is_configured() else 1

    if args.check:
        return _self_check()

    from . import config
    if not config.is_configured():
        # Not fatal: the window opens and explains itself. A user who ran the
        # command in a terminal deserves the same explanation there.
        print(config.missing_repo_message(), file=sys.stderr)
        print(file=sys.stderr)

    from .app import main as run_app  # noqa: PLC0415 — Tk import is heavy
    run_app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
