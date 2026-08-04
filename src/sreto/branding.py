"""
branding.py — who made this, and what it is built on.

Everything the lock screen and the credits dialog display comes from here, so
there is one place to correct a name or add a collaborator.

TWO ASSET DIRECTORIES, searched in this order:

    USER_ASSETS_DIR   <state dir>/assets   — writable, survives reinstalls
    ASSETS_DIR        the installed package's assets/ — read-only in a wheel

That split is what makes an installed copy configurable at all: a user cannot
drop files into site-packages, and anything they did drop there would be
deleted by the next `pip install --upgrade`.

Values can be overridden without touching code by dropping a JSON file at
``<state dir>/assets/branding.json``, e.g.

    {
      "author": "Luke Pugin",
      "role": "GNSS-R researcher",
      "institution": "GFZ Helmholtz Centre for Geosciences",
      "extra_credits": [["Field support", "…"]]
    }

The GFZ logo is NOT bundled — this module only points at a slot. Drop the
official asset in either assets directory as ``gfz_logo.png`` and it is picked
up automatically; until then a plain typographic placeholder is drawn, because
inventing an institution's mark is not something a build script should do.
"""

import json
import os
import subprocess

from . import config, paths

ASSETS_DIR = paths.ASSETS_DIR
USER_ASSETS_DIR = os.path.join(config.state_dir(), "assets")


def asset_path(name):
    """The user's copy of `name` if it exists, else the packaged one.

    Returns the USER path when neither exists, because the only reason to ask
    for a missing asset is to tell someone where to put it.
    """
    user = os.path.join(USER_ASSETS_DIR, name)
    if os.path.isfile(user):
        return user
    packaged = os.path.join(ASSETS_DIR, name)
    if os.path.isfile(packaged):
        return packaged
    return user


BRANDING_JSON = os.path.join(USER_ASSETS_DIR, "branding.json")
LOGO_PNG = asset_path("gfz_logo.png")
APP_ICON_PNG = asset_path("app_icon.png")

# The lock screen's backdrop photograph. Any of these names is picked up, in
# order, so dropping in a higher-resolution replacement is a file copy — the
# extension does not have to match what is there now.
LOADING_IMAGE_NAMES = ("loading_screen.png", "loading_screen.jpg",
                       "loading_screen.jpeg", "loading_screen.webp")

APP_NAME = "SReTo"
APP_LONG_NAME = "SDR Reflectometry Toolkit"
APP_TAGLINE = "SReTo · Plan, Capture and Analyse"


def _git_user_name():
    try:
        out = subprocess.run(["git", "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_DEFAULTS = {
    "author": "Luke Joshua Pugin",                    # filled from git config when empty
    "contact": "lukepugin@gmail.com",
    "role": "Student Research Assistant",
    "institution": "GFZ Helmholtz Centre for Geosciences",
    "department": "Section 1.1.: Space Geodetic Techniques",
    "location": "Telegrafenberg, Potsdam",
    "extra_credits": [],
}


def _load():
    data = dict(_DEFAULTS)
    try:
        with open(asset_path("branding.json"), encoding="utf-8") as f:
            override = json.load(f)
        if isinstance(override, dict):
            data.update({k: v for k, v in override.items() if v is not None})
    except (OSError, ValueError):
        pass
    if not data.get("author"):
        data["author"] = _git_user_name() or "Unattributed"
    return data


INFO = _load()


def author():
    return INFO["author"]

def contact():
    return INFO["contact"]


def institution():
    return INFO["institution"]


def logo_path():
    """The GFZ logo, or None when the slot has not been filled."""
    path = asset_path("gfz_logo.png")
    return path if os.path.isfile(path) else None


def app_icon_path():
    path = asset_path("app_icon.png")
    return path if os.path.isfile(path) else None


def loading_image_path():
    """The lock screen backdrop photo, or None when no slot is filled."""
    for name in LOADING_IMAGE_NAMES:
        path = asset_path(name)
        if os.path.isfile(path):
            return path
    return None


def credit_rows():
    """[(heading, text)] — the body of the credits dialog."""
    from . import __version__

    rows = [
        ("Author", f"{INFO['author']} — {INFO['role']}"),
        ("Institution", INFO["institution"]),
        ("Department", INFO["department"]),
        ("Contact", INFO["contact"]),
        ("Receiver site", INFO["location"]),
        ("", ""),
        ("Application", f"{APP_NAME} — {APP_LONG_NAME} v{__version__}"),
        ("Purpose",
         "Front-end for the bistatic reflectometry pipeline in the science "
         "repository's 01_CODE. rx1 = direct (RE), rx2 = ground-reflected (GR)."),
        ("", ""),
        ("Pipeline", "capture.sh · soop_capture.sh · soop_planner.py · MAIN.py\n"
                     "waterfalls · iq_dashboard · band_correlator · physics · "
                     "orbits · fresnel · satellites · sdr_core · console"),
        ("Hardware", "Nuand bladeRF, dual-channel coherent RX"),
        ("", ""),
        ("Built on", "Python · tkinter/ttk · NumPy · SciPy · Matplotlib · "
                     "Skyfield · Pillow"),
        ("Orbital data", "TLEs from CelesTrak (celestrak.org)"),
        ("Time zone", "Displays Europe/Berlin; machine files stay UTC"),
        ("", ""),
        ("Design note",
         "SReTo is strictly non-destructive: it launches the CLI tools as "
         "subprocesses and never modifies the science repository. Every tool "
         "remains fully usable from a terminal."),
    ]
    for heading, text in INFO.get("extra_credits") or []:
        rows.append((str(heading), str(text)))
    return rows


def ensure_assets_dir():
    """The WRITABLE assets directory — the packaged one may be read-only."""
    os.makedirs(USER_ASSETS_DIR, exist_ok=True)
    return USER_ASSETS_DIR
