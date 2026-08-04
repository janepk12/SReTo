"""
config.py — where the science repository is, and where SReTo may write.

SReTo is a front-end. The instruments it drives — ``capture.sh``,
``soop_capture.sh``, ``soop_planner.py``, ``MAIN.py`` and the analysis modules
they import — live in a separate *science repository* that is NOT bundled with
this package and never will be: it is several gigabytes of code, calibration
data and captures, and it changes on its own schedule.

So the location of that repository is configuration, not a constant. It is
resolved once, from the first source that answers:

    1. ``$SRETO_REPO_ROOT``                      explicit, wins over everything
    2. ``<config dir>/config.json``  key ``repo_root``
    3. the current working directory, or any parent of it, that looks like the
       science repo (see :func:`looks_like_science_repo`)
    4. the parent of the installed package, for a source checkout that happens
       to sit inside the science repo

When nothing answers, ``repo_root()`` returns ``None`` and every consumer is
expected to degrade with a message that names ``SRETO_REPO_ROOT`` — see
:func:`missing_repo_message`. Nothing here raises on import: a GUI that cannot
find the science repo must still start, so that it can *say* so.

WHERE SRETO WRITES.  Never into the science repo. State goes to the platform's
user-data directory (overridable with ``$SRETO_STATE_DIR``), so an installed
copy does not try to write into ``site-packages`` and two checkouts of the
science repo do not fight over one journal.
"""

import json
import os
import sys

# Marker files that identify a directory as the science repository. Deliberately
# few and load-bearing: every one of them is something SReTo actually drives.
REPO_MARKERS = (
    os.path.join("01_CODE", "capture.sh"),
    os.path.join("01_CODE", "MAIN.py"),
)

# The subdirectory of the science repo holding the CLI tools.
CODE_SUBDIR = "01_CODE"

ENV_REPO_ROOT = "SRETO_REPO_ROOT"
ENV_STATE_DIR = "SRETO_STATE_DIR"
ENV_CONFIG_DIR = "SRETO_CONFIG_DIR"

APP_DIRNAME = "sreto"

_repo_root = None
_repo_source = "unresolved"
_resolved = False


# ── the directories SReTo owns ────────────────────────────────────────────
def config_dir():
    """Where ``config.json`` lives. Honours ``$SRETO_CONFIG_DIR`` and XDG."""
    explicit = os.environ.get(ENV_CONFIG_DIR)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if sys.platform == "darwin":
        return os.path.expanduser(
            f"~/Library/Application Support/{APP_DIRNAME}")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIRNAME)


def state_dir():
    """Where the run journal, presets, logs and temp copies go.

    Never inside the science repo and never inside the installed package: an
    installed wheel may sit in a read-only ``site-packages``, and the journal
    describes a *machine*, not a checkout.
    """
    explicit = os.environ.get(ENV_STATE_DIR)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if sys.platform == "darwin":
        return os.path.expanduser(
            f"~/Library/Application Support/{APP_DIRNAME}/state")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state")
    return os.path.join(base, APP_DIRNAME)


def config_path():
    return os.path.join(config_dir(), "config.json")


# ── finding the science repo ──────────────────────────────────────────────
def looks_like_science_repo(path):
    """True when `path` contains the CLI tools SReTo drives."""
    if not path or not os.path.isdir(path):
        return False
    return all(os.path.isfile(os.path.join(path, m)) for m in REPO_MARKERS)


def _from_env():
    value = os.environ.get(ENV_REPO_ROOT)
    if not value:
        return None
    return os.path.abspath(os.path.expanduser(value))


def _from_config_file():
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("repo_root")
    if not value:
        return None
    return os.path.abspath(os.path.expanduser(str(value)))


def _walk_up(start):
    """The first ancestor of `start` (inclusive) that is a science repo."""
    current = os.path.abspath(start)
    while True:
        if looks_like_science_repo(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def resolve(force=False):
    """(root, source). Cached; pass ``force=True`` after changing the config.

    ``root`` is ``None`` when the science repo could not be found. ``source``
    always names where the answer came from, so the GUI can show it — a wrong
    repo is much easier to spot when the reason it was chosen is on screen.
    """
    global _repo_root, _repo_source, _resolved
    if _resolved and not force:
        return _repo_root, _repo_source

    for candidate, source in (
        (_from_env(), f"${ENV_REPO_ROOT}"),
        (_from_config_file(), config_path()),
        (_walk_up(os.getcwd()), "working directory"),
        (_walk_up(os.path.dirname(os.path.abspath(__file__))), "package location"),
    ):
        if candidate is None:
            continue
        # An explicitly configured root is honoured even when the markers are
        # missing: telling the user "your SRETO_REPO_ROOT is wrong" is far more
        # useful than silently falling through to a directory they never named.
        if source.startswith("$") or source.endswith("config.json"):
            _repo_root, _repo_source, _resolved = candidate, source, True
            return _repo_root, _repo_source
        if looks_like_science_repo(candidate):
            _repo_root, _repo_source, _resolved = candidate, source, True
            return _repo_root, _repo_source

    _repo_root, _repo_source, _resolved = None, "not found", True
    return _repo_root, _repo_source


def repo_root():
    """Absolute path of the science repository, or None."""
    return resolve()[0]


def repo_source():
    """Human-readable description of how the repo root was chosen."""
    return resolve()[1]


def code_dir():
    """``<repo>/01_CODE``, or None when the repo is unknown."""
    root = repo_root()
    return os.path.join(root, CODE_SUBDIR) if root else None


def is_configured():
    """True when a science repo was found AND it contains the CLI tools."""
    return looks_like_science_repo(repo_root())


def set_repo_root(path, persist=True):
    """Point SReTo at a science repo, optionally writing it to config.json."""
    root = os.path.abspath(os.path.expanduser(path))
    if persist:
        os.makedirs(config_dir(), exist_ok=True)
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["repo_root"] = root
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    global _resolved
    _resolved = False
    return resolve(force=True)[0]


def missing_repo_message():
    """One block of text explaining what is missing and how to fix it.

    Used by the launcher, the pre-checks tab and the console banner, so the
    instruction a user sees is the same wherever they hit the wall.
    """
    root, source = resolve()
    if root and not looks_like_science_repo(root):
        return (
            f"The configured science repository does not contain the tools "
            f"SReTo drives.\n"
            f"  configured : {root}\n"
            f"  source     : {source}\n"
            f"  expected   : {', '.join(REPO_MARKERS)}\n\n"
            f"Point {ENV_REPO_ROOT} at the repository that holds 01_CODE, or "
            f"run:  sreto --set-repo /path/to/sdr_r")
    return (
        "The science repository was not found, so nothing can be captured, "
        "planned or analysed.\n\n"
        "SReTo is a front-end: capture.sh, soop_capture.sh, soop_planner.py "
        "and MAIN.py live in a separate repository that is not bundled with "
        "this package.\n\n"
        f"Set it once:\n"
        f"  export {ENV_REPO_ROOT}=/path/to/sdr_r\n"
        f"or persist it:\n"
        f"  sreto --set-repo /path/to/sdr_r      (writes {config_path()})\n\n"
        "The GUI still opens without it — every panel that needs the "
        "repository will say so instead of failing silently.")


def summary():
    """[(label, value)] — what the About box and pre-checks display."""
    root, source = resolve()
    return [
        ("science repo", root or "NOT CONFIGURED"),
        ("resolved from", source),
        ("repo usable", "yes" if is_configured() else "no"),
        ("state dir", state_dir()),
        ("config file", config_path()),
    ]
