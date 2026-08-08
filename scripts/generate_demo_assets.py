from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "assets" / "covers" / "weekly-tech-briefing"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ACCENTS = (
    (234, 84, 20),
    (20, 184, 166),
    (37, 99, 235),
    (219, 39, 119),
    (124, 58, 237),
    (5, 150, 105),
    (202, 138, 4),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate original deterministic cover assets for the synthetic demo.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, weekday in enumerate(WEEKDAYS):
        _write_cover(output_dir / f"tech-briefing-{weekday}.png", weekday_index=index, accent=ACCENTS[index])
    print(f"Generated {len(WEEKDAYS)} covers in {output_dir}")
    return 0


def _write_cover(path: Path, *, weekday_index: int, accent: tuple[int, int, int]) -> None:
    width, height = 900, 383
    pixels = bytearray(width * height * 3)

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)

    for y in range(height):
        for x in range(width):
            shade = int(248 - 12 * (x / width) - 5 * (y / height))
            set_pixel(x, y, (shade, min(252, shade + 2), min(255, shade + 5)))

    for y in range(0, height):
        for x in range(0, 22):
            set_pixel(x, y, accent)

    for row in range(7):
        x0 = 90 + row * 46
        color = accent if row <= weekday_index else (203, 213, 225)
        for y in range(76, 98):
            for x in range(x0, x0 + 30):
                set_pixel(x, y, color)

    for y in range(146, 169):
        for x in range(90, 540):
            set_pixel(x, y, (31, 41, 55))
    for y in range(192, 210):
        for x in range(90, 438):
            set_pixel(x, y, (71, 85, 105))
    for y in range(234, 249):
        for x in range(90, 365):
            set_pixel(x, y, (148, 163, 184))

    cx, cy = 710, 190
    for y in range(cy - 92, cy + 93):
        for x in range(cx - 92, cx + 93):
            distance = (x - cx) ** 2 + (y - cy) ** 2
            if distance <= 92**2:
                set_pixel(x, y, accent)
            if distance <= 54**2:
                set_pixel(x, y, (255, 255, 255))
    for y in range(cy - 8, cy + 9):
        for x in range(cx - 44, cx + 45):
            set_pixel(x, y, (31, 41, 55))

    raw = bytearray()
    row_bytes = width * 3
    for y in range(height):
        start = y * row_bytes
        raw.extend(b"\x00")
        raw.extend(pixels[start : start + row_bytes])
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    payload = chunk_type + data
    return struct.pack("!I", len(data)) + payload + struct.pack("!I", zlib.crc32(payload) & 0xFFFFFFFF)


if __name__ == "__main__":
    raise SystemExit(main())
