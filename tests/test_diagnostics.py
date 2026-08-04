"""
Failure reporting, tested against the real thing that went wrong.

A single-channel capture was run four times through a dual-channel pipeline.
Each time the GUI printed "a missing capture .json, a satellite that could not
be resolved, or an unreachable CelesTrak are the usual causes" — three guesses
from the exit code, all three wrong, while the actual ValueError sat two lines
above. The first test below is that exact output.
"""

import unittest

from sreto import diagnostics
from sreto.panels.analysis_panel import DUAL_CHANNEL_STAGES, is_dual_channel

# Verbatim tail of the failing run (<state>/logs, 2026-08-03). The two traceback
# paths were the reporter's own home directory; they are shown here relative to
# the state dir and the science repo, because nothing in this file depends on
# them being absolute and a committed fixture should not carry someone's
# username. diagnostics.py matches on the exception and the module name.
REAL_SINGLE_CHANNEL_FAILURE = """
[POWER] 97 rows x 2048 bins (10-frame looks) | full band 1579.0000..1580.9990 MHz

════════════════════════════════════════════════════════════════════
 PHASE WATERFALLS + 1D
════════════════════════════════════════════════════════════════════
[load] cache hit: 2,000,000 of 2,000,000 cached frames (no disk read)
Traceback (most recent call last):
  File "tmp/MAIN_gui_20260803_141112.py", line 513, in <module>
    generate_phase_waterfalls_and_1d(
  File "01_CODE/waterfalls.py", line 334, in generate_phase_waterfalls_and_1d
    raise ValueError("[PHASE] Requires dual-channel data.")
ValueError: [PHASE] Requires dual-channel data.
"""


class TestDiagnoseTheRealFailure(unittest.TestCase):

    def test_single_channel_failure_is_identified(self):
        headline, guidance = diagnostics.diagnose(
            REAL_SINGLE_CHANNEL_FAILURE, returncode=1, kind="analysis")
        self.assertIn("SINGLE-channel", headline)
        self.assertIn("ch1_2", guidance)

    def test_it_does_not_blame_the_old_three_causes(self):
        """The bug being fixed: guessing .json / satellite / CelesTrak."""
        headline, guidance = diagnostics.diagnose(
            REAL_SINGLE_CHANNEL_FAILURE, returncode=1, kind="analysis")
        blob = (headline + " " + guidance).lower()
        for wrong in ("celestrak", "missing capture", "unresolved satellite"):
            self.assertNotIn(wrong, blob,
                             f"still blaming {wrong!r} for a channel-count problem")

    def test_it_says_toggles_will_not_help(self):
        """The non-obvious part: MAIN.py:507 is unconditional."""
        _headline, guidance = diagnostics.diagnose(REAL_SINGLE_CHANNEL_FAILURE)
        self.assertIn("507", guidance)
        self.assertIn("NOT", guidance)

    def test_sibling_dual_channel_messages_match_too(self):
        for message in ("ValueError: The IQ dashboard requires a dual-channel "
                        "(ch1_2) capture.",
                        "ValueError: [band-xcorr] requires a dual-channel "
                        "(ch1_2) capture.",
                        "ValueError: Physics extraction requires a dual-channel "
                        "capture.",
                        "ValueError: Reference capture must be dual-channel."):
            headline, _g = diagnostics.diagnose(message)
            self.assertIn("SINGLE-channel", headline, message)


class TestOtherFailures(unittest.TestCase):

    def test_missing_json(self):
        headline, guidance = diagnostics.diagnose(
            "FileNotFoundError: 'X.bin' has no .json in any of: ['/a', '/b']")
        self.assertIn(".json", headline)
        self.assertIn("DATA_DIRS", guidance)

    def test_tle_abort(self):
        headline, _g = diagnostics.diagnose(
            "[tle] ABORTING — cannot resolve SATELLITE_QUERY=43641")
        self.assertIn("satellite", headline)

    def test_missing_module(self):
        headline, guidance = diagnostics.diagnose(
            "ModuleNotFoundError: No module named 'skyfield'")
        self.assertIn("module", headline)
        self.assertIn("sdrr", guidance)

    def test_oom(self):
        headline, _g = diagnostics.diagnose("MemoryError")
        self.assertIn("memory", headline)

    def test_unknown_exception_is_quoted_not_guessed(self):
        """No rule matches -> repeat the exception, do not invent a cause."""
        headline, guidance = diagnostics.diagnose(
            "Traceback (most recent call last):\n"
            "  File 'x.py', line 1\n"
            "RuntimeError: the flux capacitor desynchronised")
        self.assertIn("RuntimeError", headline)
        self.assertIn("flux capacitor", headline)
        self.assertIn("authoritative", guidance)

    def test_last_exception_wins_when_several_are_present(self):
        headline, _g = diagnostics.diagnose(
            "ValueError: an earlier, handled problem\n"
            "...\n"
            "KeyError: 'the one that actually killed it'")
        self.assertIn("KeyError", headline)

    def test_falls_back_to_the_exit_code_with_no_output(self):
        headline, guidance = diagnostics.diagnose("", returncode=127)
        self.assertIn("127", headline)
        self.assertIn("bladeRF-cli", guidance)

    def test_never_returns_none_for_a_failure(self):
        for rc in (1, 2, 127, 137, -9, 255):
            for kind in ("analysis", "capture", "autocapture", "planner"):
                result = diagnostics.diagnose("some output", returncode=rc,
                                              kind=kind)
                self.assertIsNotNone(result)
                self.assertEqual(len(result), 2)

    def test_tail_bounds_the_scan(self):
        text = "\n".join(f"line {i}" for i in range(500))
        self.assertEqual(len(diagnostics.tail(text, 80).splitlines()), 80)
        self.assertIn("line 499", diagnostics.tail(text, 80))


class TestChannelDetection(unittest.TestCase):

    def test_matches_main_pys_own_test(self):
        """MAIN.py:266 uses `"2" in str(channels)`. Nothing subtler."""
        for channels, expected in (("1,2", True), ("2,1", True), ("2", True),
                                   ("1", False), (1, False), ("", False)):
            self.assertEqual(is_dual_channel({"channels": channels}), expected,
                             f"channels={channels!r}")

    def test_missing_channels_field_is_treated_as_single(self):
        self.assertFalse(is_dual_channel({}))

    def test_the_unconditional_stage_is_recorded_as_such(self):
        """The phase stage has no toggle — that is why the guard is a block."""
        unconditional = [s for s in DUAL_CHANNEL_STAGES if s[2] is None]
        self.assertEqual(len(unconditional), 1)
        self.assertIn("phase", unconditional[0][0])

    def test_every_stage_cites_the_line_that_raises(self):
        for label, where, _toggle in DUAL_CHANNEL_STAGES:
            self.assertRegex(where, r"^\w+\.py:\d+$", label)


if __name__ == "__main__":
    unittest.main()
