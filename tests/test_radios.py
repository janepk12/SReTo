"""
The radio support catalogue.

Repo-independent: nothing here touches hardware, the science repository or a
display. The point of the tests is that the catalogue cannot quietly start
promising something — a "supported" radio with no driver, or a single-channel
device listed as if it could make the measurement.
"""

import unittest

from sreto import radios


class TestCatalogueIsWellFormed(unittest.TestCase):

    def test_keys_are_unique_and_lookupable(self):
        keys = radios.keys()
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsNotNone(radios.get(key))

    def test_lookup_is_case_and_whitespace_insensitive(self):
        self.assertIs(radios.get("  BladeRF "), radios.get("bladerf"))

    def test_unknown_key_is_none_not_an_exception(self):
        self.assertIsNone(radios.get("nosuchradio"))
        self.assertIsNone(radios.get(None))
        self.assertIsNone(radios.get(""))

    def test_every_status_is_one_of_the_declared_four(self):
        allowed = {radios.SUPPORTED, radios.IN_PROGRESS,
                   radios.PLANNED, radios.LIMITED}
        for r in radios.CATALOG:
            self.assertIn(r.status, allowed, r.key)

    def test_every_radio_names_a_tool_a_probe_and_a_capture_command(self):
        for r in radios.CATALOG:
            for field in ("label", "host_tool", "probe", "capture", "notes"):
                self.assertTrue(getattr(r, field).strip(),
                                f"{r.key} has an empty {field}")


class TestTheCoherenceClaim(unittest.TestCase):
    """The one thing this catalogue must never get wrong."""

    def test_single_channel_radios_are_never_marked_supported_or_planned(self):
        # A one-tuner radio cannot produce a carrier phase difference, so it
        # must be 'limited' — listing it as planned would read as "wait and it
        # will work", which is false and no amount of work changes it.
        for r in radios.CATALOG:
            if r.rx_channels < 2:
                self.assertEqual(r.status, radios.LIMITED, r.key)

    def test_limited_radios_are_exactly_the_non_coherent_ones(self):
        limited = {r.key for r in radios.by_status(radios.LIMITED)}
        single = {r.key for r in radios.CATALOG if not r.coherent}
        self.assertEqual(limited, single)

    def test_coherent_only_excludes_the_single_channel_devices(self):
        for r in radios.coherent_only():
            self.assertGreaterEqual(r.rx_channels, 2)


class TestSupportedSet(unittest.TestCase):

    def test_the_default_radio_is_in_the_catalogue_and_supported(self):
        default = radios.get(radios.DEFAULT)
        self.assertIsNotNone(default)
        self.assertEqual(default.status, radios.SUPPORTED)

    def test_bladerf_is_the_supported_one_and_its_probe_matches_prechecks(self):
        from sreto import paths

        supported = radios.supported()
        self.assertEqual([r.key for r in supported], ["bladerf"])
        # prechecks.check_bladerf resolves this exact binary. If the catalogue
        # ever names a different tool, the pre-check is checking the wrong one.
        self.assertIn("bladeRF-cli", supported[0].host_tool)
        self.assertTrue(paths.bladerf_cli.__doc__)

    def test_coming_soon_is_everything_else_with_in_progress_first(self):
        soon = radios.coming_soon()
        self.assertEqual(len(soon) + len(radios.supported()),
                         len(radios.CATALOG))
        self.assertNotIn(radios.SUPPORTED, [r.status for r in soon])
        self.assertEqual(soon[0].status, radios.IN_PROGRESS)


class TestFormattedOutput(unittest.TestCase):
    """`sreto --radios` — the text a user actually sees."""

    def setUp(self):
        self.text = radios.format_table()

    def test_every_radio_appears(self):
        for r in radios.CATALOG:
            self.assertIn(r.label, self.text)
            self.assertIn(r.key, self.text)

    def test_it_says_which_ones_are_not_wired_up_yet(self):
        self.assertIn("roadmap", self.text.lower())
        self.assertIn("coming soon", self.text)

    def test_it_states_the_two_channel_requirement(self):
        lowered = self.text.lower()
        self.assertIn("one clock", lowered)
        self.assertIn("two rx channels", lowered)

    def test_no_line_is_absurdly_long(self):
        # It is printed into a terminal, not a browser.
        for line in self.text.splitlines():
            self.assertLessEqual(len(line), 120, line)


class TestCliFlag(unittest.TestCase):

    def test_radios_flag_prints_the_table_and_exits_zero(self):
        import contextlib
        import io

        from sreto.__main__ import main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--radios"])
        self.assertEqual(code, 0)
        self.assertIn("bladeRF", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
