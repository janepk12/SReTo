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

- **`sreto.radios`** — the SDR support catalogue as data: which radios are
  driven today, which are coming, the host tool and command line each one
  needs, and how many coherent RX chains it has. Kept as a module rather than
  prose so the CLI, the pre-check hints and the README cannot drift apart.
- **`sreto --radios`** — prints that catalogue, including the fact that
  `--radio` is not a flag yet, so nobody discovers that by typing one.
- **An output tree SReTo creates for itself** when no science repository is
  configured: `output/02_DATA`, `output/03_FIGURES/ANALYSIS PLOTS` and
  `output/03_FIGURES/SOOP_AVAILABILITY`, next to the checkout and covered by a
  new `output/` line in `.gitignore` so a multi-gigabyte capture can never be
  committed. The layout mirrors the science repo, so `sreto --set-repo` later
  changes only which root the names hang off. Installed copies (no checkout)
  use `<state dir>/output` instead of writing into `site-packages`. Override
  with `$SRETO_OUTPUT_DIR`. Nothing is ever created inside a *configured*
  science repository — `ensure_output_dirs()` returns immediately in that case.
- **A `text rendering` pre-check**, and a `fonts` block in `sreto --check`,
  reporting the families Tk resolved and warning when it cannot draw past
  Latin-1 — the condition that makes the GUI look broken while working. The
  hint names the usual cause (a venv built on conda's Xft-less Tk) and the fix.
- **`tests/test_fonts_and_output.py`** — asserts a resolved family is one Tk
  will not substitute (which is what catches the `fixed` regression), that
  every glyph has an ASCII stand-in, and that the output tree is created,
  idempotent, git-ignored, and never placed inside the science repo.
  Font resolution and the output root are additionally exercised against
  reproduced macOS / Xft / X-core family lists and against each platform's
  directory conventions, because the macOS path cannot be run on Linux CI and
  "it was never broken there" is only true until someone changes the resolver.
- **`make_desktop_app.sh` warns when the interpreter it baked in has no Xft.**
  It prefers the conda env, which on Linux is exactly the Tk that renders the
  bitmap font — and a desktop launcher makes that choice permanent. macOS is
  exempt: Aqua has no Xft and needs none.
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

- **The Linux GUI rendered entirely in the X11 `fixed` bitmap font** — a chunky
  terminal face, nothing like the macOS build — while raising no error, because
  nothing was failing. Two bugs compounded in `theme._first_available`:
  `tkfont.families()` reports lower-cased names on X11 (`dejavu sans`), so the
  case-sensitive match against a title-cased candidate list could never hit;
  and the fallback then passed `family="TkDefaultFont"`, which is a *named
  font*, not a family. Tk answers an unknown family with `fixed` instead of an
  error, so the branch meant to be the safe one produced the worst available
  result. Matching now folds case, and the fallback resolves the named font to
  the family it actually points at — the desktop's own UI font.
- **Characters past Latin-1 became hex boxes on a Tk without Xft.** Such a Tk
  can only reach the X11 core fonts, whose scalable families are ISO8859-1
  only. `theme.glyph()` now returns an ASCII stand-in (`->`, `...`, `RET`,
  `(i)`) when that is the case, so the degraded configuration is plain rather
  than broken-looking. Latin-1 itself is safe everywhere and is still used
  freely (`·`, `°`, `±`, `µ`, `—`).
- **Capture and figure paths named a directory nothing had created.** With no
  science repository, `02_DATA`, `03_FIGURES/ANALYSIS PLOTS` and
  `03_FIGURES/SOOP_AVAILABILITY` were derived from a `no-science-repo`
  placeholder: the paths were displayed in the banner and the About box, the
  status-bar reveal buttons opened them, and the disk pre-check tried to `stat`
  them — all pointing at nothing. They now fall back to `config.output_root()`,
  a tree SReTo owns and creates on first start.

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
