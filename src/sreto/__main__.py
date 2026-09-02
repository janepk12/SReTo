"""
Entry point:  sreto   /   python -m sreto

The GUI needs no arguments. The flags exist because two things have to be
answerable *without* opening a window: where the science repository is, and
whether this installation is intact. A third reason, added later: a field
session over SSH has no window to open at all.

    sreto                          open the window
    sreto --check                  run the validation suite, no window, no radio
    sreto --where                  print the resolved paths and exit
    sreto --radios                 which SDRs are supported, and what is coming
    sreto --set-repo PATH          persist the science repository location
    sreto --version

    sreto --menu                   interactive terminal menu — capture,
                                    analysis, planner, pre-checks, status, over
                                    SSH with no display
    sreto --selftests              every self-check, exit code says pass/fail
    sreto --status                 paths, storage, plan age, recent runs
    sreto --logs                   list recent run / session / planner logs
    sreto --captures               list the captures this install can see
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
    p.add_argument("--menu", "--tui", action="store_true", dest="menu",
                   help="interactive terminal menu — capture, analysis, "
                        "planner, pre-checks, status, over SSH")
    p.add_argument("--selftests", action="store_true",
                   help="run every self-check and exit non-zero if any failed")
    p.add_argument("--status", action="store_true",
                   help="paths, storage, plan age and recent runs, then exit")
    p.add_argument("--logs", action="store_true",
                   help="list the most recent run / session / planner logs")
    p.add_argument("--captures", action="store_true",
                   help="list the captures this install can see, then exit")
    return p


def _print_summary():
    from . import config
    width = max(len(label) for label, _ in config.summary())
    for label, value in config.summary():
        print(f"  {label:<{width}} : {value}")
    if not config.is_configured():
        print()
        print(config.missing_repo_message())


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

    # The terminal front-end. No Tk import here, unlike the window path below —
    # that is the whole point: these run over SSH with no display.
    if args.menu:
        from . import tui
        return tui.run_menu()
    if args.selftests:
        from . import tui
        return tui.run_tests()
    if args.status:
        from . import tui
        return tui.run_status()
    if args.logs:
        from . import tui
        return tui.run_logs()
    if args.captures:
        from . import tui
        return tui.run_captures()

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
