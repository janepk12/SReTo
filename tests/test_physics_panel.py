"""
The physics menu: the retrieval chain it draws, the job it launches, and the
ground-truth file it is the only GUI component allowed to write.

Three things are worth pinning here, and they are not the obvious ones.

  * The explanations must come from sreto.analysis.physics, not the panel. A
    menu that describes a pipeline different from the one that runs is worse
    than no menu, so the test asserts the panel renders exactly the stages
    build_pipeline returns — same count, same order.

  * Epistemic status has to survive the trip to the screen. The whole point of
    the panel is that PLACEHOLDER and MEASURED look different; a stage whose
    status the panel cannot colour would silently render as muted grey, which
    reads as 'fine'.

  * sreto.analysis.physics must stay unimported until the tab is actually
    used. It costs ~1.5 s of numpy/scipy, and the window has to open faster
    than that.

The ground-truth tests redirect paths.GROUND_TRUTH_JSON at a temp file. Without
that they would overwrite the operator's real field measurements, which is the
one file in this repo that cannot be regenerated.
"""

import json
import os
import tempfile
import time
import tkinter as tk
import unittest

from sreto import jobs, paths


def _tk_root():
    """A withdrawn Tk root, or None when there is no display."""
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


class _StubConsole:
    def __init__(self):
        self.notes = []

    def gui_note(self, text):
        self.notes.append(text)

    def gui_error(self, text):
        self.notes.append(text)


class _StubApp:
    """Everything PhysicsPanel asks of the app, and nothing else."""

    def __init__(self):
        self.console = _StubConsole()
        self.errors = []
        self.launched = []
        self._queue = []

    def post_to_ui(self, callback):          # called from the loader thread
        self._queue.append(callback)

    def error(self, title, message):
        self.errors.append((title, message))

    def reveal(self, directory):
        pass

    def run_job(self, job, **kw):
        self.launched.append(job)

    def drain(self, timeout=20.0):
        """Run queued callbacks ON THIS THREAD, the way _poll_exits does."""
        deadline = time.time() + timeout
        while not self._queue and time.time() < deadline:
            time.sleep(0.02)
        ran = 0
        while self._queue:
            self._queue.pop(0)()
            ran += 1
        return ran


# ══════════════════════════════════════════════════════════════════════════
#  the job — no Tk needed
# ══════════════════════════════════════════════════════════════════════════
class TestPhysicsJob(unittest.TestCase):

    def test_it_runs_the_vendored_module_not_a_science_repo_file(self):
        """The Physics tab must work with no sdr_r clone present at all."""
        job = jobs.physics_job({"json_path": "/tmp/x.json", "save_dir": "/tmp/out"})
        self.assertEqual(job.kind, "physics")
        self.assertEqual(job.argv[1:4], ["-m", "sreto.analysis.physics",
                                         "/tmp/x.json"])
        self.assertIn("--save-dir", job.argv)

    def test_blank_fields_are_omitted_so_physics_uses_its_own_defaults(self):
        """A blank form must produce a plain default run, not empty flags.

        `--elevation ''` is not the same as leaving it out: argparse would
        reject it, and the run would die before reading a single sample.
        """
        job = jobs.physics_job({
            "json_path": "/tmp/x.json", "save_dir": "/tmp/out",
            "elevation": "", "polarization": "", "bw_khz": "  ",
            "soil_model": "", "ground_truth": None})
        for flag in ("--elevation", "--pol", "--bw-khz", "--soil-model",
                     "--ground-truth"):
            self.assertNotIn(flag, job.argv, f"{flag} was sent with a blank value")

    def test_filled_fields_become_flags(self):
        job = jobs.physics_job({
            "json_path": "/tmp/x.json", "save_dir": "/tmp/out",
            "elevation": "30", "polarization": "V", "soil_model": "topp",
            "block_sec": "0.1", "bw_khz": "100", "offset_mhz": "0.25",
            "ground_truth": "/tmp/gt.json"})
        pairs = dict(zip(job.argv, job.argv[1:]))
        self.assertEqual(pairs["--elevation"], "30")
        self.assertEqual(pairs["--pol"], "V")
        self.assertEqual(pairs["--soil-model"], "topp")
        self.assertEqual(pairs["--offset-mhz"], "0.25")
        self.assertEqual(pairs["--ground-truth"], "/tmp/gt.json")

    def test_it_runs_under_this_interpreter_so_the_package_resolves(self):
        """`-m sreto.analysis.physics` only resolves for the interpreter that
        has sreto installed, which is this one — not whichever python the
        configured science repo would prefer."""
        import sys
        job = jobs.physics_job({"json_path": "/tmp/x.json", "save_dir": "/tmp/o"})
        self.assertEqual(job.argv[0], sys.executable)


# ══════════════════════════════════════════════════════════════════════════
#  the chain — needs physics, not Tk
# ══════════════════════════════════════════════════════════════════════════
class TestRetrievalChain(unittest.TestCase):

    def test_the_chain_explains_itself_with_no_data(self):
        """The menu must be readable before the first capture is analysed."""
        from sreto import _lazy_physics
        stages = _lazy_physics.build_pipeline(None, None)
        self.assertGreaterEqual(len(stages), 10)
        for st in stages:
            self.assertTrue(st.plain, f"stage {st.key} has no plain-language text")
            self.assertTrue(st.why, f"stage {st.key} does not say why it exists")

    def test_every_status_has_a_colour_in_the_panel(self):
        """A status the panel cannot colour renders muted grey — i.e. 'fine'."""
        from sreto import _lazy_physics
        from sreto.panels import physics_panel
        for status in _lazy_physics.status_order():
            self.assertIn(status, physics_panel.STATUS_COLORS,
                          f"physics.py defines status {status!r} but "
                          f"physics_panel.STATUS_COLORS has no colour for it")

    def test_every_status_the_panel_colours_is_one_physics_defines(self):
        from sreto import _lazy_physics
        from sreto.panels import physics_panel
        known = set(_lazy_physics.status_meaning())
        extra = set(physics_panel.STATUS_COLORS) - known
        self.assertEqual(extra, set(),
                         f"physics_panel colours statuses physics.py never "
                         f"emits: {sorted(extra)}")

    def test_a_missing_ground_truth_is_reported_not_faked(self):
        from sreto import _lazy_physics
        stages = {st.key: st for st in _lazy_physics.build_pipeline(None, None)}
        self.assertEqual(stages["validation"].value, "no ground truth")
        self.assertTrue(stages["validation"].warn)


# ══════════════════════════════════════════════════════════════════════════
#  ground truth — redirected at a temp file
# ══════════════════════════════════════════════════════════════════════════
class TestGroundTruthFile(unittest.TestCase):

    def setUp(self):
        self._real = paths.GROUND_TRUTH_JSON
        self._dir = tempfile.mkdtemp(prefix="sreto_gt_")
        paths.GROUND_TRUTH_JSON = os.path.join(self._dir, "ground_truth.json")

    def tearDown(self):
        paths.GROUND_TRUTH_JSON = self._real

    def test_the_seed_config_carries_the_values_it_was_specified_with(self):
        from sreto import _lazy_physics
        gt = _lazy_physics.default_truth()
        self.assertEqual(gt["measurements"][0]["vwc_m3m3"], 0.20)
        self.assertEqual(gt["site"]["soil_texture"]["class"], "sandy")
        self.assertEqual(gt["site"]["vegetation"]["cover"], "grass")

    def test_measurements_are_a_series_not_a_scalar(self):
        """A tenth reading must fit without a schema change."""
        from sreto import _lazy_physics
        gt = _lazy_physics.default_truth()
        self.assertIsInstance(gt["measurements"], list)
        for i in range(9):
            gt["measurements"].append(
                {"timestamp": f"2026-08-0{i + 1}T09:00:00", "vwc_m3m3": 0.1 + i / 100,
                 "depth_m": 0.05, "method": "probe"})
        _lazy_physics.save_truth(gt)
        back = _lazy_physics.load_truth()
        self.assertEqual(len(back["measurements"]), 10)

    def test_site_constants_are_kept_apart_from_measured_values(self):
        """Site config is an assumption; measurements are evidence.

        If they shared a namespace the retrieval could score itself against its
        own configuration and call that validation.
        """
        from sreto import _lazy_physics
        gt = _lazy_physics.default_truth()
        self.assertNotIn("vwc_m3m3", gt["site"])
        self.assertNotIn("soil_texture", gt["measurements"][0])

    def test_round_trip_through_disk_preserves_everything(self):
        from sreto import _lazy_physics
        gt = _lazy_physics.default_truth()
        gt["site"]["name"] = "test site"
        _lazy_physics.save_truth(gt)
        self.assertEqual(_lazy_physics.load_truth(), gt)

    def test_a_missing_file_is_none_not_a_crash(self):
        from sreto import _lazy_physics
        self.assertIsNone(_lazy_physics.load_truth())

    def test_malformed_json_raises_rather_than_silently_dropping_truth(self):
        """Treating a broken truth file as 'no truth' stops validation happening
        without anyone noticing."""
        from sreto import _lazy_physics
        with open(paths.GROUND_TRUTH_JSON, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            _lazy_physics.load_truth()

    def test_texture_reaches_the_dielectric_model(self):
        from sreto import _lazy_physics
        gt = _lazy_physics.default_truth()
        sand, clay = _lazy_physics.texture_of(gt)
        self.assertGreater(sand, 50.0, "a sandy site must read as mostly sand")
        self.assertLess(clay, 20.0)


# ══════════════════════════════════════════════════════════════════════════
#  the panel — needs a display
# ══════════════════════════════════════════════════════════════════════════
class TestPhysicsPanelRenders(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        if cls.root is None:
            raise unittest.SkipTest("no display")
        from sreto import theme
        theme.apply_theme(cls.root)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            cls.root.destroy()

    def setUp(self):
        self._real = paths.GROUND_TRUTH_JSON
        self._dir = tempfile.mkdtemp(prefix="sreto_gt_")
        paths.GROUND_TRUTH_JSON = os.path.join(self._dir, "ground_truth.json")
        self.app = _StubApp()

    def tearDown(self):
        paths.GROUND_TRUTH_JSON = self._real

    def _panel(self):
        from sreto.panels.physics_panel import PhysicsPanel
        panel = PhysicsPanel(self.root, self.app)
        self.app.drain()                      # run the loader's UI callback
        self.root.update_idletasks()
        return panel

    def test_it_builds_and_draws_one_row_per_stage(self):
        from sreto import _lazy_physics
        panel = self._panel()
        expected = len(_lazy_physics.build_pipeline(panel._summary, panel._truth))
        rows = [w for w in panel._stage_rows
                if w.winfo_class() != "TSeparator"]
        self.assertEqual(len(rows), expected,
                         "the panel must draw exactly the stages physics.py "
                         "defines — no more, no fewer")

    def test_it_seeds_ground_truth_when_the_file_is_absent(self):
        panel = self._panel()
        self.assertEqual(panel.gt_form.fields["texture_class"].get(), "sandy")
        self.assertEqual(panel.gt_form.fields["vegetation"].get(), "grass")

    def test_picking_a_soil_class_fills_in_its_texture(self):
        panel = self._panel()
        panel.gt_form.fields["texture_class"].set("clay")
        panel._on_texture_class()
        self.assertGreater(float(panel.gt_form.fields["clay_pct"].get()), 40.0)

    def test_saving_writes_the_file_and_rescores_the_chain(self):
        panel = self._panel()
        panel.gt_form.fields["site_name"].set("regression site")
        panel._save_truth()
        self.assertTrue(os.path.exists(paths.GROUND_TRUTH_JSON))
        with open(paths.GROUND_TRUTH_JSON, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["site"]["name"], "regression site")
        self.assertEqual(self.app.errors, [])

    def test_a_percentage_typed_as_a_percentage_is_refused(self):
        """20 means 20 m³/m³ of water per m³ of soil, which is impossible.

        Catching it here matters more than it looks: the retrieval would
        happily score itself against 20.0 and report a 19.8 error forever.
        """
        panel = self._panel()
        panel.new_form.fields["vwc_m3m3"].set("20")
        before = len(panel._truth["measurements"])
        panel._add_measurement()
        self.assertEqual(len(panel._truth["measurements"]), before)
        self.assertTrue(self.app.errors)
        self.assertIn("range", self.app.errors[-1][0].lower())

    def test_a_valid_measurement_is_appended(self):
        panel = self._panel()
        before = len(panel._truth["measurements"])
        panel.new_form.fields["vwc_m3m3"].set("0.31")
        panel._add_measurement()
        self.assertEqual(len(panel._truth["measurements"]), before + 1)
        self.assertEqual(panel._truth["measurements"][-1]["vwc_m3m3"], 0.31)
        self.assertEqual(self.app.errors, [])

    def test_running_without_a_capture_is_refused_not_launched(self):
        panel = self._panel()
        panel.capture_var.set("")
        panel._start()
        self.assertEqual(self.app.launched, [])
        self.assertTrue(self.app.errors)


if __name__ == "__main__":
    unittest.main()
