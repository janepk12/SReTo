"""
Architecture validation for SReTo.

The tests defend ONE property: the science repository keeps working without
SReTo, and SReTo keeps agreeing with it. They check it three ways —

  * test_nondestructive   hashes every file in the repo's 01_CODE, exercises
                          SReTo's non-hardware code paths, and re-hashes.
  * test_capture_contract re-derives capture.sh's prompt order and
                          soop_capture.sh's flag list FROM THOSE SCRIPTS and
                          compares them with what jobs.py sends.
  * test_main_params      proves the MAIN.py parameterisation changes only the
                          lines it claims to, and that MAIN.py is never opened
                          for writing.

TWO CLASSES OF TEST, and the split matters for CI:

  * Repo-independent — sector geometry, the ANSI parser, the diagnostics, the
    skymap projection, zoom. These run anywhere, including a headless runner.
  * Repo-derived — everything above. They read the science repository's own
    scripts, so without one they SKIP rather than pass. Configure
    ``$SRETO_REPO_ROOT`` to actually run them.

A handful additionally need a Tk display and skip cleanly without one.

Run them:  pytest                     (from the repo root)
      or:  python -m tests.run_tests
      or:  sreto --check
"""
