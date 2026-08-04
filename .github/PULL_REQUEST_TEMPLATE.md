<!--
Thanks for contributing. CONTRIBUTING.md has the setup and the house style;
this is just the checklist.
-->

## What this changes

<!-- One or two sentences. Link the issue if there is one: Fixes #123 -->

## Why

<!-- The reasoning where it is not obvious from the diff. This project's
     docstrings explain *why* rather than *what*, and so should PRs. -->

## How it was verified

<!-- Which tests, on which platform, with or without a science repository.
     If hardware was involved, say which radio and what you captured. -->

## Checklist

- [ ] `ruff check src tests` is clean
- [ ] `pytest` passes locally (skips are fine and are printed with `-ra`)
- [ ] `sreto --check` still reports every module importable
- [ ] No new write target outside `sreto.config.state_dir()` — or, if there is
      one, `tests/test_nondestructive.py` was updated to cover it
- [ ] Nothing in this PR modifies the science repository
- [ ] `CHANGELOG.md` updated under `[Unreleased]` if the change is user-visible
- [ ] References to science-repo files still cite the right line numbers
