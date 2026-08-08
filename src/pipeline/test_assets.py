from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_test_cover_png(path: str | Path, *, width: int = 900, height: int = 500) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            top = y / max(1, height - 1)
            left = x / max(1, width - 1)
            r = int(24 + 60 * left + 20 * top)
            g = int(82 + 70 * top)
            b = int(140 + 70 * (1 - left))

            # Simple soft highlight block, enough to identify this as a generated test cover.
            if 95 < x < 805 and 150 < y < 350:
                r = min(255, r + 22)
                g = min(255, g + 24)
                b = min(255, b + 28)

            # Minimal geometric marks, no text dependency.
            if (x - 220) ** 2 + (y - 250) ** 2 < 72**2:
                r, g, b = 92, 186, 255
            if 520 < x < 740 and 210 < y < 290:
                r, g, b = 255, 202, 93

            row.extend([r, g, b])
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    png = _png_signature()
    png += _chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw, level=6))
    png += _chunk(b"IEND", b"")
    output.write_bytes(png)
    return output


def write_article_illustration_png(
    path: str | Path,
    *,
    role: str,
    width: int = 900,
    height: int = 420,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    palette = _illustration_palette(role)
    pixels = bytearray(width * height * 3)
    for y in range(height):
        top = y / max(1, height - 1)
        for x in range(width):
            left = x / max(1, width - 1)
            base = _mix_color(palette["bg_left"], palette["bg_right"], left)
            glow = int(22 * (1 - abs(left - 0.68) * 1.8) * (1 - abs(top - 0.34) * 1.5))
            r = min(255, base[0] + max(0, glow))
            g = min(255, base[1] + max(0, glow))
            b = min(255, base[2] + max(0, glow))
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes((r, g, b))

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for yy in range(max(0, y0), min(height, y1)):
            for xx in range(max(0, x0), min(width, x1)):
                offset = (yy * width + xx) * 3
                pixels[offset : offset + 3] = bytes(color)

    def circle(cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        radius_sq = radius * radius
        for yy in range(max(0, cy - radius), min(height, cy + radius + 1)):
            for xx in range(max(0, cx - radius), min(width, cx + radius + 1)):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= radius_sq:
                    offset = (yy * width + xx) * 3
                    pixels[offset : offset + 3] = bytes(color)

    def line(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], thickness: int = 4) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            t = step / steps
            x = int(x0 + (x1 - x0) * t)
            y = int(y0 + (y1 - y0) * t)
            rect(x - thickness // 2, y - thickness // 2, x + thickness // 2 + 1, y + thickness // 2 + 1, color)

    panel = palette["panel"]
    accent = palette["accent"]
    accent_2 = palette["accent_2"]
    muted = palette["muted"]
    ink = palette["ink"]

    rect(78, 76, 394, 308, panel)
    rect(112, 112, 350, 130, muted)
    rect(112, 158, 310, 174, muted)
    rect(112, 202, 338, 218, muted)
    rect(112, 246, 278, 262, muted)
    circle(332, 246, 30, accent)
    circle(332, 246, 14, panel)

    circle(540, 150, 46, accent)
    circle(690, 236, 58, accent_2)
    circle(558, 300, 36, muted)
    line(540, 150, 690, 236, ink, thickness=5)
    line(690, 236, 558, 300, ink, thickness=5)
    line(540, 150, 558, 300, ink, thickness=5)
    circle(540, 150, 20, panel)
    circle(690, 236, 26, panel)
    circle(558, 300, 16, panel)

    rect(636, 92, 782, 126, panel)
    rect(660, 318, 812, 352, panel)
    rect(692, 150, 808, 168, muted)
    rect(724, 184, 836, 202, muted)

    raw = bytearray()
    row_bytes = width * 3
    for y in range(height):
        start = y * row_bytes
        raw.extend(b"\x00")
        raw.extend(pixels[start : start + row_bytes])

    png = _png_signature()
    png += _chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
    png += _chunk(b"IEND", b"")
    output.write_bytes(png)
    return output


def _illustration_palette(role: str) -> dict[str, tuple[int, int, int]]:
    if role == "secondary":
        return {
            "bg_left": (248, 250, 252),
            "bg_right": (239, 246, 255),
            "panel": (255, 255, 255),
            "accent": (20, 184, 166),
            "accent_2": (245, 158, 11),
            "muted": (203, 213, 225),
            "ink": (71, 85, 105),
        }
    if role.startswith("tertiary"):
        return {
            "bg_left": (255, 251, 247),
            "bg_right": (240, 253, 250),
            "panel": (255, 255, 255),
            "accent": (234, 88, 12),
            "accent_2": (20, 184, 166),
            "muted": (254, 215, 170),
            "ink": (100, 116, 139),
        }
    return {
        "bg_left": (247, 249, 252),
        "bg_right": (239, 246, 255),
        "panel": (255, 255, 255),
        "accent": (37, 99, 235),
        "accent_2": (14, 165, 233),
        "muted": (203, 213, 225),
        "ink": (71, 85, 105),
    }


def _mix_color(left: tuple[int, int, int], right: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    return tuple(int(left[index] + (right[index] - left[index]) * ratio) for index in range(3))


def _png_signature() -> bytes:
    return b"\x89PNG\r\n\x1a\n"


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    payload = chunk_type + data
    return struct.pack("!I", len(data)) + payload + struct.pack("!I", zlib.crc32(payload) & 0xFFFFFFFF)
