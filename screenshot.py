#!/usr/bin/env python3
"""Capture README screenshots of SizeScope (treemap + sunburst).

Renders the app with a demo tree, grabs the real window via the Windows API
(PrintWindow), and writes PNGs into docs/. Pure stdlib.

Run:  python screenshot.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sizescope as ss  # noqa: E402

TITLE = "SizeScope — Disk Usage Explorer"   # em dash, matches App.title()
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


# --------------------------------------------------------------------------
# Demo data — realistic names/categories so the colors pop
# --------------------------------------------------------------------------

def demo_tree():
    def f(name, size):
        return ss.Node(name, "C:\\Demo\\" + name.replace(" ", "_"), False, size)

    def folder(name, *kids):
        d = ss.Node(name, "C:\\Demo\\" + name.replace(" ", "_"), True,
                    sum(k.size for k in kids))
        d.children = list(kids)
        return d

    root = ss.Node("Demo", "C:\\Demo", True)
    root.children = [
        folder("Videos",
               f("holiday_2025.mkv", 3_950_000_000),
               f("gameplay.mp4", 1_420_000_000),
               f("wedding.mov", 890_000_000),
               f("drone_footage.mp4", 640_000_000)),
        folder("Games",
               f("SpaceRiders.exe", 2_800_000_000),
               f("assets.pak", 1_100_000_000),
               f("engine.dll", 420_000_000),
               f("dx12.dll", 190_000_000)),
        folder("Photos",
               f("IMG_4821.raw", 96_000_000),
               f("portrait.heic", 24_000_000),
               f("wallpaper.png", 12_000_000),
               f("banner.svg", 240_000),
               f("old_album.zip", 380_000_000)),
        folder("Music",
               f("discography.flac", 780_000_000),
               f("playlist.m4a", 96_000_000),
               f("podcast_042.mp3", 61_000_000)),
        folder("Documents",
               f("thesis.pdf", 48_000_000),
               f("budget_2026.xlsx", 3_100_000),
               f("contract.docx", 1_200_000),
               f("notes.md", 96_000)),
        folder("Projects",
               f("analysis.py", 148_000),
               f("app.js", 210_000),
               f("index.html", 34_000),
               f("model.pkl", 940_000_000),
               f("dataset.csv", 1_680_000_000)),
        folder("Downloads",
               f("installer.exe", 310_000_000),
               f("ubuntu-26.04.iso", 4_700_000_000),
               f("driver_pack.7z", 780_000_000),
               f("setup.msi", 95_000_000)),
        folder("Backups",
               f("laptop_full.7z", 6_400_000_000),
               f("photos_backup.zip", 1_900_000_000)),
        f("pagefile.sys", 1_600_000_000),
        f("hiberfil.sys", 850_000_000),
    ]
    root.size = sum(c.size for c in root.children)
    ss._sort_children(root)
    return root


# --------------------------------------------------------------------------
# Windows capture
# --------------------------------------------------------------------------

def find_window():
    user32 = ctypes.windll.user32
    for _ in range(50):                       # wait for the window to appear
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    raise RuntimeError("SizeScope window not found")


def capture_window(hwnd, out_png):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    try:
        # PW_RENDERFULLCONTENT = 2 (needed for layered/DWM content)
        if not user32.PrintWindow(hwnd, hdc_mem, 2):
            raise RuntimeError("PrintWindow failed")

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bmi = BMIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down 32bpp
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf,
                               ctypes.byref(bmi), 0):
            raise RuntimeError("GetDIBits failed")

        # BGRA top-down -> RGBA rows
        rows = []
        raw = buf.raw
        for y in range(h):
            row = []
            base = y * w * 4
            for x in range(w):
                i = base + x * 4
                row.append((raw[i + 2], raw[i + 1], raw[i], 255))
            rows.append(row)
        write_png(out_png, rows)
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def write_png(path, rows):
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) +
           chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print("wrote %s (%dx%d, %d KB)" % (path, w, h, len(png) // 1024))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    app = ss.App()
    app.geometry("1180x720+80+60")
    app.load_tree(demo_tree())
    app.set_view("treemap")
    app.update()
    time.sleep(0.6)                     # let Tk finish painting
    app.update()

    hwnd = find_window()
    capture_window(hwnd, os.path.join(OUT_DIR, "screenshot-treemap.png"))

    app.set_view("sunburst")
    app.update()
    time.sleep(0.6)
    app.update()
    capture_window(hwnd, os.path.join(OUT_DIR, "screenshot-sunburst.png"))

    app.destroy()
    print("done")


if __name__ == "__main__":
    main()
