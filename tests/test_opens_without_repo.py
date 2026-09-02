"""
The headline claim: the window opens with no science repository configured.

WHY THIS FILE EXISTS
--------------------
It stopped being true, and nothing caught it. `AnalysisPanel.__init__` called
`load_defaults()`, which read `MAIN.py` off a repository that was not there.
Two failure modes followed, in this order:

  1. `FileNotFoundError` escaped through `App._build`, and the window never
     appeared at all.
  2. Once that was turned into a handled error, the panel raised a MODAL
     messagebox — during construction, before the root window had ever been
     mapped. `App()` blocked forever on a dialog with nothing to be modal to
     and no way to dismiss it. Worse than the crash: a crash at least says so.

Neither was visible to the rest of the suite, because every other test either
skips without a repository or never builds the real window. The gap was the
combination: a display AND no repository — which is precisely what a new user
who just ran `pip install` has.

The test is display-dependent and reports as skipped without one.
"""

import os
import unittest

from sreto import config, main_params
from tests.support import requires_display


class TestMainParamsWithoutRepo(unittest.TestCase):
    """The layer underneath, which needs no display and always runs."""

    @unittest.skipIf(config.is_configured(),
                     "a science repository IS configured on this machine")
    def test_read_defaults_raises_the_handled_error_not_oserror(self):
        # MainParamError is what every caller catches. An OSError escaping
        # here is the original bug.
        with self.assertRaises(main_params.MainParamError):
            main_params.read_defaults()

    @unittest.skipIf(config.is_configured(),
                     "a science repository IS configured on this machine")
    def test_the_error_names_how_to_fix_it(self):
        try:
            main_params.read_defaults()
        except main_params.MainParamError as e:
            message = str(e)
        else:
            self.fail("expected MainParamError")
        self.assertIn("--set-repo", message)
        self.assertIn("SRETO_REPO_ROOT", message)

    def test_missing_main_py_is_never_an_oserror(self):
        """Independent of this machine's configuration: point the module at a
        path that certainly does not exist and check the error type."""
        original = main_params.paths.MAIN_PY
        main_params.paths.MAIN_PY = os.path.join(
            os.path.dirname(__file__), "definitely-not-here", "MAIN.py")
        try:
            with self.assertRaises(main_params.MainParamError):
                main_params.read_defaults()
        finally:
            main_params.paths.MAIN_PY = original


@requires_display
class TestTheWindowBuilds(unittest.TestCase):
    """Build the real App, with whatever this machine has. No mainloop."""

    def setUp(self):
        from sreto import app as app_mod
        self.app = app_mod.App()
        self.app.root.withdraw()

    def tearDown(self):
        try:
            self.app.root.destroy()
        except Exception:                                  # noqa: BLE001
            pass

    def test_every_tab_is_present(self):
        tabs = [self.app.notebook.tab(t, "text")
                for t in self.app.notebook.tabs()]
        self.assertEqual(tabs, ["Capture", "Automation", "Analysis", "Physics",
                                "SoOp availability", "History", "Pre-checks"])

    def test_construction_did_not_block_on_a_dialog(self):
        """Reaching setUp at all proves it. Stated as a test so the reason is
        in the report rather than only in this docstring."""
        self.assertTrue(self.app.root.winfo_exists())

    def test_the_analysis_panel_exists_even_with_no_parameters_to_show(self):
        panel = self.app.analysis_panel
        self.assertTrue(panel.winfo_exists())
        if not config.is_configured():
            # Empty is correct here — there is no MAIN.py to read. The panel
            # must still be a usable widget, not a half-constructed one.
            self.assertEqual(panel.defaults, {})


if __name__ == "__main__":
    unittest.main()
