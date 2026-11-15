#!/usr/bin/env python3
"""Generate the PWA icons for EthOS (TODO.md P3-14).

No Pillow in the venv, so this writes PNGs by hand: a rounded square with the
theme gradient and a simple "NAS monitor" glyph, exported at the sizes a
manifest / iOS home screen wants. Run after changing the design:

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


if __name__ == "__main__":
    main()
