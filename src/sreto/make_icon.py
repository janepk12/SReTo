"""
make_icon.py — normalise any image into the square PNG a desktop icon needs.

    python -m sreto.make_icon                       # default icon -> assets/app_icon.png
    python -m sreto.make_icon photo.jpg             # use your own image
    python -m sreto.make_icon photo.jpg out.png     # explicit destination

Any aspect ratio is accepted: the image is centre-cropped to a square, which is
what a thumbnail wants, rather than squashed.

With no source it draws one from the project palette — a stylised bistatic
geometry: a direct ray (C1 blue, rx1/RE) and a reflected ray (C2 orange,
rx2/GR) meeting at a ground plane. Same colours as the figures, so the Dock
icon belongs to the same instrument as the plots.
"""

import os
import sys

ICON_SIZE = 1024


def _require_pillow():
    try:
        from PIL import Image, ImageDraw  # noqa: F401
        return True
    except ImportError:
        sys.stderr.write(
            "Pillow is required to build an icon.\n"
            "  conda install -n sdrr pillow      (or: pip install pillow)\n")
        return False


def square_crop(image, size=ICON_SIZE):
    """Centre-crop to a square, then resize. Never distorts the aspect ratio."""
    from PIL import Image

    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    image = image.crop((left, top, left + side, top + side))
    return image.resize((size, size), Image.LANCZOS)


def default_icon(size=ICON_SIZE):
    """A generated icon in the pipeline's own palette."""
    from PIL import Image, ImageDraw

    from . import theme

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Rounded background plate.
    radius = int(size * 0.22)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius,
                           fill="#101418")

    cx = size / 2
    ground_y = size * 0.74
    sat = (size * 0.80, size * 0.20)
    rx = (size * 0.26, ground_y - size * 0.12)
    spec = (cx + size * 0.04, ground_y)

    # Ground plane.
    draw.rectangle([size * 0.08, ground_y, size * 0.92, ground_y + size * 0.012],
                   fill=theme.MUTED)

    # Direct ray (rx1 = RE) and reflected ray (rx2 = GR).
    width = max(4, int(size * 0.022))
    draw.line([sat, rx], fill=theme.C1, width=width)
    draw.line([sat, spec], fill=theme.C2, width=width)
    draw.line([spec, rx], fill=theme.C2, width=width)

    # Transmitter.
    r = size * 0.055
    draw.ellipse([sat[0] - r, sat[1] - r, sat[0] + r, sat[1] + r],
                 fill="#ffffff")

    # Receiver mast with two elements, blue over orange.
    mast_w = max(3, int(size * 0.014))
    draw.rectangle([rx[0] - mast_w / 2, rx[1], rx[0] + mast_w / 2, ground_y],
                   fill=theme.MUTED)
    er = size * 0.045
    draw.ellipse([rx[0] - er, rx[1] - er, rx[0] + er, rx[1] + er], fill=theme.C1)
    draw.ellipse([rx[0] - er * 0.8, rx[1] + er * 1.1,
                  rx[0] + er * 0.8, rx[1] + er * 2.7], fill=theme.C2)

    # Specular point.
    sr = size * 0.03
    draw.ellipse([spec[0] - sr, spec[1] - sr, spec[0] + sr, spec[1] + sr],
                 fill=theme.C_EXTRACT)
    return image


def build(source=None, destination=None):
    """Write the icon PNG. Returns its path."""
    if not _require_pillow():
        return None
    from PIL import Image

    from . import branding

    destination = destination or branding.APP_ICON_PNG
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    if source:
        if not os.path.isfile(source):
            sys.stderr.write(f"no such image: {source}\n")
            return None
        image = Image.open(source).convert("RGBA")
        image = square_crop(image)
    else:
        image = default_icon()

    image.save(destination, "PNG")
    return destination


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    source = argv[0] if argv else None
    destination = argv[1] if len(argv) > 1 else None
    path = build(source, destination)
    if not path:
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
