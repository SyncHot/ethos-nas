#!/usr/bin/env python3
"""Generate the PWA icons for EthOS (TODO.md P3-14).

No Pillow in the venv, so this writes PNGs by hand: a rounded square with the
theme gradient and a simple glyph, exported at the sizes a manifest / iOS home
screen wants. Two sets share the generator:

* the desktop shell (`icon-*.png`, `apple-touch-icon.png`) — NAS monitor;
* the Sticky Notes PWA (`notes-*.png`) — note sheet with a green check.

Run after changing the design:

    .venv/bin/python scripts/make_icons.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "frontend" / "static" / "icons"

# Theme colours (index.html: --accent / --surface-0 in dark mode).
TOP = (37, 99, 235)     # blue-600
BOTTOM = (15, 23, 42)   # slate-900
GLYPH = (219, 234, 254)  # blue-100

# Sticky Notes palette (matches frontend/manifest-notes.webmanifest).
NOTE_TOP = (245, 158, 11)     # amber-500
NOTE_BOTTOM = (180, 83, 9)    # amber-700
NOTE_PAPER = (255, 247, 237)  # orange-50
NOTE_FOLD = (254, 215, 170)   # orange-200
NOTE_LINE = (146, 64, 14)     # amber-800
NOTE_CHECK = (22, 163, 74)    # green-600

# Videos PWA palette (matches frontend/manifest-videos.webmanifest).
VIDEO_TOP = (27, 34, 51)      # slate-900-ish cinema
VIDEO_BOTTOM = (11, 13, 18)   # near-black
VIDEO_PLAY = (242, 178, 62)   # gold
VIDEO_RING = (242, 178, 62)

# Music PWA palette (matches frontend/manifest-music.webmanifest).
MUSIC_TOP = (20, 61, 46)      # deep green
MUSIC_BOTTOM = (8, 13, 11)    # near-black
MUSIC_NOTE = (29, 185, 84)    # the player's green accent


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def _rounded(x: float, y: float, size: int, radius: float) -> bool:
    """Inside a rounded square of `size` with corner radius `radius`."""
    if radius <= 0:
        return 0 <= x < size and 0 <= y < size
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def render(size: int, *, rounded_ratio: float = 0.22, padding_ratio: float = 0.0) -> bytes:
    """Return RGBA pixel rows for one icon."""
    radius = size * rounded_ratio
    inset = size * padding_ratio
    inner = size - 2 * inset
    rows = bytearray()
    # Glyph metrics: a monitor body with a stand, centred.
    gw, gh = inner * 0.56, inner * 0.38
    gx0 = inset + (inner - gw) / 2
    gy0 = inset + inner * 0.22
    stand_w, stand_h = inner * 0.06, inner * 0.14
    base_w, base_h = inner * 0.30, inner * 0.055

    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            if not _rounded(x + 0.5, y + 0.5, size, radius):
                rows.extend((0, 0, 0, 0))
                continue
            t = y / max(1, size - 1)
            r, g, b = _blend(TOP, BOTTOM, t)

            px, py = x + 0.5, y + 0.5
            in_body = gx0 <= px < gx0 + gw and gy0 <= py < gy0 + gh
            in_screen = (gx0 + gw * 0.06 <= px < gx0 + gw * 0.94
                         and gy0 + gh * 0.12 <= py < gy0 + gh * 0.88)
            in_stand = (abs(px - (gx0 + gw / 2)) < stand_w / 2
                        and gy0 + gh <= py < gy0 + gh + stand_h)
            in_base = (abs(px - (gx0 + gw / 2)) < base_w / 2
                       and gy0 + gh + stand_h <= py < gy0 + gh + stand_h + base_h)
            if in_body:
                r, g, b = GLYPH if in_screen else _blend(GLYPH, BOTTOM, 0.35)
            elif in_stand or in_base:
                r, g, b = GLYPH
            rows.extend((r, g, b, 255))
    return bytes(rows)


def _rounded_rect(x: float, y: float, x0: float, y0: float,
                  x1: float, y1: float, radius: float) -> bool:
    """Inside an axis-aligned rounded rectangle."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _segment_distance(px: float, py: float, x1: float, y1: float,
                      x2: float, y2: float) -> float:
    """Shortest distance from a point to a line segment."""
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    if length <= 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length))
    return ((px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2) ** 0.5


def render_note(size: int, *, rounded_ratio: float = 0.22, padding_ratio: float = 0.0) -> bytes:
    """Return RGBA pixel rows for the Sticky Notes icon."""
    radius = size * rounded_ratio
    inset = size * padding_ratio
    inner = size - 2 * inset
    paper_x0 = inset + inner * 0.24
    paper_x1 = inset + inner * 0.76
    paper_y0 = inset + inner * 0.15
    paper_y1 = inset + inner * 0.85
    paper_w = paper_x1 - paper_x0
    paper_h = paper_y1 - paper_y0
    paper_radius = inner * 0.035
    fold = inner * 0.16
    line_h = inner * 0.052
    line_x0 = paper_x0 + paper_w * 0.16
    line_x1 = paper_x1 - paper_w * 0.16
    line1_y = paper_y0 + paper_h * 0.27
    line2_y = paper_y0 + paper_h * 0.40
    check_w = inner * 0.075
    check_a = (paper_x0 + paper_w * 0.22, paper_y0 + paper_h * 0.70)
    check_b = (paper_x0 + paper_w * 0.42, paper_y0 + paper_h * 0.84)
    check_c = (paper_x0 + paper_w * 0.80, paper_y0 + paper_h * 0.58)
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if not _rounded(px, py, size, radius):
                rows.extend((0, 0, 0, 0))
                continue
            t = y / max(1, size - 1)
            r, g, b = _blend(NOTE_TOP, NOTE_BOTTOM, t)
            if _rounded_rect(px, py, paper_x0, paper_y0, paper_x1, paper_y1, paper_radius):
                r, g, b = NOTE_PAPER
                # Folded top-right corner.
                if px >= paper_x1 - fold and py <= paper_y0 + fold:
                    if (px - (paper_x1 - fold)) + (py - paper_y0) <= fold:
                        r, g, b = NOTE_FOLD
                # Two ruled lines.
                if line_x0 <= px <= line_x1 and (
                        line1_y <= py < line1_y + line_h
                        or line2_y <= py < line2_y + line_h):
                    r, g, b = NOTE_LINE
                # Green check mark.
                if (_segment_distance(px, py, *check_a, *check_b) <= check_w / 2
                        or _segment_distance(px, py, *check_b, *check_c) <= check_w / 2):
                    r, g, b = NOTE_CHECK
            rows.extend((r, g, b, 255))
    return bytes(rows)


def _in_triangle(px: float, py: float,
                 a: tuple[float, float], b: tuple[float, float],
                 c: tuple[float, float]) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1 = sign((px, py), a, b)
    d2 = sign((px, py), b, c)
    d3 = sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def render_video(size: int, *, rounded_ratio: float = 0.22,
                 padding_ratio: float = 0.0) -> bytes:
    """Return RGBA pixel rows for the Videos PWA icon (gold play on cinema dark)."""
    radius = size * rounded_ratio
    inset = size * padding_ratio
    inner = size - 2 * inset
    ax, ay = inset + inner * 0.36, inset + inner * 0.26
    bx, by = inset + inner * 0.36, inset + inner * 0.74
    cx, cy = inset + inner * 0.74, inset + inner * 0.50
    ring_r = inner * 0.40
    ring_w = inner * 0.055
    centre = size / 2
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if not _rounded(px, py, size, radius):
                rows.extend((0, 0, 0, 0))
                continue
            t = y / max(1, size - 1)
            r, g, b = _blend(VIDEO_TOP, VIDEO_BOTTOM, t)
            dist = ((px - centre) ** 2 + (py - centre) ** 2) ** 0.5
            if abs(dist - ring_r) <= ring_w / 2:
                r, g, b = VIDEO_RING
            if _in_triangle(px, py, (ax, ay), (bx, by), (cx, cy)):
                r, g, b = VIDEO_PLAY
            rows.extend((r, g, b, 255))
    return bytes(rows)


def render_music(size: int, *, rounded_ratio: float = 0.22,
                 padding_ratio: float = 0.0) -> bytes:
    """Return RGBA pixel rows for the Music PWA icon (green eighth notes)."""
    radius = size * rounded_ratio
    inset = size * padding_ratio
    inner = size - 2 * inset
    head_r = inner * 0.078
    stem_w = inner * 0.032
    beam_w = inner * 0.085
    h1 = (inset + inner * 0.35, inset + inner * 0.74)
    h2 = (inset + inner * 0.66, inset + inner * 0.66)
    top1 = inset + inner * 0.27
    top2 = inset + inner * 0.19
    x1 = h1[0] + head_r * 0.72
    x2 = h2[0] + head_r * 0.72
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if not _rounded(px, py, size, radius):
                rows.extend((0, 0, 0, 0))
                continue
            t = y / max(1, size - 1)
            r, g, b = _blend(MUSIC_TOP, MUSIC_BOTTOM, t)
            in_head = (((px - h1[0]) ** 2 + (py - h1[1]) ** 2 <= head_r ** 2)
                       or ((px - h2[0]) ** 2 + (py - h2[1]) ** 2 <= head_r ** 2))
            in_stem1 = abs(px - x1) <= stem_w / 2 and top1 <= py <= h1[1]
            in_stem2 = abs(px - x2) <= stem_w / 2 and top2 <= py <= h2[1]
            in_beam = (_segment_distance(px, py, x1, top1, x2, top2) <= beam_w / 2)
            if in_head or in_stem1 or in_stem2 or in_beam:
                r, g, b = MUSIC_NOTE
            rows.extend((r, g, b, 255))
    return bytes(rows)


def write_png(path: Path, size: int, pixels: bytes) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(pixels, 9))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"  {path.name}: {size}×{size}, {len(png):,} bytes")


def main() -> None:
    print("Writing PWA icons to", OUT)
    for size in (192, 512):
        write_png(OUT / f"icon-{size}.png", size, render(size))
    # iOS home screen: no transparency, corners are added by the OS.
    write_png(OUT / "apple-touch-icon.png", 180,
              render(180, rounded_ratio=0.0))
    # Maskable: keep the glyph inside the 80 % safe zone.
    write_png(OUT / "icon-maskable-512.png", 512,
              render(512, rounded_ratio=0.0, padding_ratio=0.10))

    print("Writing Sticky Notes PWA icons to", OUT)
    for size in (192, 512):
        write_png(OUT / f"notes-{size}.png", size, render_note(size))
    # iOS home screen: no transparency, corners are added by the OS.
    write_png(OUT / "notes-apple-touch-icon.png", 180,
              render_note(180, rounded_ratio=0.0))
    # Maskable: keep the note inside the 80 % safe zone.
    write_png(OUT / "notes-maskable-512.png", 512,
              render_note(512, rounded_ratio=0.0, padding_ratio=0.10))

    print("Writing Videos PWA icons to", OUT)
    for size in (192, 512):
        write_png(OUT / f"videos-{size}.png", size, render_video(size))
    # iOS home screen: no transparency, corners are added by the OS.
    write_png(OUT / "videos-apple-touch-icon.png", 180,
              render_video(180, rounded_ratio=0.0))
    # Maskable: keep the glyph inside the 80 % safe zone.
    write_png(OUT / "videos-maskable-512.png", 512,
              render_video(512, rounded_ratio=0.0, padding_ratio=0.10))

    print("Writing Music PWA icons to", OUT)
    for size in (192, 512):
        write_png(OUT / f"music-{size}.png", size, render_music(size))
    # iOS home screen: no transparency, corners are added by the OS.
    write_png(OUT / "music-apple-touch-icon.png", 180,
              render_music(180, rounded_ratio=0.0))
    # Maskable: keep the note inside the 80 % safe zone.
    write_png(OUT / "music-maskable-512.png", 512,
              render_music(512, rounded_ratio=0.0, padding_ratio=0.10))


if __name__ == "__main__":
    main()
