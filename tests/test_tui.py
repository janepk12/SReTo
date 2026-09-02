"""
The terminal front-end: capturing is reachable, its prompt contract matches
capture.sh, its answers actually get sent, and a missing science repository
is explained rather than crashed into.

Ported from the science repository's own gui/tests/test_tui.py — same
guarantees, adapted for two differences: SReTo has real argparse flags instead
of tui.dispatch()'s hand-rolled routing, and capture.sh/soop_capture.sh live in
a science repository this package only READS, so every action that would shell
out to one is gated on config.is_configured() and must refuse cleanly, not
crash, when there is none.
"""

import io
import os
import unittest
from contextlib import redirect_stdout

from sreto import config, jobs, tui

from .support import requires_science_repo


class TestCapturingIsReachableFromTheMenu(unittest.TestCase):
    """The menu shipped without the one thing the instrument is for.

    Every other entry — analysis, planner, pre-checks, status — was reachable
    over SSH; capturing was not. These tests make that absence fail instead of
    passing silently.
    """

    def _entry(self, key):
        for k, label, action in tui.MENU:
            if k == key:
                return label, action
        self.fail(f"no menu entry [{key}] — the menu keys are "
                  f"{[k for k, _, _ in tui.MENU]}")

    def test_a_manual_capture_is_on_the_menu(self):
        label, action = self._entry("c")
        self.assertIn("apture", label)
        self.assertIs(action, tui.action_capture)

    def test_an_unattended_session_is_on_the_menu(self):
        _label, action = self._entry("a")
        self.assertIs(action, tui.action_autocapture)

    def test_menu_keys_are_unique(self):
        """A duplicate key silently shadows the later entry: the first match
        wins in menu()'s loop, so one option becomes unreachable."""
        keys = [k for k, _, _ in tui.MENU]
        self.assertEqual(len(keys), len(set(keys)), f"duplicate menu key in {keys}")


class TestCapturePromptContract(unittest.TestCase):
    """The menu must ask capture.sh's questions in capture.sh's order.

    capture.sh reads positionally. An extra question, a missing one, or a gain
    prompt asked when AGC suppressed it, shifts every later answer by one — a
    successful capture at the wrong frequency with a plausible log. This is
    checked against jobs.CAPTURE_PROMPTS, which is a plain data constant here
    (not derived from a repo file), so it needs no science repository.
    """

    def _drive(self, answers):
        asked = []

        def fake_ask(label, default=""):
            asked.append(label)
            for key, prompt_label, _default in jobs.CAPTURE_PROMPTS:
                if prompt_label == label:
                    return answers.get(key, "")
            self.fail(f"asked an unknown prompt: {label!r}")

        original = tui._ask
        tui._ask = fake_ask
        try:
            values = tui._ask_capture_values()
        finally:
            tui._ask = original
        return values, asked

    def test_it_asks_capture_sh_s_prompts_in_capture_sh_s_order(self):
        values, asked = self._drive({})
        expected = [label for _k, label, _d in jobs.CAPTURE_PROMPTS]
        self.assertEqual(asked, expected)
        self.assertEqual(set(values), {k for k, _, _ in jobs.CAPTURE_PROMPTS})

    def test_agc_on_suppresses_that_channel_s_gain_prompt(self):
        _values, asked = self._drive({"agc_rx1": "on"})
        self.assertNotIn("Gain rx1", asked)
        self.assertIn("Gain rx2", asked, "only rx1's AGC was on")

        _values, asked = self._drive({"agc_rx1": "on", "agc_rx2": "on"})
        self.assertNotIn("Gain rx1", asked)
        self.assertNotIn("Gain rx2", asked)

    def test_the_answers_it_produces_survive_the_round_trip(self):
        values, _asked = self._drive({"freq": "1575", "samplerate": "10",
                                      "agc_rx1": "on"})
        answers = jobs.capture_answers(values)
        self.assertEqual(answers[0], "1575")
        self.assertEqual(answers[1], "10")
        self.assertEqual(answers[9], "",
                         "an rx1 gain was sent even though AGC rx1 is on")

    def test_abandoning_a_prompt_abandons_the_whole_capture(self):
        def refuse(_label, _default=""):
            return None

        original = tui._ask
        tui._ask = refuse
        try:
            self.assertIsNone(tui._ask_capture_values())
        finally:
            tui._ask = original


class TestCaptureAnswersActuallyReachTheScript(unittest.TestCase):
    """_run_job must forward Job.stdin_lines.

    capture.sh blocks on `read`. A job whose answers are built and then
    dropped does not fail — it hangs forever at the first prompt, on a machine
    nobody is sitting at, having reported that the capture started.
    """

    def test_run_job_forwards_the_stdin_answers(self):
        seen = {}

        def fake_run(argv, cwd=None, env=None, echo=True, stdin_lines=None):
            seen["argv"] = argv
            seen["stdin_lines"] = stdin_lines
            return 0

        job = jobs.capture_job({"freq": "1575", "samplerate": "10",
                                "capture_mode": "2", "amount": "30"})
        self.assertTrue(job.stdin_lines, "capture_job built no answers")

        original = tui._run
        tui._run = fake_run
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                tui._run_job(job)
        finally:
            tui._run = original

        self.assertEqual(seen.get("stdin_lines"), job.stdin_lines,
                         "capture.sh's answers were built but never sent — the "
                         "capture would hang on its first prompt")

    def test_an_analysis_job_still_runs_with_no_stdin(self):
        job = jobs.planner_job({"mode": "now_plus_24h"})
        self.assertEqual(job.stdin_lines, [])


class TestNoRepoRefusesRatherThanCrashing(unittest.TestCase):
    """capture.sh, soop_capture.sh and MAIN.py live in the science repository,
    which this package only reads and which may not be configured at all —
    the menu must still OPEN and explain itself, not raise or shell out to a
    path built from an empty root."""

    def _unconfigured(self):
        """Monkeypatch config.is_configured() False for one test, restored after."""
        original = config.is_configured
        config.is_configured = lambda: False
        self.addCleanup(setattr, config, "is_configured", original)

    def test_needs_repo_refuses_and_explains(self):
        self._unconfigured()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = tui._needs_repo()
        self.assertFalse(ok)
        self.assertIn("SRETO_REPO_ROOT", buffer.getvalue())

    def test_capture_refuses_before_asking_a_single_prompt(self):
        """A capture attempted with no repo must not reach _ask_capture_values
        — asking fourteen questions and THEN refusing would be worse than
        refusing up front."""
        self._unconfigured()

        def fail_if_called(_label, _default=""):
            self.fail("a prompt was asked with no science repository configured")

        original = tui._ask
        tui._ask = fail_if_called
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                tui.action_capture()
        finally:
            tui._ask = original

    def test_the_menu_still_opens_and_says_so(self):
        self._unconfigured()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tui._print_menu()
        self.assertIn("no science repository", buffer.getvalue())
        self.assertIn("[c]", buffer.getvalue(), "the menu itself must still render")

    @requires_science_repo
    def test_a_configured_repo_does_not_refuse(self):
        self.assertTrue(tui._needs_repo())


class TestSustainedWriteRateWarnings(unittest.TestCase):
    """_show_capture_plan's write-rate line, over the microSD thresholds."""

    def _plan(self, values):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = tui._show_capture_plan(values)
        return ok, buffer.getvalue()

    def test_a_low_rate_capture_needs_no_confirmation_override(self):
        ok, out = self._plan({"samplerate": "2", "channels": "1,2",
                              "bitmode": "16bit", "capture_mode": "2",
                              "amount": "10"})
        self.assertTrue(ok)
        self.assertIn("16.0 MB/s", out)

    def test_a_rate_past_every_microsd_refuses_by_default(self):
        ok, out = self._plan({"samplerate": "20", "channels": "1,2",
                              "bitmode": "16bit", "capture_mode": "2",
                              "amount": "10"})
        self.assertFalse(ok)
        self.assertIn("WARNING", out)


if __name__ == "__main__":
    unittest.main()
