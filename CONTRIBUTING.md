# Contributing

## Setup

```bash
git clone https://github.com/janepk12/SReTo.git
cd SReTo
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,images]"
pre-commit install                       # optional, mirrors the CI lint job

export SRETO_REPO_ROOT=/path/to/sdr_r    # optional, see below
pytest
```

Every path in this project is relative to the checkout or derived at runtime —
`.venv` lives inside the checkout and `SRETO_REPO_ROOT` is the one location you
supply. Nothing is hardcoded to a home directory.

`tkinter` cannot be pip-installed. If `python -c "import tkinter"` fails:
`brew install python-tk` (macOS), `sudo apt install python3-tk` (Debian). A venv
inherits tkinter from the interpreter it was created with, so create the venv
from a Python that already has it.

## The one rule

**SReTo never writes into the science repository.** It launches
`capture.sh`, `soop_capture.sh`, `soop_planner.py` and `MAIN.py` as
subprocesses and reads their output; all four must stay fully usable from a
terminal, and a capture started from the GUI must be logged by the same script,
in the same files, as one started by hand.

This is enforced by `tests/test_nondestructive.py`, which hashes every file in
the repository, exercises SReTo's whole non-hardware surface, and hashes again.
If you need to change something in the science repository, change it there — in
its own repository, in its own commit.

Everything SReTo writes goes under `sreto.config.state_dir()`. If you add a new
write target, add it to `test_sreto_writes_only_into_its_own_state_dir`.

## Tests

Two classes, and the distinction matters:

| Class | Needs | Behaviour without it |
|---|---|---|
| Repo-independent | nothing | always run |
| Repo-derived | `$SRETO_REPO_ROOT` | **skip**, with the reason printed |
| Display-dependent | a Tk display | **skip**, with the reason printed |

Skips are reported (`pytest -ra`), never silent. A test that returns early
without skipping reads as a pass, which is the exact failure mode this suite
exists to catch — so use `tests/support.requires_science_repo` /
`requires_display`, or a module-level `setUpModule` guard, rather than an `if`.

CI runs the suite **without** a science repository and fails the build if fewer
than 40 tests actually execute, so the repo-independent half cannot quietly
stop running.

### Adding a radio

`src/sreto/radios.py` is the single source of truth for SDR support — the CLI,
the pre-check hints and the README table all read from it. Add the entry there,
never in prose, and be honest about `rx_channels`: a single-tuner device is
`LIMITED`, not `PLANNED`, because it cannot produce a carrier phase difference
and `tests/test_radios.py` will fail if it is listed otherwise.

Several tests deliberately re-derive their expectations from the shell scripts
by parsing them (`test_capture_contract.py`, `test_radio_settings.py`,
`test_theme_matches_plots.py`). Do not replace that parsing with hardcoded
values — the point is that a change to `capture.sh` becomes a failing test
rather than a capture at the wrong frequency.

## Style

```bash
ruff check src tests
```

House conventions, all load-bearing:

- **Docstrings explain *why*.** Most modules open with the failure that
  motivated them. Keep that; it is the most useful documentation here.
- **References to the science repo cite file and line** (`capture.sh:250`,
  `MAIN.py:275`). Update them when you notice drift.
- **No heavy imports at module scope.** `numpy`, `matplotlib` and `skyfield`
  belong to the subprocess, not to the GUI. `_lazy_orbits.py` is the only place
  the pipeline's propagator is imported, and it is deferred to first use.
- **Nothing heavy on the Tk thread.** Worker threads hand results back through
  `App.post_to_ui()`; touching the Tcl interpreter from another thread raises
  in the *worker*, where the traceback is invisible.
- **Named Tk fonts, never `(family, size)` tuples** — a tuple freezes at widget
  construction and breaks zoom.

## Commits and PRs

Conventional-ish subject lines (`fix:`, `feat:`, `docs:`, `refactor:`), a body
explaining the reasoning where it is not obvious, and green CI. Update
`CHANGELOG.md` under `[Unreleased]` for anything user-visible.
