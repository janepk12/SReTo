"""
The shared-radio-settings contract, re-derived from soop_capture.sh itself.

The bug this defends against is not a crash — it is a capture that ran at a
bandwidth nobody chose. Three properties matter:

  1. the values the GUI DISPLAYS as "fixed in soop_capture.sh" are actually the
     ones in soop_capture.sh, parsed from it, not copied into Python;
  2. only parameters the script can actually be told about are forwarded, so
     the GUI never shows a setting it cannot deliver;
  3. an rx1/rx2 gain split is NOT silently flattened onto both chains, because
     that would corrupt the direct/reflected ratio the experiment measures.
"""

import json
import os
import re
import tempfile
import unittest

from sreto import jobs, paths
from sreto import radio_settings as rs


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


class TestScriptParsing(unittest.TestCase):

    def test_the_fixed_settings_are_read_from_the_script(self):
        defaults = rs.script_defaults()
        self.assertTrue(defaults, "soop_capture.sh's settings block did not parse")
        for var in ("CHANNELS", "BITMODE", "BIASTEE", "AGC_RX1", "AGC_RX2",
                    "RX1_GAIN", "RX2_GAIN", "ANTENNA_INFO"):
            self.assertIn(var, defaults,
                          f"soop_capture.sh no longer defines {var} at column 0 "
                          f"— the GUI would display a stale value for it")

    def test_parsed_values_match_the_file(self):
        """Compare against an independent read of the script."""
        with open(paths.SOOP_CAPTURE_SH, encoding="utf-8") as f:
            source = f.read()
        for var in ("CHANNELS", "BITMODE", "RX1_GAIN"):
            m = re.search(rf'^{var}="?([^"\n#]*)"?', source, re.MULTILINE)
            self.assertIsNotNone(m)
            self.assertEqual(rs.script_value(var), m.group(1).strip())

    def test_origin_points_at_a_real_line(self):
        origin = rs.script_origin("RX1_GAIN")
        self.assertTrue(origin.startswith("soop_capture.sh:"), origin)
        line_no = int(origin.split(":")[1])
        with open(paths.SOOP_CAPTURE_SH, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertTrue(lines[line_no - 1].startswith("RX1_GAIN="),
                        f"line {line_no} is not the RX1_GAIN assignment")

    def test_indented_assignments_are_ignored(self):
        """Only the settings block counts — assignments inside functions are
        per-pass working state and would be mistaken for defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fake.sh")
            with open(path, "w") as f:
                f.write('CHANNELS="1,2"\n')
                f.write('split_row() {\n  CHANNELS="9,9"\n}\n')
            rs._cache["mtime"] = None
            values = rs.script_defaults(path)
            self.assertEqual(values["CHANNELS"][0], "1,2")
        rs._cache["mtime"] = None          # drop the fake from the cache


class TestForwarding(unittest.TestCase):

    def test_only_flags_the_script_accepts_are_forwarded(self):
        with open(paths.SOOP_CAPTURE_SH, encoding="utf-8") as f:
            source = f.read()
        accepted = set()
        for m in re.finditer(r"^\s*(--[a-z-]+(?:\|--[a-z-]+)*)\)", source,
                             re.MULTILINE):
            accepted.update(m.group(1).split("|"))
        for flag in rs.AUTO_FORWARDED.values():
            self.assertIn(flag, accepted,
                          f"radio_settings forwards {flag}, which "
                          f"soop_capture.sh does not parse")

    def test_shared_settings_become_automation_values(self):
        shared = {"bandwidth": "10", "samplerate": "4", "rx1_gain": "35",
                  "rx2_gain": "35"}
        self.assertEqual(rs.auto_overrides(shared),
                         {"bw": "10", "sr": "4", "gain": "35"})

    def test_blank_settings_forward_nothing(self):
        self.assertEqual(rs.auto_overrides({}), {})

    def test_those_values_survive_into_the_argv(self):
        values = dict(rs.auto_overrides({"bandwidth": "10", "samplerate": "4",
                                         "rx1_gain": "35", "rx2_gain": "35"}))
        argv = jobs.autocapture_job(values).argv
        for flag, value in (("--bw", "10"), ("--sr", "4"), ("--gain", "35")):
            self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_a_split_gain_is_never_flattened(self):
        """rx1 20 dB / rx2 45 dB cannot become 20/20 behind the user's back."""
        shared = {"rx1_gain": "20", "rx2_gain": "45"}
        self.assertNotIn("gain", rs.auto_overrides(shared),
                         "a split gain was forwarded as one value — the "
                         "reflected chain would run at the direct chain's gain")
        self.assertEqual(rs.gain_conflict(shared), ("20", "45"))

    def test_matching_gains_are_forwarded(self):
        shared = {"rx1_gain": "30", "rx2_gain": "30"}
        self.assertIsNone(rs.gain_conflict(shared))
        self.assertEqual(rs.auto_overrides(shared), {"gain": "30"})


class TestEffectiveView(unittest.TestCase):

    def test_every_capture_prompt_is_accounted_for(self):
        """No radio prompt may be left without a stated origin."""
        keys = {s.key for s in rs.effective(shared={})}
        prompt_keys = {k for k, _label, _default in jobs.CAPTURE_PROMPTS}
        # freq/bandwidth/samplerate/gain are grouped or renamed; the rest map 1:1.
        expected = prompt_keys - {"experiment_info", "capture_mode",
                                  "rx1_gain", "rx2_gain"}
        missing = sorted(expected - keys)
        self.assertEqual(missing, [],
                         f"these prompts are sent to capture.sh with no origin "
                         f"shown anywhere in the GUI: {missing}")

    def test_capture_tab_values_win_over_the_script(self):
        rows = {s.key: s for s in rs.effective(shared={"bandwidth": "10"})}
        self.assertEqual(rows["bandwidth"].value, "10")
        self.assertEqual(rows["bandwidth"].kind, "shared")
        self.assertEqual(rows["bandwidth"].origin, "Capture tab")

    def test_automation_overrides_win_over_the_capture_tab(self):
        rows = {s.key: s for s in rs.effective(shared={"bandwidth": "10"},
                                               overrides={"bw": "2"})}
        self.assertEqual(rows["bandwidth"].value, "2")
        self.assertEqual(rows["bandwidth"].origin, "Automation tab")

    def test_unset_bandwidth_is_reported_as_per_satellite(self):
        rows = {s.key: s for s in rs.effective(shared={})}
        self.assertEqual(rows["bandwidth"].kind, "plan")
        self.assertIn("plan", rows["bandwidth"].origin)

    def test_samplerate_mirrors_bandwidth_not_the_plan(self):
        """soop_capture.sh:258 sets sr=bw when --sr is absent. Showing 'per
        satellite' there would be wrong whenever a bandwidth IS forced."""
        rows = {s.key: s for s in rs.effective(shared={})}
        self.assertIn("matched to bandwidth", rows["samplerate"].value)

    def test_inheritance_can_be_switched_off(self):
        rows = {s.key: s for s in rs.effective(shared={"bandwidth": "10"},
                                               use_shared=False)}
        self.assertNotEqual(rows["bandwidth"].value, "10")

    def test_conflict_is_surfaced_not_hidden(self):
        rows = {s.key: s for s in rs.effective(
            shared={"rx1_gain": "20", "rx2_gain": "45"})}
        self.assertEqual(rows["gain"].kind, "conflict")
        self.assertIn("20", rows["gain"].origin)
        self.assertIn("45", rows["gain"].origin)


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self._backup = None
        if os.path.isfile(paths.PRESETS_JSON):
            with open(paths.PRESETS_JSON, encoding="utf-8") as f:
                self._backup = f.read()

    def tearDown(self):
        if self._backup is None:
            if os.path.isfile(paths.PRESETS_JSON):
                os.remove(paths.PRESETS_JSON)
        else:
            with open(paths.PRESETS_JSON, "w", encoding="utf-8") as f:
                f.write(self._backup)

    def test_round_trip(self):
        rs.save({"bandwidth": "10", "samplerate": "10", "rx1_gain": "30"})
        self.assertEqual(rs.load(), {"bandwidth": "10", "samplerate": "10",
                                     "rx1_gain": "30"})

    def test_blank_values_are_not_stored(self):
        rs.save({"bandwidth": "10", "samplerate": "", "bitmode": "   "})
        self.assertEqual(set(rs.load()), {"bandwidth"})

    def test_saving_preserves_other_sections(self):
        """The sky mask lives in the same file and must survive a radio save."""
        paths.ensure_state_dirs()
        with open(paths.PRESETS_JSON, "w", encoding="utf-8") as f:
            json.dump({"sky_mask": {"open_sectors": ["N"], "min_elev_deg": 5}}, f)
        rs.save({"bandwidth": "10"})
        with open(paths.PRESETS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("sky_mask", data)
        self.assertEqual(data["sky_mask"]["open_sectors"], ["N"])

    def test_unknown_keys_are_dropped(self):
        rs.save({"bandwidth": "10", "not_a_radio_setting": "x"})
        self.assertNotIn("not_a_radio_setting", rs.load())


if __name__ == "__main__":
    unittest.main()
