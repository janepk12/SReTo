"""
MAIN.py parameterisation: correct, minimal, and read-only.

The risk being tested is specific. MAIN.py reassigns several of its own
parameter names further down the file — MAX_SAMPLES at :275, WINDOW_START_SEC
and WINDOW_END_SEC at :453 — as part of its engine logic. A substitution that
reached those lines would silently disable the speed and window handling. So
the tests check not just that the right lines changed, but that NOTHING ELSE did.
"""

import ast
import os
import unittest

from sreto import main_params, paths


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


def _read_main():
    with open(paths.MAIN_PY, encoding="utf-8") as f:
        return f.read()


class TestReadDefaults(unittest.TestCase):

    def test_every_spec_exists_in_main_py(self):
        defaults = main_params.read_defaults()
        missing = [name for name, *_ in main_params.PARAM_SPECS
                   if name not in defaults]
        self.assertEqual(missing, [],
                         f"gui/main_params.PARAM_SPECS names parameters MAIN.py "
                         f"no longer has: {missing}")

    def test_defaults_are_literals_not_expressions(self):
        defaults = main_params.read_defaults()
        for name, kind, *_ in main_params.PARAM_SPECS:
            value = defaults[name]
            if kind == "bool":
                self.assertIsInstance(value, bool, f"{name} is not a bool")
            elif kind in ("float", "int"):
                self.assertTrue(value is None or isinstance(value, (int, float)),
                                f"{name} is {type(value).__name__}, not numeric")

    def test_reading_defaults_does_not_import_main(self):
        """read_defaults() parses; it must never execute MAIN.py's pipeline."""
        import sys
        before = set(sys.modules)
        main_params.read_defaults()
        new = set(sys.modules) - before
        self.assertNotIn("MAIN", new)
        for heavy in ("numpy", "matplotlib", "scipy"):
            self.assertNotIn(heavy, new,
                             f"reading MAIN.py's defaults pulled in {heavy}")


class TestSubstitution(unittest.TestCase):

    def test_only_the_named_lines_change(self):
        overrides = {"PROCESS_PERCENTAGE": 0.25, "RUN_SIGNAL_XCORR": False,
                     "file_name": "test.bin", "MAX_SAMPLES": 1_000_000}
        original = _read_main().splitlines()
        result = main_params.build_overridden_source(overrides).splitlines()

        # The provenance header is prepended; strip it before comparing.
        body = result[main_params.HEADER_LINE_COUNT:]
        self.assertEqual(len(body), len(original),
                         "substitution changed the LINE COUNT of MAIN.py")

        differing = [i for i, (a, b) in enumerate(zip(original, body)) if a != b]
        self.assertEqual(len(differing), len(overrides),
                         f"expected {len(overrides)} changed lines, got "
                         f"{len(differing)} at {differing}")
        for i in differing:
            name = body[i].split("=")[0].strip()
            self.assertIn(name, overrides,
                          f"line {i + 1} changed but {name} was not an override")

    def test_engine_reassignments_are_left_alone(self):
        """MAX_SAMPLES and the window vars are reassigned by MAIN's engine."""
        result = main_params.build_overridden_source(
            {"MAX_SAMPLES": 42, "WINDOW_START_SEC": 1.0, "WINDOW_END_SEC": 2.0})
        self.assertIn("MAX_SAMPLES = min(MAX_SAMPLES, _frames)", result,
                      "the engine's MAX_SAMPLES clamp (MAIN.py:275) was "
                      "overwritten — PROCESS_PERCENTAGE would stop limiting the "
                      "IQ read")
        self.assertIn('WINDOW_START_SEC = float(_timing.get("window_start_sec"',
                      result,
                      "the engine's geometry-driven window override "
                      "(MAIN.py:453) was overwritten")

    def test_result_is_valid_python(self):
        source = main_params.build_overridden_source(
            {name: _sample_value(kind) for name, kind, *_ in main_params.PARAM_SPECS})
        ast.parse(source)          # raises on failure — that is the assertion

    def test_unknown_parameter_is_rejected(self):
        with self.assertRaises(main_params.MainParamError):
            main_params.build_overridden_source({"NOT_A_REAL_PARAMETER": 1})

    def test_blank_optional_becomes_none(self):
        source = main_params.build_overridden_source({"SATELLITE_QUERY": "  "})
        self.assertIn("SATELLITE_QUERY = None", source)

    def test_numeric_satellite_query_stays_an_int(self):
        source = main_params.build_overridden_source({"SATELLITE_QUERY": "43641"})
        self.assertIn("SATELLITE_QUERY = 43641", source)

    def test_named_satellite_query_is_quoted(self):
        source = main_params.build_overridden_source({"SATELLITE_QUERY": "NOAA-19"})
        self.assertIn("SATELLITE_QUERY = 'NOAA-19'", source)

    def test_booleans_render_as_python_literals(self):
        source = main_params.build_overridden_source(
            {"RUN_PHYSICS_EXTRACTION": True, "SAVE_PLOTS": False})
        self.assertIn("RUN_PHYSICS_EXTRACTION = True", source)
        self.assertIn("SAVE_PLOTS = False", source)


class TestRunCopy(unittest.TestCase):

    def test_copy_lands_outside_01_code_proper(self):
        path = main_params.write_run_copy({"PROCESS_PERCENTAGE": 0.5})
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.assertTrue(
            os.path.abspath(path).startswith(os.path.abspath(paths.GUI_STATE_DIR)),
            f"the generated copy was written to {path}, outside gui/.state/")

    def test_main_py_is_unchanged_by_writing_a_copy(self):
        before = _read_main()
        path = main_params.write_run_copy({"PROCESS_PERCENTAGE": 0.5})
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.assertEqual(before, _read_main(), "MAIN.py was modified")

    def test_copy_carries_a_provenance_header(self):
        path = main_params.write_run_copy({"PROCESS_PERCENTAGE": 0.5})
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, encoding="utf-8") as f:
            head = f.read(600)
        self.assertIn("GENERATED by 01_CODE/gui", head)
        self.assertIn("MAIN.py itself is unmodified", head)

    def test_old_copies_are_pruned(self):
        made = [main_params.write_run_copy({"PROCESS_PERCENTAGE": 0.1 * i})
                for i in range(1, 15)]
        self.addCleanup(lambda: [os.remove(p) for p in made if os.path.exists(p)])
        remaining = [f for f in os.listdir(paths.TMP_DIR)
                     if f.startswith("MAIN_gui_")]
        self.assertLessEqual(len(remaining), 12,
                             "generated copies are accumulating without bound")


def _sample_value(kind):
    return {"float": 0.5, "int": 3, "bool": True, "choice": "V",
            "path": "/tmp/out", "str": "sample"}.get(kind, "sample")


if __name__ == "__main__":
    unittest.main()
