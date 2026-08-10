"""
Two things a clean install got wrong, and neither of them raised anything.

FONT RESOLUTION
---------------
The GUI rendered on Linux in the X11 'fixed' bitmap font — a chunky terminal
face, nothing like the macOS build — while working perfectly. There was no
error to search for, because there was no error. Two bugs compounded:

  1. ``tkfont.families()`` reports lower-cased names on X11 ('dejavu sans'),
     so a case-sensitive membership test against a title-cased candidate list
     never matched, and every Linux machine fell through to the fallback.
  2. The fallback passed ``family="TkDefaultFont"`` — a NAMED FONT, not a
     family. Tk answers an unknown family with 'fixed' rather than an error,
     so the branch meant to be the safe one produced the worst result
     available.

OUTPUT DIRECTORIES
------------------
With no science repository, every capture and figure path was derived from a
``no-science-repo`` placeholder that nothing ever created. The paths were
displayed, the reveal buttons pointed at them, and the disk pre-check tried to
stat them — all naming a directory that did not exist. They now fall back to a
tree SReTo owns and creates, which is what makes a fresh clone usable.

The guarantee that must survive both: SReTo still creates NOTHING inside a
configured science repository.
"""

import os
import tempfile
import unittest

from sreto import config, paths, theme
from tests.support import requires_display


@requires_display
class TestFontResolution(unittest.TestCase):

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_resolved_families_are_families_not_named_fonts(self):
        """The exact bug: a named font used where a family was wanted.

        'TkDefaultFont' and 'TkFixedFont' are named fonts. Tk accepts either as
        a `family=` without complaint and silently draws in 'fixed'.
        """
        theme.probe_fonts(self.root)
        for resolved in (theme.Fonts.ui, theme.Fonts.mono):
            self.assertNotIn(resolved, ("TkDefaultFont", "TkFixedFont"),
                             "a NAMED FONT leaked through as a family name")

    def test_the_resolved_family_is_one_tk_actually_has(self):
        """Whatever we picked, Tk must resolve it to itself and not substitute.

        This is what catches the fallback regressing: asking for a family Tk
        does not have comes back as 'fixed', so a mismatch here means the GUI
        is about to be drawn in the bitmap font again.
        """
        import tkinter.font as tkfont

        theme.probe_fonts(self.root)
        for resolved in (theme.Fonts.ui, theme.Fonts.mono):
            actual = tkfont.Font(root=self.root, family=resolved,
                                 size=11).actual("family")
            self.assertEqual(actual.lower(), resolved.lower(),
                             f"Tk substituted {actual!r} for {resolved!r}")

    def test_candidate_matching_ignores_case(self):
        """X11 reports 'dejavu sans'; the candidate list says 'DejaVu Sans'."""
        import tkinter.font as tkfont

        family = next((f for f in tkfont.families(self.root) if f.strip()), None)
        if family is None:                                 # pragma: no cover
            self.skipTest("Tk reported no font families at all")
        self.assertEqual(
            theme._first_available(self.root, (family.upper(),), "TkDefaultFont"),
            family,
            "matching is case-sensitive, so no Linux family can ever match")

    def test_an_unknown_candidate_falls_back_to_a_real_family(self):
        resolved = theme._first_available(
            self.root, ("No Such Font 12345",), "TkDefaultFont")
        self.assertNotEqual(resolved, "TkDefaultFont")
        self.assertTrue(resolved)


class TestFontResolutionIsCrossPlatform(unittest.TestCase):
    """The same code has to be right on a Mac, which cannot be tested on CI.

    The resolver's only input is what ``tkfont.families()`` returns, so each
    platform is reproduced exactly by substituting that list. These are the
    lists the three Tk builds really report:

        macOS / Aqua    cased names, no Xft and none needed
        Linux / Xft     cased names from fontconfig
        Linux / X core  lower-case XLFD names, Latin-1 only

    The macOS case matters most: it was never broken, and the fix must not
    change what it resolves to.
    """

    MACOS = ("SF Pro Text", "SF Pro Display", "Helvetica Neue", "Menlo",
             "Monaco", "Courier New", "Geneva", ".AppleSystemUIFont")
    LINUX_XFT = ("DejaVu Sans", "DejaVu Sans Mono", "Noto Sans",
                 "Liberation Sans", "Liberation Mono", "Ubuntu")
    LINUX_CORE = ("nimbus sans l", "nimbus mono l", "helvetica", "courier",
                  "fixed", "clean")

    def _resolve(self, families):
        """(ui, mono, unicode_ok) for a Tk reporting `families`."""
        original = theme.tkfont.families
        theme.tkfont.families = lambda root=None: list(families)
        try:
            return (
                theme._first_available(None, theme._UI_CANDIDATES,
                                       "TkDefaultFont"),
                theme._first_available(None, theme._MONO_CANDIDATES,
                                       "TkFixedFont"),
                theme._unicode_text_available(None),
            )
        finally:
            theme.tkfont.families = original

    def test_macos_still_gets_its_native_fonts(self):
        ui, mono, unicode_ok = self._resolve(self.MACOS)
        self.assertEqual(ui, "SF Pro Text")
        self.assertEqual(mono, "Menlo")
        self.assertTrue(unicode_ok, "macOS must keep the typographic glyphs")

    def test_linux_with_xft_gets_a_real_desktop_font(self):
        ui, mono, unicode_ok = self._resolve(self.LINUX_XFT)
        self.assertIn(ui, self.LINUX_XFT)
        self.assertIn(mono, self.LINUX_XFT)
        self.assertTrue(unicode_ok)

    def test_linux_without_xft_degrades_but_never_to_the_bitmap_font(self):
        """The original bug's exact conditions: lower-case names, no match."""
        ui, mono, unicode_ok = self._resolve(self.LINUX_CORE)
        self.assertNotEqual(ui, "fixed", "back to the X11 bitmap font")
        self.assertNotEqual(mono, "fixed", "back to the X11 bitmap font")
        self.assertNotIn(ui, ("TkDefaultFont", "TkFixedFont"))
        self.assertFalse(unicode_ok,
                         "X core fonts are Latin-1 only; glyphs must degrade")

    def test_a_case_only_difference_still_matches(self):
        """'DejaVu Sans' in the candidates vs 'dejavu sans' from X11."""
        ui, _mono, _u = self._resolve(("dejavu sans", "dejavu sans mono"))
        self.assertEqual(ui, "dejavu sans")


class TestGlyphFallback(unittest.TestCase):
    """Characters past Latin-1 must degrade to ASCII, not to a hex box."""

    def tearDown(self):
        theme.unicode_text = True

    def test_every_glyph_has_an_ascii_stand_in(self):
        theme.unicode_text = False
        for key in theme._GLYPHS:
            plain = theme.glyph(key)
            self.assertTrue(plain.isascii(),
                            f"{key!r} degrades to non-ASCII {plain!r}, which is "
                            f"exactly what a Latin-1-only Tk cannot draw")

    def test_the_preferred_glyph_is_used_when_tk_can_draw_it(self):
        theme.unicode_text = True
        self.assertEqual(theme.glyph("arrow"), "→")


class TestOutputRoot(unittest.TestCase):

    def test_output_root_is_never_inside_the_science_repo(self):
        """SReTo may read that tree. It may not put its own output in it."""
        if not config.is_configured():
            self.skipTest("no science repository configured")
        out = os.path.realpath(config.output_root())
        repo = os.path.realpath(config.repo_root())
        self.assertFalse(out == repo or out.startswith(repo + os.sep))

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.get(config.ENV_OUTPUT_DIR)
            os.environ[config.ENV_OUTPUT_DIR] = tmp
            try:
                self.assertEqual(config.output_root(), os.path.abspath(tmp))
            finally:
                if original is None:
                    del os.environ[config.ENV_OUTPUT_DIR]
                else:
                    os.environ[config.ENV_OUTPUT_DIR] = original

    def test_installed_copies_fall_back_to_the_state_dir(self):
        """No checkout means no pyproject.toml above the package.

        An installed wheel must not try to write captures into site-packages.
        """
        real = config._source_checkout_root
        config._source_checkout_root = lambda: None
        original = os.environ.pop(config.ENV_OUTPUT_DIR, None)
        try:
            self.assertTrue(
                config.output_root().startswith(config.state_dir()))
        finally:
            config._source_checkout_root = real
            if original is not None:
                os.environ[config.ENV_OUTPUT_DIR] = original


class TestEnsureOutputDirs(unittest.TestCase):

    def test_nothing_is_created_inside_a_configured_science_repo(self):
        """The promise tests/test_nondestructive.py exists to enforce."""
        if not paths.have_science_repo():
            self.skipTest("no science repository configured")
        self.assertEqual(paths.ensure_output_dirs(), [],
                         "SReTo created directories inside the science repo")

    def test_the_tree_is_created_and_is_idempotent(self):
        if paths.have_science_repo():
            self.skipTest("a science repository owns these directories")
        paths.ensure_output_dirs()
        for d in paths.output_dirs():
            self.assertTrue(os.path.isdir(d), f"{d} was not created")
        # A second call must be a no-op, not a second round of makedirs.
        self.assertEqual(paths.ensure_output_dirs(), [])

    def test_the_layout_mirrors_the_science_repo(self):
        """So pointing at a real repo later changes only the root."""
        if paths.have_science_repo():
            self.skipTest("a science repository owns these directories")
        self.assertEqual(os.path.basename(paths.DATA_DIR), "02_DATA")
        self.assertEqual(os.path.basename(paths.FIGURES_DIR), "03_FIGURES")
        self.assertEqual(os.path.basename(paths.ANALYSIS_DIR), "ANALYSIS PLOTS")
        self.assertEqual(os.path.basename(paths.SOOP_DIR), "SOOP_AVAILABILITY")
        self.assertEqual(os.path.dirname(paths.ANALYSIS_DIR), paths.FIGURES_DIR)
        self.assertEqual(os.path.dirname(paths.SOOP_DIR), paths.FIGURES_DIR)


class TestOutputRootIsCrossPlatform(unittest.TestCase):
    """The fallback must land somewhere sane on each platform's conventions."""

    def _output_root_on(self, platform):
        import importlib
        import sys

        real_platform = sys.platform
        saved = {k: os.environ.pop(k, None)
                 for k in (config.ENV_OUTPUT_DIR, config.ENV_STATE_DIR)}
        sys.platform = platform
        module = importlib.reload(config)
        real_checkout = module._source_checkout_root
        module._source_checkout_root = lambda: None       # simulate a wheel
        try:
            return module.output_root(), module.state_dir()
        finally:
            module._source_checkout_root = real_checkout
            sys.platform = real_platform
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
            importlib.reload(config)

    def test_macos_installed_copy_stays_under_application_support(self):
        out, _state = self._output_root_on("darwin")
        self.assertIn("Library/Application Support", out)

    def test_linux_installed_copy_stays_under_the_state_dir(self):
        out, state = self._output_root_on("linux")
        self.assertTrue(out.startswith(state))

    def test_no_platform_writes_into_the_package(self):
        """An installed wheel must never put gigabytes into site-packages."""
        package = os.path.dirname(os.path.abspath(config.__file__))
        for platform in ("darwin", "linux", "win32"):
            out, _state = self._output_root_on(platform)
            self.assertFalse(os.path.abspath(out).startswith(package),
                             f"{platform} would write inside the package")


class TestOutputTreeIsGitIgnored(unittest.TestCase):
    """A capture is gigabytes. It must be impossible to commit one by accident."""

    def test_gitignore_covers_the_output_tree(self):
        checkout = config._source_checkout_root()
        if checkout is None:
            self.skipTest("not running from a source checkout")
        path = os.path.join(checkout, ".gitignore")
        if not os.path.isfile(path):                       # pragma: no cover
            self.skipTest("no .gitignore in this checkout")
        with open(path, encoding="utf-8") as f:
            lines = {line.strip() for line in f}
        self.assertIn("output/", lines,
                      "config.output_root() writes to <checkout>/output, which "
                      ".gitignore does not cover")


if __name__ == "__main__":
    unittest.main()
