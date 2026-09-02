"""
The dual-mode guarantee, tested.

SReTo is only allowed to READ the science repository. This module proves it by
hashing every file in 01_CODE (excluding machine-generated caches), running
SReTo's whole non-hardware surface — parameter parsing, job building, MAIN.py
transpiling, history reading, availability providers, pre-checks — and hashing
again. Any difference fails, with the changed path named.

It also checks that the files and entry points SReTo depends on still exist, so
a rename in 01_CODE surfaces here instead of as a dead button.

Since SReTo moved out of the science repo, this test got STRICTER rather than
weaker: the package is no longer inside the tree being hashed, so there is no
longer a directory that has to be excluded from the guarantee.
"""

import ast
import hashlib
import os
import unittest

from sreto import (
    config,
    history,
    jobs,
    location,
    main_params,
    paths,
    prechecks,
    radio_settings,
    soop_availability,
    system_open,
)


def setUpModule():
    """Skip the whole module when no science repository is configured.

    These tests re-derive their expectations FROM the science repo's own
    scripts — that is the point of them. Without the repo there is nothing to
    compare against, and a green run would be green by omission. Recognised by
    both `pytest` and `python -m unittest`.
    """
    if not paths.have_science_repo():
        raise unittest.SkipTest(
            "no science repository configured — set $SRETO_REPO_ROOT "
            "(see README: Roadmap to standalone)")


# Written by the pipeline at runtime, or by the OS. Not part of the contract.
EXCLUDED_NAMES = {".DS_Store", ".tle_cache.json"}
EXCLUDED_DIRS = {"__pycache__", ".git"}

# Named by ROLE, not by path: the science repo supports two layouts and each
# of these sits in a different place depending on which one is in front of us.
# paths.* already resolved that, so ask it rather than re-deriving it here.
def required_files():
    """Absolute paths of every science-repo file SReTo drives, this layout."""
    scripts = (paths.CAPTURE_SH, paths.SOOP_CAPTURE_SH)
    pipeline = ("soop_planner.py", "console.py", "waterfalls.py",
                "iq_dashboard.py", "physics.py", "orbits.py", "fresnel.py",
                "satellites.py", "sdr_core.py", "band_correlator.py")
    return (list(scripts)
            + [paths.MAIN_PY, paths.GEOMETRY_JSON]
            + [os.path.join(paths.ANALYSIS_SRC_DIR, f) for f in pipeline])


def hash_code_dir():
    """{relative path: sha256} for every tracked file in 01_CODE."""
    manifest = {}
    for root, dirs, files in os.walk(paths.CODE_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in sorted(files):
            if name in EXCLUDED_NAMES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, paths.CODE_DIR)
            try:
                with open(full, "rb") as f:
                    manifest[rel] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                manifest[rel] = "<unreadable>"
    return manifest


def exercise_gui_read_paths():
    """Every GUI code path that does not need the radio or a window.

    Deliberately broad: this is the payload the hash test wraps, so anything it
    fails to touch is something the test does not actually cover.
    """
    main_params.read_defaults()
    main_params.build_overridden_source({"PROCESS_PERCENTAGE": 0.1})
    main_params.write_run_copy({"PROCESS_PERCENTAGE": 0.1, "file_name": "x.bin"})

    jobs.capture_job({})
    jobs.capture_job({"freq": "1621.25", "agc_rx1": "on", "capture_mode": "1",
                      "amount": "5"})
    jobs.autocapture_job({})
    jobs.autocapture_job({"sat": "IRIDIUM", "next_only": True, "run_mode": "list"})
    jobs.planner_job({"mode": "now_plus_24h"})
    jobs.analysis_job({"file_name": "x.bin", "PROCESS_PERCENTAGE": 0.1}, nice=10)
    jobs.estimate_capture({"samplerate": "10", "amount": "120"})

    history.merged_rows()
    history.stats(history.merged_rows())
    history.data_volume_gb()

    for provider in soop_availability.providers():
        provider.available()
        soop_availability.summarise(provider.passes(horizon_h=24))

    prechecks.run_all(probe_radio=False)      # no USB, no subprocess
    system_open.newest_captures(paths.data_dirs())

    # The shared radio settings PARSE soop_capture.sh and the location module
    # reads the plan header and geometry.json — both read pipeline files that
    # they must never write back to.
    radio_settings.script_defaults()
    radio_settings.effective()
    radio_settings.auto_overrides()
    radio_settings.summary_line()
    location.from_plan_or_geometry()
    location.resolve("plan")


class TestCodeDirUntouched(unittest.TestCase):

    def test_gui_never_modifies_01_code(self):
        before = hash_code_dir()
        exercise_gui_read_paths()
        after = hash_code_dir()

        changed = sorted(k for k in before.keys() & after.keys()
                         if before[k] != after[k])
        removed = sorted(before.keys() - after.keys())
        added = sorted(after.keys() - before.keys())

        self.assertEqual(changed, [], f"GUI MODIFIED files in 01_CODE: {changed}")
        self.assertEqual(removed, [], f"GUI DELETED files in 01_CODE: {removed}")
        self.assertEqual(added, [], f"GUI CREATED files in 01_CODE: {added}")

    def test_required_files_present(self):
        missing = sorted(os.path.relpath(f, paths.REPO_ROOT)
                         for f in required_files() if not os.path.isfile(f))
        self.assertEqual(
            missing, [],
            f"the science repo ({paths.SCIENCE_LAYOUT} layout) is missing "
            f"files SReTo drives: {missing}")

    def test_sreto_lives_outside_the_science_repo(self):
        """The package must not sit inside the tree it promises not to touch.

        This replaces the in-tree version's "gui/ is a child of 01_CODE" check
        and inverts it: SReTo is now a separate, installable package, and a
        checkout that ended up back inside the science repo would put its own
        files into the hash above and quietly weaken the guarantee.
        """
        package = os.path.realpath(paths.PACKAGE_DIR)
        repo = os.path.realpath(paths.REPO_ROOT)
        self.assertFalse(
            package.startswith(repo + os.sep),
            f"the sreto package is inside the science repo ({package})")

    def test_sreto_writes_only_into_its_own_state_dir(self):
        """Every SReTo-owned write target sits under the state directory."""
        for target in (paths.RUN_JOURNAL, paths.PRESETS_JSON, paths.TMP_DIR,
                       paths.GUI_LOG_DIR, location.CACHE_PATH):
            self.assertTrue(
                os.path.abspath(target).startswith(
                    os.path.abspath(paths.GUI_STATE_DIR)),
                f"{target} is outside SReTo's state directory")

    def test_state_dir_is_outside_the_science_repo(self):
        """The state directory must never land inside the repo being hashed."""
        state = os.path.realpath(config.state_dir())
        repo = os.path.realpath(paths.REPO_ROOT)
        self.assertFalse(
            state == repo or state.startswith(repo + os.sep),
            f"SReTo would write into the science repo ({state})")

    def test_no_gui_module_opens_a_pipeline_file_for_writing(self):
        """Static scan: no open(<pipeline path constant>, 'w'|'a') anywhere.

        The hash test above covers the paths it exercises; this covers the ones
        it does not, by reading the GUI's own source.
        """
        forbidden = {"MAIN_PY", "CAPTURE_SH", "SOOP_CAPTURE_SH", "SOOP_PLANNER_PY",
                     "GEOMETRY_JSON", "CODE_DIR", "MASTER_CSV", "MASTER_JSONL",
                     "MASTER_FAILURES_CSV", "PLAN_TSV"}
        offenders = []

        for py_file in _gui_python_files():
            with open(py_file, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=py_file)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "open"):
                    continue
                mode = _literal_mode(node)
                if mode is None or not any(c in mode for c in "wax+"):
                    continue
                name = _referenced_paths_attr(node.args[0] if node.args else None)
                if name in forbidden:
                    offenders.append(
                        f"{os.path.relpath(py_file, paths.GUI_DIR)}:{node.lineno} "
                        f"opens paths.{name} with mode {mode!r}")

        self.assertEqual(offenders, [],
                         "GUI code opens pipeline files for writing:\n  "
                         + "\n  ".join(offenders))


def _gui_python_files():
    for root, dirs, files in os.walk(paths.GUI_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".state")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _literal_mode(call):
    """The mode string of an open() call, or 'r' when it is defaulted."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return "r" if not call.args or len(call.args) == 1 else None


def _referenced_paths_attr(node):
    """'MAIN_PY' for `paths.MAIN_PY`, else None."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "paths":
            return node.attr
    return None


if __name__ == "__main__":
    unittest.main()
