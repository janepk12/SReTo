"""
sreto — SDR Reflectometry Toolkit.

A desktop front-end for a bistatic GNSS-R / SoOp-R experiment: plan which
satellites are worth recording, drive a two-channel SDR to record the direct
and ground-reflected signal, and run the analysis chain over the result.

STRICTLY NON-DESTRUCTIVE. SReTo launches the science repository's existing
tools as subprocesses and never modifies them: capture.sh, soop_capture.sh,
soop_planner.py and MAIN.py all keep working exactly as standalone CLI tools,
and a capture started here is logged by the same script, in the same files, as
one started from a terminal. tests/test_nondestructive.py enforces that with a
content hash of every file in the repository.

Launch:  sreto                    (console entry point)
   or:   python -m sreto
   or:   sreto --check            validation suite, no window, no radio

The science repository is NOT bundled. Point SReTo at it once:

    export SRETO_REPO_ROOT=/path/to/sdr_r
    sreto --set-repo /path/to/sdr_r     # persists it

Modules:
    app.py                the window and the job lifecycle
    config.py             where the science repo is; where SReTo may write
    paths.py              every filesystem location, derived from config
    theme.py              the plotting palette + the scalable named fonts
    widgets.py            reusable form pieces
    header.py             the title tile: UTC/local clocks and coordinates
    status.py             app state, the traffic light and the spinner
    panels/               one module per tab
    runner.py             pty-backed subprocess execution
    ansi_console.py       the embedded terminal
    jobs.py               GUI values -> CLI invocations (the prompt contract)
    radio_settings.py     the radio parameters shared between manual and auto
    main_params.py        MAIN.py parameterisation without editing MAIN.py
    prechecks.py          pre-flight checks
    history.py            the run journal and the master logs
    soop_availability.py  pass providers (plan file + placeholder)
    skyview.py            the azimuth mask and the TLE track cache
    skymap.py             the polar az/el view drawn from those tracks
    location.py           receiver coordinates, cached with their timestamp
    system_open.py        Finder / file-manager and figure opening
"""

__version__ = "0.1.0"
