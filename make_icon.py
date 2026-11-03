"""Build the Windows icons from ``ui/logo.png``.

Writes:

* ``ui/app.ico``   - the multi-size icon the running app uses (window/taskbar)
* ``app.ico``      - the same icon at the project root, for PyInstaller
* ``ui/app-icon-preview.png`` - a 256 px preview for quick eyeballing

The logo is the app's rounded square mark on a transparent canvas.  The icon
is the mark trimmed to its own pixels, re-centred on a square, and exported at
every size Windows asks for (16 px title bar up to 256 px Explorer).
"""

from __future__ import annotations

import os

from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(ROOT, "ui", "logo.png")
ICON = os.path.join(ROOT, "ui", "app.ico")
ROOT_ICON = os.path.join(ROOT, "app.ico")
PREVIEW = os.path.join(ROOT, "ui", "app-icon-preview.png")

SIZES = [16, 24, 32, 48, 64, 128, 256]


def build_icon() -> Image.Image:
    """Return the 256 px master icon (logo trimmed, centred, transparent)."""
    source = Image.open(LOGO).convert("RGBA")

    # Trim the transparent padding around the mark, then re-centre it on a
    # square canvas with a small breathing margin (Windows draws the icon
    # edge to edge on the taskbar).
    bbox = source.getbbox()
    mark = source.crop(bbox) if bbox else source
    side = max(mark.size)
    margin = int(side * 0.06)
    canvas_side = side + margin * 2
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(
        mark,
        ((canvas_side - mark.width) // 2, (canvas_side - mark.height) // 2),
    )
    return canvas.resize((256, 256), Image.LANCZOS)


def main() -> None:
    master = build_icon()
    master.save(PREVIEW)

    frames = [master.resize((size, size), Image.LANCZOS) for size in SIZES]
    for path in (ICON, ROOT_ICON):
        frames[-1].save(
            path,
            format="ICO",
            sizes=[(size, size) for size in SIZES],
            append_images=frames[:-1],
        )

    print("wrote", ICON, os.path.getsize(ICON), "bytes")
    print("wrote", ROOT_ICON, os.path.getsize(ROOT_ICON), "bytes")
    print("preview", PREVIEW)


if __name__ == "__main__":
    main()
