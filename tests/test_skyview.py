"""
Sky-view masking: the geometry, the mask logic, and the lock.

The mask decides which passes are worth capturing, so the sector arithmetic has
to be right at the wrap point (a sector centred on North spans 337.5°–22.5°,
which is the one place an off-by-one hides) and the elevation floor has to be
applied per sector, not globally after the fact.

Propagation is exercised against the real plan when one exists, and skipped
cleanly when it does not — the suite must stay runnable on a fresh checkout.
"""

import os
import time
import unittest

from sreto import branding, paths
from sreto import skyview as sv
from sreto import soop_availability as sa


class TestSectorGeometry(unittest.TestCase):

    def test_cardinal_bearings(self):
        for az, expected in ((0, "N"), (45, "NE"), (90, "E"), (135, "SE"),
                             (180, "S"), (225, "SW"), (270, "W"), (315, "NW")):
            self.assertEqual(sv.sector_of(az), expected, f"az={az}")

    def test_north_wraps_correctly(self):
        """The only boundary that crosses 0°/360°."""
        for az in (337.5, 350, 359.9, 0, 10, 22.4):
            self.assertEqual(sv.sector_of(az), "N", f"az={az}")
        self.assertEqual(sv.sector_of(22.6), "NE")
        self.assertEqual(sv.sector_of(337.4), "NW")

    def test_azimuth_is_normalised(self):
        self.assertEqual(sv.sector_of(360), "N")
        self.assertEqual(sv.sector_of(720 + 90), "E")
        self.assertEqual(sv.sector_of(-90), "W")

    def test_every_bearing_lands_in_exactly_one_sector(self):
        counts = {}
        for tenth in range(3600):
            counts[sv.sector_of(tenth / 10.0)] = counts.get(sv.sector_of(tenth / 10.0), 0) + 1
        self.assertEqual(set(counts), set(sv.SECTOR_NAMES))
        # 45° each, sampled at 0.1° -> 450 samples per sector.
        for name, n in counts.items():
            self.assertEqual(n, 450, f"{name} covers {n / 10.0}°, expected 45°")

    def test_presets_reference_only_real_sectors(self):
        for label, sectors in sv.PRESETS.items():
            self.assertTrue(sectors <= sv.ALL_SECTORS, label)
            self.assertTrue(sectors, f"{label} is empty")

    def test_full_sky_preset_is_everything(self):
        self.assertEqual(sv.PRESETS["Full sky"], sv.ALL_SECTORS)


class TestHorizonMask(unittest.TestCase):

    def test_full_sky_accepts_any_azimuth_above_the_floor(self):
        m = sv.HorizonMask(sv.ALL_SECTORS, min_elev_deg=10)
        self.assertTrue(m.is_full_sky())
        for az in range(0, 360, 17):
            self.assertTrue(m.contains(az, 11))
            self.assertFalse(m.contains(az, 9))

    def test_sector_mask_rejects_closed_directions(self):
        m = sv.HorizonMask({"S"}, min_elev_deg=10)
        self.assertTrue(m.contains(180, 30))
        self.assertFalse(m.contains(0, 80), "north is closed, elevation is irrelevant")
        self.assertFalse(m.contains(180, 5), "floor still applies inside an open sector")

    def test_per_sector_floor_overrides_the_global_one(self):
        m = sv.HorizonMask({"S", "W"}, min_elev_deg=10,
                           per_sector_min={"W": 35})
        self.assertTrue(m.contains(180, 12))       # south: global floor
        self.assertFalse(m.contains(270, 12))      # west: obstructed to 35°
        self.assertTrue(m.contains(270, 40))

    def test_blind_mask(self):
        m = sv.HorizonMask(set())
        self.assertTrue(m.is_blind())
        self.assertFalse(m.contains(180, 89))
        self.assertIn("nothing", m.describe().lower())

    def test_describe_names_a_preset_when_it_matches(self):
        m = sv.HorizonMask(sv.PRESETS["South view (SE·S·SW)"], min_elev_deg=15)
        self.assertIn("South view", m.describe())
        self.assertIn("15", m.describe())

    def test_round_trip_through_dict(self):
        m = sv.HorizonMask({"N", "E"}, min_elev_deg=22.5,
                           per_sector_min={"N": 30})
        back = sv.HorizonMask.from_dict(m.to_dict())
        self.assertEqual(back.open_sectors, m.open_sectors)
        self.assertEqual(back.min_elev_deg, m.min_elev_deg)
        self.assertEqual(back.per_sector_min, m.per_sector_min)

    def test_from_dict_survives_rubbish(self):
        for junk in (None, [], "nope", {"open_sectors": ["Q", "ZZ"]}):
            mask = sv.HorizonMask.from_dict(junk)
            self.assertTrue(mask.open_sectors <= sv.ALL_SECTORS)


class TestReceiverAndEngine(unittest.TestCase):

    def test_receiver_comes_from_the_plan_header_when_available(self):
        rx = sv.receiver_location()
        if rx is None:
            self.skipTest("no plan and no geometry.json on this machine")
        lat, lon, alt, source = rx
        self.assertTrue(-90 <= lat <= 90)
        self.assertTrue(-180 <= lon <= 180)
        self.assertIn(source, ("plan header", "geometry.json"))

    def test_pass_without_a_tle_reports_instead_of_raising(self):
        engine = sv.VisibilityEngine(receiver=(52.0, 13.0, 100.0, "test"))
        p = sa.SatellitePass(name="no-tle", catnr="1",
                             rise_unix=time.time(), set_unix=time.time() + 300)
        v = engine.evaluate(p, sv.HorizonMask({"S"}))
        self.assertFalse(v.visible)
        self.assertIn("TLE", v.error)

    def test_blind_mask_short_circuits(self):
        engine = sv.VisibilityEngine(receiver=(52.0, 13.0, 100.0, "test"))
        p = sa.SatellitePass(name="x", rise_unix=0, set_unix=1)
        self.assertIn("no sectors", engine.evaluate(p, sv.HorizonMask(set())).error)


class TestAgainstTheRealPlan(unittest.TestCase):
    """Only runs where a plan with TLEs exists."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(paths.PLAN_TSV):
            raise unittest.SkipTest("no capture plan on this machine")
        cls.passes = sa.CapturePlanProvider().passes(horizon_h=168, min_elev_deg=10)
        if not cls.passes:
            raise unittest.SkipTest("plan has no passes")
        cls.engine = sv.VisibilityEngine()
        if not cls.engine.receiver:
            raise unittest.SkipTest("no receiver coordinates")

    def test_full_sky_sees_every_pass_the_planner_listed(self):
        """The planner already applied an elevation mask, so nothing drops out."""
        mask = sv.HorizonMask(sv.ALL_SECTORS, min_elev_deg=10)
        sample = self.passes[:12]
        for p in sample:
            v = self.engine.evaluate(p, mask)
            if v.error:
                continue
            self.assertTrue(v.visible,
                            f"{p.name} peaks at {p.peak_el_deg:.1f}° but is "
                            f"invisible under a full-sky 10° mask")

    def test_a_narrow_mask_removes_some_passes(self):
        narrow = sv.HorizonMask({"N"}, min_elev_deg=10)
        wide = sv.HorizonMask(sv.ALL_SECTORS, min_elev_deg=10)
        sample = self.passes[:25]
        n_narrow = sum(1 for p in sample if self.engine.evaluate(p, narrow).visible)
        n_wide = sum(1 for p in sample if self.engine.evaluate(p, wide).visible)
        self.assertLess(n_narrow, n_wide,
                        "a single 45° sector accepted as many passes as the "
                        "whole sky — the azimuth test is not being applied")

    def test_visible_window_lies_inside_the_pass(self):
        mask = sv.HorizonMask(sv.PRESETS["South view (SE·S·SW)"], min_elev_deg=10)
        for p in self.passes[:20]:
            v = self.engine.evaluate(p, mask)
            if not v.visible:
                continue
            self.assertGreaterEqual(v.start_unix, p.rise_unix - 1)
            self.assertLessEqual(v.end_unix, p.set_unix + 1)
            self.assertLessEqual(v.best_el_deg, p.peak_el_deg + 0.5,
                                 "best visible elevation exceeded the pass peak")
            self.assertTrue(0 < v.fraction <= 1.0)

    def test_best_visible_azimuth_is_inside_an_open_sector(self):
        sectors = {"SE", "S", "SW"}
        mask = sv.HorizonMask(sectors, min_elev_deg=10)
        for p in self.passes[:20]:
            v = self.engine.evaluate(p, mask)
            if v.visible:
                self.assertIn(sv.sector_of(v.best_az_deg), sectors,
                              f"{p.name}: best-visible azimuth "
                              f"{v.best_az_deg:.1f}° is outside the mask")

    def test_changing_the_mask_reuses_the_cached_track(self):
        sample = self.passes[:8]
        self.engine.evaluate_many(sample, sv.HorizonMask({"S"}))
        self.assertEqual(self.engine.cached_count(sample), len(sample))
        t0 = time.time()
        self.engine.evaluate_many(sample, sv.HorizonMask({"N", "E"}))
        self.assertLess(time.time() - t0, 1.0,
                        "re-applying a mask re-propagated instead of using the "
                        "cache — tracks do not depend on the mask")

    def test_suggested_capture_window_covers_the_visible_part(self):
        mask = sv.HorizonMask(sv.PRESETS["South view (SE·S·SW)"], min_elev_deg=10)
        for p in self.passes[:20]:
            v = self.engine.evaluate(p, mask)
            if not v.visible:
                continue
            start, duration = sv.suggest_capture_window(p, v, lead_s=10)
            self.assertLessEqual(start, v.start_unix)
            self.assertGreaterEqual(start + duration, v.end_unix - 1)
            _s, capped = sv.suggest_capture_window(p, v, lead_s=10, max_s=30)
            self.assertLessEqual(capped, 30.0)
            return
        self.skipTest("no visible pass in the sample")


class TestBranding(unittest.TestCase):

    def test_author_is_resolved(self):
        self.assertTrue(branding.author())
        self.assertNotEqual(branding.author().strip(), "")

    def test_credit_rows_are_pairs_of_strings(self):
        for row in branding.credit_rows():
            self.assertEqual(len(row), 2)
            self.assertIsInstance(row[0], str)
            self.assertIsInstance(row[1], str)

    def test_logo_slot_is_optional(self):
        """No bundled institution mark — the placeholder path must be exercisable."""
        self.assertIn(branding.logo_path(), (None, branding.LOGO_PNG))

    def test_assets_live_inside_the_gui_package(self):
        self.assertTrue(
            os.path.abspath(branding.ASSETS_DIR).startswith(
                os.path.abspath(paths.GUI_DIR)))


class TestLockContract(unittest.TestCase):
    """The lock must gate job execution, not merely cover the buttons."""

    def test_run_job_checks_the_lock_before_anything_else(self):
        import ast
        import inspect

        from sreto import app as app_module
        tree = ast.parse(inspect.getsource(app_module.App))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_job":
                body = ast.unparse(node)
                self.assertIn("lock", body.split("self.manager.busy")[0],
                              "run_job must test the lock BEFORE it starts "
                              "anything — a panel calling it directly would "
                              "otherwise bypass the overlay entirely")
                return
        self.fail("App.run_job not found")

    def test_unlock_key_is_a_single_printable_character(self):
        from sreto import lockscreen
        self.assertEqual(len(lockscreen.UNLOCK_KEY), 1)
        self.assertTrue(lockscreen.UNLOCK_KEY.isalpha())


if __name__ == "__main__":
    unittest.main()
