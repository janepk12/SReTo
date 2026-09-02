# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security / Privacy

- **No real name, email or institution ships in the source anymore.** The
  `branding.py` defaults (`author`, `contact`, `institution`, `department`,
  `location`) were hardcoded to the original author's identity and workplace;
  they are now blank, and `author` falls back to `git config user.name`. Real
  values belong in `<state dir>/assets/branding.json`, which was already
  gitignored per-machine state and is unaffected.
- **The lock screen no longer hardcodes an institution name.** It previously
  drew "GFZ" / "Helmholtz Centre for Geosciences" as literal text regardless of
  configuration; it now shows `branding.institution()` (blank by default) and
  omits the line entirely when unset.
- `credit_rows()` omits identity rows (institution, department, contact,
  receiver site) instead of showing them blank.
- The `gfz_logo.png` asset slot is renamed `institution_logo.png` — a generic
  name for a slot any installation can use, documented in
  `src/sreto/assets/README.md`.
- `pyproject.toml` authorship, `LICENSE` and the README's license line now
  read `janepk12`, matching the account this project is published from.

### Added

- **Both science-repo layouts are supported.** The science repository moved its
  tooling out of a flat `01_CODE/` and into an application package at
  `01_CODE/gui/` (`scripts/`, `analysis/`, `config/`), leaving only `MAIN.py`
  outside it. SReTo ships independently of that repo and is pinned to neither
  version, so `config.looks_like_science_repo()` and every path in `paths.py`
  now accept either location, per file rather than wholesale — a half-migrated
  clone still resolves whatever it has. `paths.SCIENCE_LAYOUT` reports which
  one was found, and `paths.ANALYSIS_SRC_DIR` / `CONFIG_SRC_DIR` /
  `SCRIPTS_SRC_DIR` give the parity checks one name to ask instead of repeating
  the conditional.

### Fixed

- **The sky-view filter silently reported zero visible passes** against a
  current science repo. `_lazy_orbits` located `orbits.py` by inserting
  `01_CODE` on `sys.path` and doing `import orbits`; once that file moved into
  the application package the import found nothing, every azimuth went
  unresolved, and the mask reported "no passes" instead of an error — the worst
  possible failure for a planning tool, because an empty sky looks like a quiet
  night. It is now loaded **by location** from `paths.ANALYSIS_SRC_DIR`, which
  also removes the chance of picking up an unrelated `orbits` module that
  happens to be importable on the host.
- `test_capture_contract` loaded the science repo's `repo_paths.py` by name for
  the same reason and had the same failure mode; it now loads it by location.
- The capture-directory contract test pinned the literal string
  `. "${SDRR_ROOT}/01_CODE/paths.env"`. Where `paths.env` lives is
  layout-dependent; the contract is that `capture.sh` *sources* it, so that is
  what is asserted now.

- **`sreto.radios`** — the SDR support catalogue as data: which radios are
  driven today, which are coming, the host tool and command line each one
  needs, and how many coherent RX chains it has. Kept as a module rather than
  prose so the CLI, the pre-check hints and the README cannot drift apart.
- **`sreto --radios`** — prints that catalogue, including the fact that
  `--radio` is not a flag yet, so nobody discovers that by typing one.
- **`tests/test_radios.py`** — asserts the catalogue is well-formed and, most
  importantly, that a single-channel radio can never be listed as supported or
  as merely pending. One tuner cannot produce a carrier phase difference, and
  no amount of future work changes that.
- Project infrastructure: `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and pull
  request templates, `dependabot.yml`, `MANIFEST.in`, `.editorconfig` and a
  `.pre-commit-config.yaml` mirroring the CI lint job.
- README: a step-by-step **Download & install** section with a graded
  requirements table, a **Supported radios** section with the per-radio probe
  and capture commands, a command reference, and a troubleshooting table.

### Fixed

- **The window could not open without a science repository** — the project's
  headline claim, and it had stopped being true. `AnalysisPanel.__init__`
  called `load_defaults()`, which read `MAIN.py` off a repository that was not
  there, and the `FileNotFoundError` escaped through `App._build`. A fresh
  `pip install` on a machine with no repository produced no window at all.
  `main_params._read_source` now raises the `MainParamError` every caller
  already handles, naming `--set-repo` as the fix.
- **…and then blocked on a modal instead.** With the error handled, the panel
  raised a `messagebox.showerror` during construction — before the root window
  had ever been mapped — so `App()` hung forever on a dialog with nothing to be
  modal to and no way to dismiss it. `load_defaults(interactive=False)` is used
  at construction and reports on the console; the 'Reload MAIN.py defaults'
  button still gets the dialog, because a button press is a question.
- **`tests/test_opens_without_repo.py`** guards both. Neither was visible
  before because every other test either skips without a repository or never
  builds the real window; the gap was needing a display *and* no repository at
  once, which is precisely what a new install has.
- **`location.corelocation_available()` names a fix in every branch.** Without
  pyobjc it said only "pyobjc is not installed in this environment" — a package
  name with no explanation, and on Linux it pointed at something that cannot
  help. It now gives the install command on macOS and the working fallback
  elsewhere. The test asserting this was itself passing only by accident, on
  machines that happened to have pyobjc; it fails on a clean venv and on CI,
  which install `[dev,images]`, not `[macos]`.
- **`ruff check src tests` is clean**, so the CI lint job can pass. Two
  pre-existing import-ordering errors in `location.py` were failing it.

### Changed

- **`tests/run_tests.py` puts `src/` on `sys.path`.** `python -m tests.run_tests`
  from a source checkout that had not been pip-installed failed with
  `ModuleNotFoundError: sreto` before the first test ran; pytest was unaffected
  because `pyproject.toml` sets `pythonpath = ["src"]`.
- The `bladeRF-cli not on PATH` pre-check hint now points at `sreto --radios`.
- `tests/test_diagnostics.py`'s traceback fixture no longer carries the
  reporter's absolute home directory. Nothing in the test depended on the paths
  being absolute.

## [0.1.0] — 2026-08-04

First release as a standalone project. The application itself was previously
an in-tree `gui/` package inside the science repository; this release extracts
it into an installable Python distribution without changing what it does.

### Added

- **`sreto.config`** — the science repository's location is now configuration
  rather than a constant derived from `__file__`. Resolved, in order, from
  `$SRETO_REPO_ROOT`, `<config dir>/config.json`, the working directory, or the
  package location. `sreto --set-repo PATH` persists it.
- **`sreto --check` / `--where` / `--set-repo` / `--version`** — a real CLI, so
  the two questions that must be answerable without a window (where is the
  repository, is this install intact) have answers.
- **`[project.scripts] sreto`** — `pip install .` yields a launchable command.
- **Graceful degradation without the science repository.** The window opens,
  the console explains what is missing, `App.run_job()` refuses to start
  anything, and `_lazy_orbits` raises with the fix instead of a bare
  `ImportError: orbits`.
- **A "Science repository" pre-check**, listed first, so a missing repository
  is reported once rather than as eight downstream failures.
- **`tests/support.py`** with `requires_science_repo` / `requires_display`
  decorators, so environment-dependent tests report as skipped rather than
  silently passing.
- Packaging and project infrastructure: `pyproject.toml` (PEP 621), MIT
  `LICENSE`, `CONTRIBUTING.md`, this changelog, ruff and pytest configuration,
  and a GitHub Actions workflow running lint plus the suite on macOS and Linux
  across Python 3.11 and 3.12.

### Changed

- **Package renamed `gui` → `sreto`**, moved to a `src/` layout. Intra-package
  relative imports were unaffected; `python -m gui`, the `sys.path`
  manipulation in `__main__.py`, and every string reference to `gui/…` were
  rewritten.
- **State moved out of the source tree.** Was `gui/.state/`; now the platform
  user-data directory (`~/Library/Application Support/sreto/state` on macOS,
  `$XDG_STATE_HOME/sreto` on Linux), overridable with `$SRETO_STATE_DIR`. An
  installed wheel must not write into `site-packages`, and the run journal
  describes a machine rather than a checkout.
- **Assets are searched in two places** — a writable user assets directory
  first, then the packaged one. Drop-in slots (`gfz_logo.png`, `app_icon.png`,
  `loading_screen.*`, `branding.json`) survive a reinstall.
- **`prechecks.check_python_env` probes the interpreter that actually runs the
  analysis.** With SReTo installed in its own environment, `find_spec("numpy")`
  in the GUI's interpreter answered a question nobody asked; when
  `$SRETO_PYTHON` differs from `sys.executable`, the modules are probed in a
  subprocess of the correct interpreter.
- **`paths.python_executable()` honours `$SRETO_PYTHON`**, so the GUI and the
  pipeline no longer have to share one environment.
- **The non-destructive test got stricter.** `EXCLUDED_DIRS` no longer needs to
  exclude the application's own directory, because the package is no longer
  inside the tree it promises not to modify. Two new tests assert that the
  package and the state directory both live outside the science repository.
- `make_desktop_app.sh` builds the bundle around the installed console script
  and carries `$SRETO_REPO_ROOT` into it.

### Removed

- `launch_gui.sh` — superseded by the `sreto` console entry point. Its conda
  interpreter discovery moved into `scripts/dev_launch.sh` for source
  checkouts.
- Bundled binary assets (`app_icon.png`, `gfz_logo.png`, `loading_screen.png`).
  They are user-supplied drop-in slots, the GFZ mark is not the project's to
  redistribute, and the icon is regenerated by `make_desktop_app.sh`.

### Known gaps

The application is **not yet runnable standalone** — it drives tools that live
in the science repository. See "Roadmap to standalone" in the README for the
precise list.

[Unreleased]: https://github.com/janepk12/SReTo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/janepk12/SReTo/releases/tag/v0.1.0
