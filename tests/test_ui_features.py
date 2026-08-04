"""
The interface features that are easy to get subtly wrong: zoom, the status
model, constellation identity, the skymap projection and the location cache.

Headless where it can be. The two font tests need a Tk interpreter, so they
create a withdrawn root and skip cleanly if no display is available — the suite
has to stay runnable over SSH during a capture.
"""

import os
import tempfile
import time
import unittest

from sreto import location, skymap, status, theme


def _tk_root():
    """A withdrawn Tk root, or None when there is no display."""
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


# ── zoom ──────────────────────────────────────────────────────────────────
class TestZoomSteps(unittest.TestCase):
    """No Tk needed: the step arithmetic is pure."""

    def test_steps_are_sorted_and_include_actual_size(self):
        self.assertEqual(list(theme.SCALE_STEPS), sorted(theme.SCALE_STEPS))
        self.assertIn(1.0, theme.SCALE_STEPS)

    def test_stepping_up_and_down_is_symmetric(self):
        self.assertEqual(theme.snap_scale(1.0, +1), 1.15)
        self.assertEqual(theme.snap_scale(1.15, -1), 1.0)

    def test_stepping_stops_at_the_ends(self):
        self.assertEqual(theme.snap_scale(theme.MAX_SCALE, +1), theme.MAX_SCALE)
        self.assertEqual(theme.snap_scale(theme.MIN_SCALE, -1), theme.MIN_SCALE)

    def test_an_arbitrary_value_snaps_to_the_nearest_stop(self):
        self.assertEqual(theme.snap_scale(1.31), 1.30)
        self.assertEqual(theme.snap_scale(0.4), theme.MIN_SCALE)

    def test_font_sizes_never_collapse(self):
        for key in theme.BASE_SIZES:
            self.assertGreaterEqual(theme.scaled(key, 0.1), 7,
                                    f"{key} would become unreadable when zoomed out")


class TestZoomAppliesToFonts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        if cls.root is None:
            raise unittest.SkipTest("no display")
        theme.apply_theme(cls.root)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            theme.set_scale(1.0)
            cls.root.destroy()

    def tearDown(self):
        theme.set_scale(1.0)

    def test_named_fonts_resize_together(self):
        before = {name: getattr(theme.F, name).cget("size")
                  for name in ("ui", "small", "mono", "submenu", "title")}
        theme.set_scale(1.5)
        for name, was in before.items():
            now = getattr(theme.F, name).cget("size")
            self.assertGreater(now, was, f"{name} did not grow with the zoom")

    def test_the_same_font_object_is_shared_not_copied(self):
        """A widget built before the zoom must resize with it. That only holds
        if widgets reference the named font rather than a (family, size)."""
        import tkinter as tk
        label = tk.Label(self.root, text="x", font=theme.F.ui)
        before = tk.font.Font(font=label.cget("font")).cget("size")
        theme.set_scale(1.75)
        after = tk.font.Font(font=label.cget("font")).cget("size")
        self.assertGreater(after, before,
                           "an existing widget did not follow the zoom — the "
                           "font was copied into it instead of shared")
        label.destroy()

    def test_submenu_titles_stand_out_from_body_text(self):
        self.assertGreater(theme.BASE_SIZES["submenu"],
                           theme.BASE_SIZES["ui"] + 5,
                           "the panel headings are not distinct enough from "
                           "the body text to work as headings")

    def test_scale_is_clamped(self):
        self.assertEqual(theme.set_scale(99.0), theme.MAX_SCALE)
        self.assertEqual(theme.set_scale(0.01), theme.MIN_SCALE)

    def test_listeners_are_notified(self):
        """Canvas widgets draw in pixels and cannot ride on a font resize, so
        they subscribe. If this stops firing, the compass and skymap freeze at
        one size while the text around them grows."""
        seen = []
        listener = theme.on_scale_change(seen.append)
        try:
            theme.set_scale(1.3)
            self.assertEqual(seen[-1], 1.3)
        finally:
            theme._scale_listeners.remove(listener)

    def test_a_broken_listener_cannot_break_the_zoom(self):
        def explode(_scale):
            raise RuntimeError("boom")

        listener = theme.on_scale_change(explode)
        try:
            self.assertEqual(theme.set_scale(1.15), 1.15)
        finally:
            theme._scale_listeners.remove(listener)


# ── status ────────────────────────────────────────────────────────────────
class TestStatusModel(unittest.TestCase):

    def test_every_state_has_a_label_a_colour_and_lamps(self):
        for state in (status.IDLE, status.BUSY, status.STRAINED, status.FAILED):
            self.assertIn(state, status.STATE_LABELS)
            self.assertIn(state, status.STATE_COLORS)
            self.assertIn(state, status.STATE_LAMPS)
            for lamp in status.STATE_LAMPS[state]:
                self.assertIn(lamp, status.LAMP_COLORS)

    def test_the_overload_state_is_named_the_way_the_user_asked(self):
        self.assertEqual(status.STATE_LABELS[status.STRAINED],
                         "barely holding on")

    def test_strain_only_moves_between_busy_and_strained(self):
        m = status.StatusModel()
        m.set_idle()
        self.assertEqual(m.refresh_strain(), status.IDLE)
        m.set_failed("MAIN.py")
        self.assertEqual(m.refresh_strain(), status.FAILED,
                         "a failure was masked by the machine being idle")

    def test_failure_is_sticky_until_something_else_starts(self):
        m = status.StatusModel()
        m.set_failed("MAIN.py", "exit 1")
        self.assertEqual(m.state, status.FAILED)
        m.refresh_strain()
        self.assertEqual(m.state, status.FAILED)
        m.set_busy("capture.sh")
        self.assertEqual(m.state, status.BUSY)

    def test_titles_name_the_job(self):
        m = status.StatusModel()
        m.set_busy("capture.sh")
        self.assertIn("capture.sh", m.title_suffix())
        m.set_failed("MAIN.py")
        self.assertIn("FAILED", m.title_suffix())
        m.set_idle()
        self.assertEqual(m.title_suffix(), "idle")

    def test_strained_title_reports_the_load(self):
        m = status.StatusModel()
        m.set_busy("capture.sh")
        m._load = 1.8
        m.state = status.STRAINED
        self.assertIn("1.8", m.title_suffix())

    def test_is_running_covers_both_active_states(self):
        m = status.StatusModel()
        m.set_busy("x")
        self.assertTrue(m.is_running())
        m.state = status.STRAINED
        self.assertTrue(m.is_running())
        m.set_idle()
        self.assertFalse(m.is_running())

    def test_load_factor_is_normalised_per_core(self):
        status._load_cache["unix"] = 0.0
        value = status.load_factor()
        self.assertGreaterEqual(value, 0.0)
        self.assertLess(value, 100.0)

    def test_colour_mixing(self):
        self.assertEqual(status._mix("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(status._mix("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(status._mix("#000000", "#ffffff", 0.5), "#808080")


# ── constellations ────────────────────────────────────────────────────────
class TestConstellationIdentity(unittest.TestCase):

    def test_the_families_this_project_targets_are_recognised(self):
        for name, expected in (
                ("IRIDIUM 113", "IRIDIUM"),
                ("GLOBALSTAR M073", "GLOBALSTAR"),
                ("NAVSTAR 81 (USA 319)", "GPS"),
                ("GPS BIIF-12", "GPS"),
                ("GSAT0220 (GALILEO 26)", "GALILEO"),
                ("NOAA 19", "NOAA"),
                ("METOP-C", "METOP"),
                ("SAOCOM 1A", "SAOCOM"),
                ("ORBCOMM FM 108", "ORBCOMM")):
            self.assertEqual(theme.constellation_of(name), expected, name)

    def test_unknown_names_fall_back_without_raising(self):
        self.assertEqual(theme.constellation_of("SOMETHING NEW"), "OTHER")
        self.assertEqual(theme.constellation_of(""), "OTHER")
        self.assertEqual(theme.constellation_of(None), "OTHER")

    def test_geostationary_targets_get_their_own_colour(self):
        self.assertEqual(theme.constellation_of("UNKNOWN SAT", geo=True), "GEO")

    def test_every_family_has_a_colour(self):
        for key, _needles in theme._CONSTELLATION_PATTERNS:
            self.assertIn(key, theme.CONSTELLATION_COLORS,
                          f"{key} is classified but has no colour")

    def test_channel_colours_are_never_used_for_satellites(self):
        """C1/C2 mean rx1/rx2 everywhere in this project. A satellite dot in
        either colour would read as a channel."""
        for key, colour in theme.CONSTELLATION_COLORS.items():
            self.assertNotEqual(colour, theme.C1, key)
            self.assertNotEqual(colour, theme.C2, key)

    def test_colours_are_distinguishable(self):
        colours = [c for k, c in theme.CONSTELLATION_COLORS.items()
                   if k != "OTHER"]
        self.assertEqual(len(colours), len(set(colours)),
                         "two constellations share a colour")


# ── the skymap ────────────────────────────────────────────────────────────
class TestSkymapProjection(unittest.TestCase):

    def test_interpolation_between_samples(self):
        self.assertEqual(skymap.interpolate_track([0, 10], [0, 100], 5), 50.0)
        self.assertEqual(skymap.interpolate_track([0, 10], [0, 100], 0), 0.0)

    def test_outside_the_sampled_span_returns_nothing(self):
        self.assertIsNone(skymap.interpolate_track([0, 10], [0, 100], 11))
        self.assertIsNone(skymap.interpolate_track([0, 10], [0, 100], -1))

    def test_azimuth_wraps_the_short_way_across_north(self):
        """359° -> 1° is +2°, not -358°. Getting this wrong sends a dot the
        whole way round the compass mid-pass."""
        value = skymap.interpolate_azimuth([0, 10], [359.0, 1.0], 5)
        self.assertAlmostEqual(value, 0.0, places=6)

    def test_azimuth_stays_in_range(self):
        for when in range(0, 11):
            value = skymap.interpolate_azimuth([0, 10], [350.0, 10.0], when)
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 360.0)

    def test_mismatched_tracks_do_not_raise(self):
        self.assertIsNone(skymap.interpolate_track([0, 1], [0], 0.5))
        self.assertIsNone(skymap.interpolate_azimuth([], [], 0))

    def test_short_names_fit_a_canvas(self):
        for name in ("IRIDIUM 113", "GLOBALSTAR M073", "NOAA 19"):
            short = skymap.SkyPosition(name, "1", 0, 0).short_name()
            self.assertLessEqual(len(short), 10, f"{name} -> {short}")

    def test_a_position_carries_its_constellation_colour(self):
        p = skymap.SkyPosition("IRIDIUM 113", "42803", 90.0, 45.0)
        self.assertEqual(p.color, theme.CONSTELLATION_COLORS["IRIDIUM"])


class TestSkymapGeometry(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        if cls.root is None:
            raise unittest.SkipTest("no display")
        theme.apply_theme(cls.root)
        cls.map = skymap.SkyMap(cls.root, size=200)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            cls.root.destroy()

    def test_zenith_is_the_centre(self):
        x, y = self.map._to_xy(0.0, 90.0)
        self.assertAlmostEqual(x, self.map.size / 2.0, places=3)
        self.assertAlmostEqual(y, self.map.size / 2.0, places=3)

    def test_north_is_up_and_east_is_right(self):
        centre = self.map.size / 2.0
        nx, ny = self.map._to_xy(0.0, 0.0)
        ex, ey = self.map._to_xy(90.0, 0.0)
        self.assertAlmostEqual(nx, centre, places=3)
        self.assertLess(ny, centre, "north is not up")
        self.assertGreater(ex, centre, "east is not right")
        self.assertAlmostEqual(ey, centre, places=3)

    def test_low_elevation_sits_near_the_rim(self):
        centre = self.map.size / 2.0
        _x, y_low = self.map._to_xy(0.0, 5.0)
        _x2, y_high = self.map._to_xy(0.0, 70.0)
        self.assertLess(abs(y_low - centre) * 0.5, abs(y_low - centre))
        self.assertGreater(abs(y_low - centre), abs(y_high - centre))


# ── location ──────────────────────────────────────────────────────────────
class TestLocationFix(unittest.TestCase):

    def test_short_form_is_readable_and_hemisphered(self):
        self.assertEqual(location.Fix(52.379222, 13.066138).short(),
                         "52.3792°N 13.0661°E")
        self.assertEqual(location.Fix(-33.9, -18.4).short(),
                         "33.9000°S 18.4000°W")

    def test_the_copied_value_keeps_full_precision(self):
        """The tile shows 4 decimals; pasting those into geometry.json would
        move the receiver by metres."""
        fix = location.Fix(52.37922213, 13.06613847)
        self.assertIn("52.37922213", fix.precise())
        self.assertIn("13.06613847", fix.precise())
        self.assertNotEqual(fix.precise(), fix.short())

    def test_age_label_is_the_wording_the_tile_shows(self):
        stamp = time.mktime(time.strptime("2026-03-04 14:32", "%Y-%m-%d %H:%M"))
        label = location.Fix(1.0, 2.0, unix=stamp).age_label()
        self.assertTrue(label.startswith("last updated "), label)
        self.assertIn("04.03.2026", label)

    def test_a_fix_with_no_timestamp_says_so(self):
        self.assertIn("unknown", location.Fix(1.0, 2.0).age_label())

    def test_staleness(self):
        self.assertFalse(location.Fix(1.0, 2.0, unix=time.time()).is_stale())
        self.assertTrue(location.Fix(1.0, 2.0,
                                     unix=time.time() - 40 * 3600).is_stale())

    def test_cache_round_trip(self):
        original = location.CACHE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                location.CACHE_PATH = os.path.join(tmp, "location.json")
                fix = location.Fix(52.5, 13.4, 40.0, "laptop", time.time(), 12.0)
                self.assertTrue(location.save_cached(fix))
                back = location.load_cached()
                self.assertIsNotNone(back)
                self.assertAlmostEqual(back.lat, 52.5)
                self.assertAlmostEqual(back.lon, 13.4)
                self.assertEqual(back.source, "laptop")
                self.assertAlmostEqual(back.accuracy_m, 12.0)
        finally:
            location.CACHE_PATH = original

    def test_a_missing_cache_is_not_an_error(self):
        original = location.CACHE_PATH
        try:
            location.CACHE_PATH = "/nonexistent/dir/location.json"
            self.assertIsNone(location.load_cached())
        finally:
            location.CACHE_PATH = original

    def test_corrupt_cache_is_ignored(self):
        original = location.CACHE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                location.CACHE_PATH = os.path.join(tmp, "location.json")
                with open(location.CACHE_PATH, "w") as f:
                    f.write("{not json")
                self.assertIsNone(location.load_cached())
        finally:
            location.CACHE_PATH = original

    def test_resolve_falls_back_when_no_laptop_fix_is_cached(self):
        original = location.CACHE_PATH
        try:
            location.CACHE_PATH = "/nonexistent/location.json"
            fix = location.resolve("laptop")
            # Either the repo's own coordinates, or None if neither source
            # exists — but never a crash, and never a laptop fix.
            if fix is not None:
                self.assertNotEqual(fix.source, "laptop")
        finally:
            location.CACHE_PATH = original

    def test_clock_labels(self):
        self.assertRegex(location.utc_now_label(), r"^\d{2}:\d{2}:\d{2}$")
        self.assertRegex(location.local_now_label(), r"^\d{2}:\d{2}:\d{2}$")
        self.assertTrue(location.local_tz_label())

    def test_availability_probe_never_raises(self):
        ok, reason = location.corelocation_available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)

    def test_bundle_check_never_raises(self):
        self.assertIsInstance(location.is_bundled_app(), bool)

    def test_a_bare_process_is_rejected_with_the_bundle_fix_named(self):
        """The regression this guards: telling the user to 'grant it to your
        terminal' sends them looking for a setting that cannot exist — a bare
        process has no bundle identifier for macOS to attach a grant to.

        The bundle fix is named on macOS whether or not pyobjc is installed:
        a clean venv (CI installs [dev,images], not [macos]) takes the
        ImportError branch, and that branch must not stop at 'pyobjc is not
        installed' — that is a package name with no explanation attached.
        """
        if location.is_bundled_app():
            raise unittest.SkipTest("test suite is running inside a bundle")
        ok, reason = location.corelocation_available()
        self.assertFalse(ok)
        if os.path.isdir("/System/Library/Frameworks/CoreLocation.framework"):
            self.assertIn("make_desktop_app.sh", reason)
        else:
            # Off macOS there is no bundle to build, so the honest answer is
            # the framework's absence plus the fallback that does work.
            self.assertIn("macOS framework", reason)
            self.assertIn("geometry.json", reason)
        self.assertNotIn("terminal", reason.lower())

    def test_every_unavailability_reason_names_a_fix_or_a_fallback(self):
        """A reason with no next step is a dead end, and this probe's whole
        job is to be the thing that stops the search."""
        ok, reason = location.corelocation_available()
        if ok:
            self.assertEqual(reason, "")
            return
        self.assertTrue(any(hint in reason for hint in
                            ('pip install "sreto[macos]"',
                             "make_desktop_app.sh",
                             "geometry.json")), reason)


if __name__ == "__main__":
    unittest.main()
