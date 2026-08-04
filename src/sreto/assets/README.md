# sreto/assets

Drop-in slots. Nothing here is required — SReTo runs without any of it.

Assets are searched in **two** directories, user first:

1. `<state dir>/assets/` — writable, survives reinstalls. This is where your
   files should go. Find the exact path with `sreto --where`.
2. this directory, inside the installed package — read-only in a wheel, and
   wiped by the next `pip install --upgrade`.

| File | Used by | If missing |
|---|---|---|
| `gfz_logo.png` | lock screen | a plain typographic placeholder is drawn |
| `app_icon.png` | desktop shortcut | a default icon is generated from the palette |
| `loading_screen.png` | lock screen backdrop | the blur uses the layout likeness alone |
| `branding.json` | lock screen, credits | author is read from `git config user.name` |

## gfz_logo.png

The official GFZ mark is **not** bundled — an institution's logo is not
something a build script should invent, or that this project should
redistribute. Save the real asset here (PNG, ideally with transparency, at
least 600 px wide) and the lock screen picks it up on the next launch. It is
scaled to fit 300×96.

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

Overrides `sreto/branding.py` without touching code:

```json
{
  "author": "Luke Pugin",
  "role": "Student Research Assistant",
  "institution": "GFZ Helmholtz Centre for Geosciences",
  "department": "Section 1.1.: Space Geodetic Techniques",
  "location": "Telegrafenberg, Potsdam",
  "extra_credits": [
    ["Supervision", "..."],
    ["Field support", "..."]
  ]
}
```
