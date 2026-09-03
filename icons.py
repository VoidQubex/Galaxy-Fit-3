"""
Icon rendering for the band.

The band asks for a notification's app icon by URL and expects raw pixels in
its own format: size x size, 4 bytes per pixel, [R][G][B][255-alpha].

Note the alpha byte is INVERTED versus the usual sense: 0 = opaque,
255 = fully transparent. Confirmed from the companion app's own conversion
routine, not guessed.

Requires: pip install Pillow
"""

import colorsys

from PIL import Image, ImageDraw, ImageFont


def color_for(key: str):
    """Stable, pleasant colour derived from a package/app name."""
    h = (hash(key) & 0xFFFF) / 0xFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.62, 0.88)
    return int(r * 255), int(g * 255), int(b * 255)


def _font(px: int):
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def render_image(size: int, label: str, key: str = None) -> Image.Image:
    """A rounded tile with the app's initial - a stand-in for a real app icon."""
    key = key or label
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(2, size // 5)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius,
                        fill=color_for(key) + (255,))

    letter = (label.strip()[:1] or "?").upper()
    f = _font(max(8, int(size * 0.58)))
    try:
        box = d.textbbox((0, 0), letter, font=f)
        w, h = box[2] - box[0], box[3] - box[1]
        pos = ((size - w) / 2 - box[0], (size - h) / 2 - box[1])
    except Exception:  # noqa: BLE001 - default font lacks textbbox on some builds
        pos = (size * 0.3, size * 0.2)
    d.text(pos, letter, font=f, fill=(255, 255, 255, 255))
    return img


def to_band_pixels(img: Image.Image, size: int) -> bytes:
    """PIL image -> the band's [R][G][B][255-alpha] raster."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    out = bytearray(size * size * 4)
    px = img.load()
    i = 0
    for y in range(size):
        for x in range(size):
            r, g, b, a = px[x, y]
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            out[i + 3] = 255 - a        # inverted alpha - see module docstring
            i += 4
    return bytes(out)


def load_frames(path: str, size: int = 112) -> list:
    """Frames from a GIF/APNG, or from a directory of images (sorted naturally).

    Returned pre-scaled and letterboxed to size x size on black, which is what
    the band wants and what silhouette content (hi, Bad Apple) looks best as.
    """
    import re
    from pathlib import Path

    p = Path(path)
    out = []

    def prep(im):
        im = im.convert("RGBA")
        # Fit inside the square, keeping aspect, on an opaque black bed.
        im.thumbnail((size, size), Image.LANCZOS)
        bed = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        bed.paste(im, ((size - im.size[0]) // 2, (size - im.size[1]) // 2), im)
        return bed

    if p.is_dir():
        def natural(f):
            return [int(t) if t.isdigit() else t.lower()
                    for t in re.split(r"(\d+)", f.name)]
        files = sorted((f for f in p.iterdir()
                        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp")),
                       key=natural)
        for f in files:
            with Image.open(f) as im:
                out.append(prep(im))
        return out

    with Image.open(p) as im:
        try:
            while True:
                out.append(prep(im))
                im.seek(im.tell() + 1)
        except EOFError:
            pass
    return out


def render_frame(size: int, frame: int):
    """An animation frame: a rotating hue wheel with the frame number on it.

    Deliberately unmistakable - if the band's icon changes, you'll see both the
    colour and the digit change.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    hue = (frame * 0.13) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
    d.ellipse([0, 0, size - 1, size - 1], fill=(int(r * 255), int(g * 255), int(b * 255), 255))

    # A wedge that sweeps round, so motion is obvious even at a glance.
    ang = (frame * 40) % 360
    d.pieslice([0, 0, size - 1, size - 1], ang, ang + 45, fill=(255, 255, 255, 255))

    txt = str(frame)
    f = _font(max(10, int(size * 0.5)))
    try:
        box = d.textbbox((0, 0), txt, font=f)
        pos = ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1])
    except Exception:  # noqa: BLE001
        pos = (size * 0.35, size * 0.25)
    d.text(pos, txt, font=f, fill=(0, 0, 0, 255))
    return img


def render_icon(size: int, label: str, key: str = None) -> bytes:
    """Convenience: rendered icon straight to the band's pixel format."""
    return to_band_pixels(render_image(size, label, key), size)
