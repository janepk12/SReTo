"""
run_tests.py — the validation suite, without pytest.

    python -m tests.run_tests        (from the repo root)
    sreto --check                    (same suite, via the console entry point)

Runs headless: no window is created, no radio is touched, no subprocess of the
pipeline is started. Safe to run while a capture is in progress.

Modules that re-derive their expectations from the science repository skip
themselves when no repository is configured — see each module's setUpModule.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
# The package lives under src/, so a source checkout that has NOT been
# pip-installed cannot import sreto from the project root alone. pytest gets
# this from `pythonpath = ["src"]` in pyproject.toml; `python -m tests.run_tests`
# has no such setting, and used to fail with ModuleNotFoundError before the
# first test ran. An installed copy already has sreto on sys.path and this
# entry is simply never consulted.
SRC_DIR = os.path.join(PROJECT_ROOT, "src")


def build_suite():
    for path in (PROJECT_ROOT, SRC_DIR):
        if path not in sys.path:
            sys.path.insert(0, path)
    loader = unittest.TestLoader()
    return loader.discover(start_dir=TESTS_DIR, pattern="test_*.py",
                           top_level_dir=PROJECT_ROOT)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(build_suite())
    if result.wasSuccessful():
        print("\nThe science repository is unmodified and every CLI contract "
              "still holds.")
        return 0
    print("\nVALIDATION FAILED — see the failures above. SReTo and the tools "
          "in the science repository have drifted apart.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
