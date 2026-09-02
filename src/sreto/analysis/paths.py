"""
sreto.analysis.paths — where the ported analysis pipeline reads captures from
and writes figures to.

Mirrors 01_CODE/repo_paths.py's contract (capture_stem, analysis_dir(),
FIG_WATERFALLS/DASHBOARD/PHYSICS, find_capture()) so a capture analysed by
this package and a capture analysed by sdr_r's own repo_paths.py land in
IDENTICAL locations whenever the same science repo is configured — that is
the whole point of the port: same input, same output, regardless of which
tree ran it.

Two ways this resolves, exactly mirroring sreto/config.py and sreto/paths.py:

  * a science repo is configured (sreto.config.is_configured()) -> rooted at
    that repo's 02_DATA / 03_FIGURES, exactly where 01_CODE/paths.env points
    a checkout of sdr_r itself.
  * no science repo is configured -> rooted at sreto.config.state_dir(), so a
    bare `pip install sreto[analysis]` with no separately-cloned sdr_r still
    computes and writes real output end to end. Nothing here is ever
    hardcoded to a particular machine or home directory — every path is
    derived from sreto.config at call time.

Nothing in this module raises on import (same rule as sreto/config.py); the
one place a caller can hit a raise is resolve_input(), and it raises with a
message that says exactly what was searched and how to fix it — never a bare
traceback from deep inside the DSP code.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import config

# ── per-capture figure kinds (mirrors repo_paths.FIG_* / paths.env) ────────
FIG_WATERFALLS = "waterfalls"
FIG_DASHBOARD = "dashboard"
FIG_PHYSICS = "physics"
FIG_KINDS = (FIG_WATERFALLS, FIG_DASHBOARD, FIG_PHYSICS)


def using_science_repo() -> bool:
    """True when a configured science repo is what this module is rooted at."""
    return bool(config.repo_root()) and config.is_configured()


def _root() -> Path:
    """Where captures/figures live: the configured science repo if there is
    one (byte-for-byte the same layout sdr_r's own paths.env describes), else
    SReTo's own state directory (self-contained mode — never the science
    repo, same rule sreto/config.py already keeps for the GUI's own state)."""
    if using_science_repo():
        return Path(config.repo_root())
    return Path(config.state_dir()) / "standalone-data"


def data_dir() -> Path:
    return _root() / "02_DATA"


def captures_dir() -> Path:
    """One directory per capture stem — the post-restructure layout."""
    return data_dir() / "captures"


def legacy_data_dir() -> Path:
    """Pre-restructure flat capture layout. Readers fall back to this so a
    half-migrated (or never-migrated) science repo still resolves; this
    package never writes new captures here. Mirrors repo_paths.LEGACY_DATA_DIR."""
    return data_dir()


def figures_dir() -> Path:
    return _root() / "03_FIGURES"


def analysis_root() -> Path:
    """03_FIGURES/10_ANALYSIS — same subpath sdr_r's paths.env names."""
    return figures_dir() / "10_ANALYSIS"


# ── stems (identical logic to repo_paths.py) ────────────────────────────────
def capture_stem(path) -> str:
    p = Path(path)
    if p.is_dir():
        return p.name
    return p.name.split(".")[0]


def capture_dir(stem_or_path) -> Path:
    return captures_dir() / capture_stem(stem_or_path)


def capture_file(stem_or_path, suffix: str) -> Path:
    stem = capture_stem(stem_or_path)
    return capture_dir(stem) / f"{stem}{suffix}"


def find_capture(stem_or_path, suffix: str = ".bin"):
    """Locate a capture file in the new layout, then the legacy flat one.
    Returns a Path or None. Mirrors repo_paths.find_capture exactly."""
    stem = capture_stem(stem_or_path)
    candidate = capture_file(stem, suffix)
    if candidate.exists():
        return candidate
    legacy = legacy_data_dir() / f"{stem}{suffix}"
    return legacy if legacy.exists() else None


def resolve_input(json_or_bin_or_stem):
    """Accept a full path, a bare filename, or a stem; return the resolved
    .json path. Raises FileNotFoundError with a message that names exactly
    what was searched and how to fix it, instead of a bare traceback three
    modules downstream.
    """
    p = Path(json_or_bin_or_stem)
    direct = p if p.suffix == ".json" else p.with_suffix(".json")
    if direct.is_absolute() and direct.exists():
        return direct
    if direct.exists():
        return direct.resolve()

    found = find_capture(json_or_bin_or_stem, ".json")
    if found is not None:
        return found

    where = (f"the configured science repository at {config.repo_root()}"
             if using_science_repo() else
             f"SReTo's own standalone data directory ({data_dir()}) — no "
             f"science repository is configured")
    raise FileNotFoundError(
        f"Cannot find a capture matching {str(json_or_bin_or_stem)!r}.\n"
        f"Looked in:\n"
        f"  - {capture_dir(json_or_bin_or_stem)}\n"
        f"  - {legacy_data_dir()}\n\n"
        f"Searched {where}.\n"
        f"Point SReTo at a science repo (export SRETO_REPO_ROOT=/path/to/sdr_r, "
        f"or `sreto --set-repo /path/to/sdr_r`), or place the .bin/.json pair "
        f"in the directory above.")


# ── 03_FIGURES/10_ANALYSIS/<stem>/<kind>/ ──────────────────────────────────
def analysis_dir(stem_or_path, kind: str = "") -> Path:
    """Where this capture's figures go. `kind` is one of FIG_KINDS, or ''.

    Keyed by capture stem so two different captures can never collide —
    identical contract to repo_paths.analysis_dir().
    """
    if kind and kind not in FIG_KINDS:
        raise ValueError(f"unknown figure kind {kind!r}; expected one of {FIG_KINDS}")
    base = analysis_root() / capture_stem(stem_or_path)
    return base / kind if kind else base


def ensure_analysis_dir(stem_or_path, kind: str = "") -> Path:
    d = analysis_dir(stem_or_path, kind)
    d.mkdir(parents=True, exist_ok=True)
    return d


def rel(path) -> str:
    """Repo/state-root-relative path for display; absolute passthrough
    when outside both roots."""
    try:
        return str(Path(path).resolve().relative_to(_root()))
    except ValueError:
        return str(path)
