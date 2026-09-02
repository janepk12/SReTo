"""
The prompt/flag contracts, re-derived from the shell scripts themselves.

jobs.py answers capture.sh's questions positionally. If someone inserts a prompt
into capture.sh, the GUI would keep sending the same list and every answer after
the insertion point would land in the wrong variable — a capture at the wrong
frequency with a plausible-looking log. Nothing would raise.

So these tests do not hard-code the contract. They PARSE capture.sh and
soop_capture.sh and compare the result with what jobs.py believes.
"""

import re
import unittest

from sreto import jobs, paths


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

_READ_RE = re.compile(r'^\s*read\s+-p\s+"(?P<prompt>[^"]*)"\s+(?P<var>\w+)\s*$',
                      re.MULTILINE)
_DEFAULT_RE = re.compile(r'^\s*(?P<var>\w+)=\$\{(?P=var):-(?P<default>[^}]*)\}',
                         re.MULTILINE)
_CASE_FLAG_RE = re.compile(r'^\s*(--[a-z-]+(?:\|--[a-z-]+)*)\)', re.MULTILINE)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestCaptureShPromptContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = _read(paths.CAPTURE_SH)
        cls.prompt_vars = [m.group("var") for m in _READ_RE.finditer(cls.src)]
        cls.defaults = {m.group("var"): m.group("default")
                        for m in _DEFAULT_RE.finditer(cls.src)}

    def test_prompt_order_matches_jobs_py(self):
        """The first 13 prompts are unconditional and must line up exactly."""
        expected = [k for k, _label, _default in jobs.CAPTURE_PROMPTS][:-1]
        actual = self.prompt_vars[:len(expected)]
        self.assertEqual(
            actual, expected,
            "capture.sh's prompt order no longer matches jobs.CAPTURE_PROMPTS.\n"
            f"  capture.sh : {actual}\n"
            f"  jobs.py    : {expected}\n"
            "Every answer after the first difference would go to the WRONG "
            "parameter. Update jobs.CAPTURE_PROMPTS and capture_answers().")

    def test_final_prompt_is_the_mode_dependent_amount(self):
        """The last question is samples-in-M or timeout-in-seconds."""
        tail = self.prompt_vars[len(jobs.CAPTURE_PROMPTS) - 1:]
        self.assertEqual(set(tail), {"n", "timeout_sec"},
                         f"expected the branch prompts n / timeout_sec, got {tail}")

    def test_no_prompts_were_added(self):
        self.assertEqual(
            len(self.prompt_vars), len(jobs.CAPTURE_PROMPTS) + 1,
            f"capture.sh asks {len(self.prompt_vars)} questions; jobs.py answers "
            f"{len(jobs.CAPTURE_PROMPTS)} (one of which covers both branch "
            f"prompts). A prompt was added or removed.")

    def test_blank_answers_select_the_scripts_own_defaults(self):
        """Every prompt has a ${var:-default}, which is what blank relies on."""
        missing = [v for v in self.prompt_vars
                   if v not in self.defaults and v != "n"]
        self.assertEqual(missing, [],
                         f"prompts without a shell default: {missing} — sending "
                         f"a blank line for these would set them EMPTY, not "
                         f"default.")

    def test_documented_defaults_match_the_script(self):
        for key, _label, gui_default in jobs.CAPTURE_PROMPTS:
            if key == "amount":
                continue        # branch-dependent; covered above
            script_default = self.defaults.get(key)
            self.assertEqual(
                str(script_default), str(gui_default),
                f"the GUI shows '{gui_default}' as capture.sh's default for "
                f"{key}, but the script uses '{script_default}'")

    def test_agc_sentinel_values_match(self):
        """The gain prompt is skipped only for a literal on/ON."""
        for value in jobs.AGC_ON_VALUES:
            self.assertIn(f'"$agc_rx1" != "{value}"', self.src)
            self.assertIn(f'"$agc_rx2" != "{value}"', self.src)

    def test_answer_count_tracks_the_agc_branches(self):
        base = len(jobs.capture_answers({}))
        self.assertEqual(base, len(jobs.CAPTURE_PROMPTS),
                         "with AGC off, every prompt gets one answer")
        self.assertEqual(len(jobs.capture_answers({"agc_rx1": "on"})), base - 1)
        self.assertEqual(len(jobs.capture_answers({"agc_rx2": "ON"})), base - 1)
        self.assertEqual(
            len(jobs.capture_answers({"agc_rx1": "on", "agc_rx2": "on"})), base - 2)

    def test_blank_form_produces_all_blank_answers(self):
        """An untouched form must run capture.sh's default capture."""
        answers = jobs.capture_answers({})
        self.assertTrue(all(a == "" for a in answers),
                        f"a blank form should send only blank lines, got {answers}")

    def test_base_dir_matches_the_gui(self):
        """capture.sh writes into CAPTURES_DIR/<stem>/; the GUI reveals the
        same parent folder.

        capture.sh no longer hardcodes a BASE_DIR literal — it sources
        01_CODE/paths.env (see capture.sh's own comment on why: soop_capture.sh
        used to recover the directory by grepping 'BASE_DIR=' out of THIS file,
        which could drift the moment the literal moved) and builds
        CAPTURE_DIR="${CAPTURES_DIR}/${CAPTURE_STEM}". So the real contract is
        no longer a single regex against capture.sh's text; it is that
        capture.sh actually sources paths.env and keys its capture directory
        off CAPTURES_DIR, and that repo_paths.py (the Python parser of that
        SAME paths.env) resolves CAPTURES_DIR to what sreto.paths computes by
        hand. Importing repo_paths.py directly (pure stdlib — os/re/pathlib,
        exactly like sreto._lazy_orbits does for orbits.py) is the strongest
        version of that check: it runs the SAME parser capture.sh's own
        Python-side tools trust, rather than a second regex that could drift
        from repo_paths.py's the way the old BASE_DIR regex drifted from
        capture.sh.
        """
        # WHERE paths.env sits is layout-dependent (01_CODE/paths.env in the
        # flat layout, 01_CODE/gui/config/paths.env in the package one), so the
        # contract is that capture.sh SOURCES it — not the literal path it uses.
        # Pinning the path made this fail the moment the science repo moved a
        # file it was never really asserting anything about.
        self.assertRegex(
            self.src, r'(?m)^\s*\.\s+"[^"]*paths\.env"',
            "capture.sh no longer sources paths.env — the single source of "
            "truth for CAPTURES_DIR/DATA_DIR/etc. that both capture.sh and "
            "sreto.paths are supposed to agree with.")
        self.assertIn(
            'CAPTURE_DIR="${CAPTURES_DIR}/${CAPTURE_STEM}"', self.src,
            "capture.sh no longer keys its output directory off "
            "${CAPTURES_DIR} — sreto.paths.CAPTURES_DIR would then be "
            "pointing the GUI's 'reveal output folder' at the wrong place.")

        # Load repo_paths.py BY LOCATION rather than by name. It moved into the
        # application package (01_CODE/gui/repo_paths.py), so `import
        # repo_paths` after a sys.path insert now resolves to nothing — and,
        # worse, would resolve to the wrong file on a machine that happens to
        # have another module of that name importable.
        import importlib.util  # noqa: PLC0415
        import os              # noqa: PLC0415
        candidates = (os.path.join(paths.APP_DIR, "repo_paths.py"),
                      os.path.join(paths.CODE_DIR, "repo_paths.py"))
        src_path = next((c for c in candidates if os.path.isfile(c)), None)
        self.assertIsNotNone(
            src_path,
            f"repo_paths.py — the Python parser of paths.env — is at none of "
            f"{candidates}. capture.sh and sreto.paths have no shared "
            f"definition of CAPTURES_DIR without it.")
        spec = importlib.util.spec_from_file_location("_science_repo_paths",
                                                      src_path)
        repo_paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repo_paths)

        self.assertEqual(
            str(repo_paths.CAPTURES_DIR).rstrip("/"), paths.CAPTURES_DIR.rstrip("/"),
            "repo_paths.CAPTURES_DIR (parsed from 01_CODE/paths.env, what "
            "capture.sh actually writes to) and sreto.paths.CAPTURES_DIR "
            "(hand-derived) disagree — the GUI would reveal the wrong folder "
            "and analysis_panel would search the wrong directory for captures.")
        self.assertEqual(
            str(repo_paths.DATA_DIR).rstrip("/"), paths.DATA_DIR.rstrip("/"))
        self.assertEqual(
            str(repo_paths.ANALYSIS_DIR).rstrip("/"), paths.ANALYSIS_DIR.rstrip("/"))


class TestSoopCaptureFlagContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = _read(paths.SOOP_CAPTURE_SH)
        cls.flags = set()
        for m in _CASE_FLAG_RE.finditer(cls.src):
            cls.flags.update(m.group(1).split("|"))

    def _emitted_flags(self, values):
        argv = jobs.autocapture_job(values).argv
        return {a for a in argv if a.startswith("--")}

    def test_every_flag_the_gui_can_emit_is_parsed_by_the_script(self):
        values = {
            "for": "6h", "sat": "IRIDIUM", "min_elev": "20", "count": "3",
            "max_sec": "60", "budget_gb": "50", "gain": "30", "bw": "10",
            "sr": "10", "lead": "5", "plan": "/tmp/plan.tsv",
            "include_geo": True, "refresh": True, "run_mode": "dry-run",
        }
        emitted = self._emitted_flags(values)
        unknown = sorted(emitted - self.flags)
        self.assertEqual(unknown, [],
                         f"the GUI emits flags soop_capture.sh does not accept: "
                         f"{unknown} (it exits 1 on an unknown option)")

    def test_alternate_branches_are_also_valid(self):
        for values in ({"session_mode": "until", "until": "22:00"},
                       {"next_only": True},
                       {"no_refresh": True, "run_mode": "list"}):
            unknown = sorted(self._emitted_flags(values) - self.flags)
            self.assertEqual(unknown, [], f"{values} -> unknown flags {unknown}")

    def test_untouched_form_passes_no_flags(self):
        argv = jobs.autocapture_job({}).argv
        self.assertEqual(argv, ["bash", paths.SOOP_CAPTURE_SH],
                         "an untouched form must run soop_capture.sh with its "
                         "own defaults and no flags at all")

    def test_prompt_contract_is_still_documented_in_the_script(self):
        """soop_capture.sh answers the same prompts; the GUI mirrors its order."""
        self.assertIn("in its exact ask order", self.src,
                      "soop_capture.sh's prompt-contract comment is gone — the "
                      "shared assumption behind jobs.capture_answers() is no "
                      "longer stated anywhere in the script.")

    def test_planner_flags_exist(self):
        planner_src = _read(paths.SOOP_PLANNER_PY)
        for flag in ("--selftest", "--mode", "--hours"):
            self.assertIn(f'"{flag}"', planner_src,
                          f"soop_planner.py no longer accepts {flag}")


if __name__ == "__main__":
    unittest.main()
