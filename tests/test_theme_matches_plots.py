"""
The GUI's palette must stay identical to the figures'.

theme.py duplicates the colours from waterfalls.py and iq_dashboard.py rather
than importing them, because importing either module drags matplotlib and numpy
into a process that is supposed to stay light. Duplication is only acceptable
with a test that notices when the two copies drift, which is this one: it
re-parses both plotting modules and compares every shared constant.
"""

import ast
import os
import re
import unittest

from sreto import paths, theme


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

SHARED = ("BG", "PANEL", "BORDER", "TEXT", "MUTED", "C1", "C2")
WATERFALL_ONLY = ("C3", "C_EXTRACT")
DASHBOARD_ONLY = ("C_DIFF", "C_PH", "C_XC", "C_PEAK")

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _module_constants(filename):
    """Top-level string constants of a plotting module, without importing it.

    Handles the tuple form both modules use:
        BG, PANEL, BORDER, TEXT, MUTED = "#f8f9fa", "#ffffff", …
    """
    path = os.path.join(paths.ANALYSIS_SRC_DIR, filename)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass
            elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for name, value in zip(target.elts, node.value.elts):
                    if isinstance(name, ast.Name):
                        try:
                            out[name.id] = ast.literal_eval(value)
                        except (ValueError, SyntaxError):
                            pass
    return out


class TestPaletteParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.waterfalls = _module_constants("waterfalls.py")
        cls.dashboard = _module_constants("iq_dashboard.py")

    def _compare(self, source, names, module_name):
        for name in names:
            self.assertIn(name, source,
                          f"{module_name} no longer defines {name}")
            self.assertEqual(
                getattr(theme, name), source[name],
                f"{name} drifted: gui/theme.py has {getattr(theme, name)}, "
                f"{module_name} has {source[name]}. The GUI and the figures "
                f"would no longer match — update theme.py.")

    def test_shared_neutrals_and_channel_colours(self):
        self._compare(self.waterfalls, SHARED, "waterfalls.py")
        self._compare(self.dashboard, SHARED, "iq_dashboard.py")

    def test_waterfall_specific_colours(self):
        self._compare(self.waterfalls, WATERFALL_ONLY, "waterfalls.py")

    def test_dashboard_specific_colours(self):
        self._compare(self.dashboard, DASHBOARD_ONLY, "iq_dashboard.py")

    def test_the_two_plotting_modules_agree_with_each_other(self):
        for name in SHARED:
            self.assertEqual(self.waterfalls[name], self.dashboard[name],
                             f"waterfalls.py and iq_dashboard.py disagree on "
                             f"{name} — the figures themselves are inconsistent")

    def test_channel_identity_is_not_swapped(self):
        """C1 = rx1 = direct (RE, blue); C2 = rx2 = reflected (GR, orange)."""
        self.assertEqual(theme.C1, "#1f77b4")
        self.assertEqual(theme.C2, "#ff7f0e")

    def test_every_palette_entry_is_a_valid_hex_colour(self):
        names = list(SHARED) + list(WATERFALL_ONLY) + list(DASHBOARD_ONLY) + [
            "OK", "AMBER", "FAIL", "RUNNING", "IDLE", "SURFACE", "SURFACE_ALT",
            "SELECT_BG", "CONSOLE_BG", "CONSOLE_FG", "CONSOLE_SEL"]
        for name in names:
            value = getattr(theme, name)
            self.assertRegex(value, _HEX_RE, f"{name} = {value!r} is not #rrggbb")
        for mapping in (theme.ANSI_COLORS, theme.CONSOLE_ANSI):
            for key, value in mapping.items():
                self.assertRegex(value, _HEX_RE, f"{key} = {value!r}")

    def test_warning_colour_is_not_the_reflected_channel_colour(self):
        """Amber must stay distinct from C2, which means 'rx2' everywhere."""
        self.assertNotEqual(theme.AMBER, theme.C2)

    def test_console_ansi_covers_every_code_console_py_emits(self):
        console_src = os.path.join(paths.ANALYSIS_SRC_DIR, "console.py")
        with open(console_src, encoding="utf-8") as f:
            src = f.read()
        emitted = set(re.findall(r'"(\w+)":\s*"\\033\[\d+m"', src))
        emitted -= {"bold", "dim", "reset"}          # attributes, not colours
        missing = sorted(emitted - set(theme.CONSOLE_ANSI))
        self.assertEqual(missing, [],
                         f"console.py emits colours the embedded terminal cannot "
                         f"render: {missing}")


if __name__ == "__main__":
    unittest.main()
