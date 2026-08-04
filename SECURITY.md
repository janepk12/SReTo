# Security Policy

## Supported versions

SReTo is pre-1.0. Only the latest release on `main` receives fixes.

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's [private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (Security → Report a vulnerability), or email the maintainer
address in `pyproject.toml`.

Please include the SReTo version (`sreto --version`), your OS and Python
version, and the smallest reproduction you can manage. Expect an
acknowledgement within a week; this is a research tool maintained by one
person, so please be patient with the fix timeline.

## What is in scope

SReTo executes external programs by design — that is its entire job. The
following are **intended behaviour**, not vulnerabilities:

- It runs `capture.sh`, `soop_capture.sh`, `soop_planner.py` and `MAIN.py` from
  the configured science repository. Pointing `$SRETO_REPO_ROOT` at a directory
  you do not trust is equivalent to running the scripts in it yourself.
- It runs those tools in a **pty**, so they receive a real terminal.
- It opens figures and folders with the platform handler (`open`,
  `xdg-open`).

Genuinely in scope:

- Anything that makes SReTo write **outside** its state directory —
  particularly into the science repository, which
  `tests/test_nondestructive.py` exists to prevent.
- Command or argument injection from a value SReTo did not construct itself
  (a plan TSV field, a filename, a `config.json` entry) reaching an argv or a
  shell.
- Path traversal out of the configured roots.
- Credentials, tokens or absolute home paths leaking into the run journal,
  saved logs, or a crash report.

## Hardening notes

- SReTo has **no required third-party runtime dependencies**, so its supply
  chain is the Python standard library plus whatever the science environment
  brings.
- Everything SReTo writes lives under `sreto.config.state_dir()`; nothing is
  written into the installed package or the science repository.
- The lock screen is a **consent gate, not a security boundary**. It stops a
  stray click from starting a capture. It does not stop anyone with access to
  the machine.
