# sreto/assets

Drop-in slots. Nothing here is required — SReTo runs without any of it.

Assets are searched in **two** directories, user first:

1. `<state dir>/assets/` — writable, survives reinstalls. This is where your
   files should go. Find the exact path with `sreto --where`.
2. this directory, inside the installed package — read-only in a wheel, and
   wiped by the next `pip install --upgrade`.

| File | Used by | If missing |
|---|---|---|
| `institution_logo.png` | lock screen | a plain typographic placeholder is drawn |
| `app_icon.png` | desktop shortcut | a default icon is generated from the palette |
| `loading_screen.png` | lock screen backdrop | the blur uses the layout likeness alone |
| `branding.json` | lock screen, credits | author is read from `git config user.name`; institution/department/location stay blank |

## institution_logo.png

Named for the institution this project was originally built at; the file is
**not** bundled regardless of which institution you use it for — a logo is not
something a build script should invent, or that this project should
redistribute. Save your own institution's mark here (PNG, ideally with
transparency, at least 600 px wide) and the lock screen picks it up on the next
launch. It is scaled to fit 300×96.

## app_icon.png

Either drop a square-ish image in, or let the build make one:

```bash
scripts/make_desktop_app.sh --icon ~/Pictures/whatever.jpg
```

Any aspect ratio works — it is centre-cropped to a square, not squashed.
To regenerate the default icon: `python -m sreto.make_icon`.

## loading_screen.png

The lock screen's backdrop photograph. `.jpg`, `.jpeg` and `.webp` also work.
It is cover-fitted so it is never squashed, Gaussian-blurred, with a faint
likeness of the live layout composited on top. Replacing it with a
higher-resolution file is a file copy — nothing to configure.

## branding.json

Overrides `sreto/branding.py` without touching code. This file lives in the
**user** assets directory, not the package, so it is per-machine and never
committed — it is the right place for a real name, institution or physical
location. The shipped defaults are blank/generic on purpose (author falls
back to `git config user.name`; institution, department and location stay
empty and are simply omitted from the credits dialog until set here):

```json
{
  "author": "A. Researcher",
  "role": "Research Assistant",
  "institution": "Example Institute",
  "department": "Example Department",
  "location": "City, Country",
  "extra_credits": [
    ["Supervision", "..."],
    ["Field support", "..."]
  ]
}
```
