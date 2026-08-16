#!/usr/bin/env python3
"""Generate sizescope.ico (16/32/48 BMP + 256 PNG entries) for the exe.

Pure stdlib. Sunburst design matching the app's runtime icon.
Run:  python make_icon.py
"""
import math
import struct
import zlib

HUES = ("#e2574c", "#4caf7d", "#4f8fde", "#e0a13f")  # video/image/docs/archives


def blend(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def hex_rgb(h):
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


HUES_RGB = tuple(hex_rgb(h) for h in HUES)
WHITE = (255, 255, 255)


def render(size, ss=4):
    """Render RGBA rows (top-down) with ss x ss supersampling."""
    c = (size - 1) / 2.0
    outer = size / 2.0 - max(1.0, size / 32.0)   # outer edge (1px-ish margin)
    hole = size * 0.16                            # center hole radius
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            r_sum = g_sum = b_sum = a_sum = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss - 0.5
                    py = y + (sy + 0.5) / ss - 0.5
                    dx, dy = px - c, py - c
                    r = math.hypot(dx, dy)
                    if r > outer or r < hole:
                        continue
                    ang = (math.degrees(math.atan2(dy, dx)) + 112.5) % 360
                    base = HUES_RGB[int(ang // 90)]
                    band = min(2.0, (r - hole) / (outer - hole) * 3.0)
                    col = blend(base, WHITE, 0.13 * band)
                    r_sum += col[0]
                    g_sum += col[1]
                    b_sum += col[2]
                    a_sum += 1.0
            n = ss * ss
            if a_sum == 0:
                row.append((0, 0, 0, 0))
            else:
                # average color over the covered subsamples; alpha = coverage
                row.append((min(255, round(r_sum / a_sum)),
                            min(255, round(g_sum / a_sum)),
                            min(255, round(b_sum / a_sum)),
                            min(255, round(a_sum / n * 255))))
        rows.append(row)
    return rows


def bmp_entry(rows):
    """32-bit BMP entry (bottom-up BGRA + AND mask)."""
    size = len(rows)
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32,
                      0, 0, 0, 0, 0, 0)
    xor = bytearray()
    and_mask = bytearray()
    for y in range(size - 1, -1, -1):          # bottom-up
        for (r, g, b, a) in rows[y]:
            xor += bytes((b, g, r, a))
    for y in range(size - 1, -1, -1):          # AND mask, rows padded to 4B
        row_bits = bytearray((size + 7) // 8)
        for x, (r, g, b, a) in enumerate(rows[y]):
            if a == 0:
                row_bits[x // 8] |= 0x80 >> (x % 8)
        and_mask += bytes(row_bits) + b"\x00" * (((size + 7) // 8 + 3) // 4 * 4
                                                 - (size + 7) // 8)
    return hdr + bytes(xor) + bytes(and_mask)


def png_entry(rows):
    """Minimal PNG encoder (RGBA, non-interlaced)."""
    h = len(rows)
    w = len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes((r, g, b, a)) for (r, g, b, a) in row)
                   for row in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) +
            chunk(b"IEND", b""))


def main():
    entries = []
    for size, kind in ((16, "bmp"), (32, "bmp"), (48, "bmp"), (256, "png")):
        rows = render(size)
        data = png_entry(rows) if kind == "png" else bmp_entry(rows)
        entries.append((size, data))
        print("rendered %dpx %s entry (%d bytes)" % (size, kind, len(data)))

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(entries))     # ICONDIR
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _size, data in entries:
        out += data

    with open("sizescope.ico", "wb") as f:
        f.write(out)
    print("wrote sizescope.ico (%d bytes)" % len(out))


if __name__ == "__main__":
    main()
