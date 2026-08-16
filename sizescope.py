#!/usr/bin/env python3
"""
SizeScope — a visual disk-usage explorer for Windows.

See the size of files and folders as graphics:
  * Treemap  — WinDirStat / GrandPerspective style rectangles
  * Sunburst — DaisyDisk style concentric rings

Pure Python 3 + tkinter. No third-party dependencies.
Run:       pythonw sizescope.py      (or double-click SizeScope.bat)
Self-test: python sizescope.py --selftest
"""

import heapq
import math
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, ttk

# High-DPI crispness on Windows (no-op elsewhere / if unavailable)
try:
    import ctypes

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

APP_NAME = "SizeScope"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# --------------------------------------------------------------------------
# Theme / colors
# --------------------------------------------------------------------------

BG = "#171a21"          # app background
PANEL = "#20242e"       # panels / toolbar
CANVAS_BG = "#12151b"   # visualization background
FG = "#e8eaee"          # main text
FG_DIM = "#9aa1ad"      # secondary text
ACCENT = "#4f8fde"      # highlight blue
HL_COLOR = "#ffd24d"    # "locate this file" highlight (yellow)
BUTTON_BG = "#2a3040"
SEPARATOR = "#2c313c"

CATEGORY_COLORS = {
    "video":     "#e2574c",
    "image":     "#4caf7d",
    "audio":     "#a77bd6",
    "documents": "#4f8fde",
    "archives":  "#e0a13f",
    "code":      "#38b2a3",
    "apps":      "#d95fa0",
    "other":     "#7e8590",
    "folder":    "#55607a",
}

CATEGORY_EXT = {
    "video": {"mp4", "mkv", "avi", "mov", "wmv", "m4v", "mpg", "mpeg", "webm", "flv", "ts"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "svg",
              "psd", "ico", "raw", "cr2", "nef", "avif"},
    "audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "mid", "aiff"},
    "documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf",
                  "csv", "epub", "odt", "ods", "xps"},
    "archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab", "zst"},
    "code": {"py", "js", "ts", "html", "htm", "css", "java", "c", "cpp", "h", "cs", "rb",
             "go", "rs", "php", "sh", "ipynb", "json", "xml", "yml", "yaml", "sql", "r"},
    "apps": {"exe", "msi", "dll", "appx", "msix", "bat", "cmd", "ps1", "jar", "apk"},
}

# Shades used for folder segments in the sunburst (inner ring -> outer)
FOLDER_SHADES = ["#6b7890", "#5f6b83", "#545f75", "#495468", "#3f495b", "#37404f"]


def category_of(node):
    if node.is_dir:
        return "folder"
    ext = os.path.splitext(node.name)[1].lower().lstrip(".")
    for cat, exts in CATEGORY_EXT.items():
        if ext in exts:
            return cat
    return "other"


def color_for(node):
    return CATEGORY_COLORS[category_of(node)]


def blend(c1, c2, t):
    """Blend two hex colors; t=0 -> c1, t=1 -> c2."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return "#%02x%02x%02x" % (round(r1 + (r2 - r1) * t),
                              round(g1 + (g2 - g1) * t),
                              round(b1 + (b2 - b1) * t))


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            if unit == "B":
                return "%d %s" % (n, unit)
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def human_count(n):
    return "{:,}".format(n)


def fmt_mtime(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _short(path, n):
    return path if len(path) <= n else "…" + path[-(n - 1):]


# --------------------------------------------------------------------------
# Data model + scanner
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("name", "path", "size", "is_dir", "children", "mtime")

    def __init__(self, name, path, is_dir, size=0, mtime=0.0):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.children = []   # list[Node], sorted by size desc after scan
        self.mtime = mtime


class _MergedNode(Node):
    """Synthetic node that groups many too-small-to-see siblings."""
    __slots__ = ("count",)

    def __init__(self, count, path, size):
        Node.__init__(self, "%d small item%s" % (count, "s" if count > 1 else ""),
                      path, False, size)
        self.count = count


class _StopScan(Exception):
    pass


class ScanStats:
    __slots__ = ("files", "dirs", "errors", "elapsed", "skipped_links")

    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.errors = 0
        self.elapsed = 0.0
        self.skipped_links = 0


def _sort_children(node):
    node.children.sort(key=lambda c: c.size, reverse=True)
    for c in node.children:
        if c.is_dir:
            _sort_children(c)


class Scanner(threading.Thread):
    """Walks a folder tree in a background thread, aggregating sizes."""

    PROGRESS_EVERY = 0.2  # seconds between progress messages

    def __init__(self, root_path, out_queue):
        super().__init__(daemon=True)
        self.root_path = root_path
        self.out = out_queue
        self.stop_event = threading.Event()
        self._last_ping = 0.0
        self._files_since_ping = 0

    def cancel(self):
        self.stop_event.set()

    # -- internals ----------------------------------------------------------

    def _ping(self, path, files, total):
        now = time.monotonic()
        if now - self._last_ping >= self.PROGRESS_EVERY:
            self._last_ping = now
            self.out.put(("progress", files, total, path))

    def _walk(self, path, name, depth):
        """Recursively scan; returns (node, files, dirs, errors, links, bytes)."""
        if self.stop_event.is_set():
            raise _StopScan()
        node = Node(name, path, True)
        files = dirs = errors = links = total = 0
        if depth > 64:  # extremely deep trees: stop recursing, count nothing
            return (node, 0, 0, 0, 0, 0)
        try:
            it = os.scandir(path)
        except OSError:
            return (node, 0, 0, 1, 0, 0)
        with it:
            for entry in it:
                if self.stop_event.is_set():
                    raise _StopScan()
                try:
                    if entry.is_symlink():
                        links += 1
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                        # junction / mount point -> skip to avoid cycles
                        links += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child, f, d, e, l, b = self._walk(
                            entry.path, entry.name, depth + 1)
                        node.children.append(child)
                        total += b
                        files += f
                        dirs += d + 1
                        errors += e
                        links += l
                        self._ping(entry.path, files, total)
                    else:
                        size = st.st_size
                        node.children.append(Node(entry.name, entry.path, False,
                                                  size, st.st_mtime))
                        total += size
                        files += 1
                        if files % 1000 == 0:
                            self._ping(entry.path, files, total)
                except OSError:
                    errors += 1
        node.size = total
        return (node, files, dirs, errors, links, total)

    # -- thread body ---------------------------------------------------------

    def run(self):
        t0 = time.monotonic()
        path = os.path.abspath(self.root_path)
        try:
            root, files, dirs, errors, links, total = self._walk(
                path, os.path.basename(path.rstrip("\\/")) or path, 0)
            root.path = path
            root.name = os.path.basename(path.rstrip("\\/")) or path
            root.size = total
            _sort_children(root)
            stats = ScanStats()
            stats.files, stats.dirs, stats.errors, stats.skipped_links = \
                files, dirs, errors, links
            stats.elapsed = time.monotonic() - t0
            self.out.put(("done", root, stats))
        except _StopScan:
            self.out.put(("cancelled",))
        except Exception as exc:  # pragma: no cover - safety net
            self.out.put(("error", str(exc)))


# --------------------------------------------------------------------------
# Squarified treemap layout (Bruls, Huizing & van Wijk)
# --------------------------------------------------------------------------

def squarify(sizes, x, y, w, h):
    """Lay out rectangles proportional to `sizes` inside (x, y, w, h).

    `sizes` must be positive and sum to <= w*h. Returns rects in the same
    order as `sizes`: a list of (rx, ry, rw, rh).
    """
    rects = []
    i, n = 0, len(sizes)
    cx, cy, cw, ch = float(x), float(y), float(w), float(h)
    while i < n and cw > 0.5 and ch > 0.5:
        short = min(cw, ch)
        row = [sizes[i]]
        i += 1
        s = row[0]
        row_worst = max(short * short * s / (s * s), s * s / (short * short * s))
        while i < n:
            cand = sizes[i]
            new_row = row + [cand]
            s = sum(new_row)
            mx, mn = max(new_row), min(new_row)
            cand_worst = max(short * short * mx / (s * s),
                             s * s / (short * short * mn))
            if cand_worst <= row_worst:
                row.append(cand)
                i += 1
                row_worst = cand_worst
            else:
                break

        area = sum(row)
        if cw >= ch:
            rw = area / ch
            if rw <= 0:
                break
            ry = cy
            for a in row:
                rh = a / rw
                rects.append((cx, ry, rw, rh))
                ry += rh
            cx += rw
            cw -= rw
        else:
            rh = area / cw
            if rh <= 0:
                break
            rx = cx
            for a in row:
                rw2 = a / rh
                rects.append((rx, cy, rw2, rh))
                rx += rw2
            cy += rh
            ch -= rh
    return rects


# --------------------------------------------------------------------------
# Tooltip
# --------------------------------------------------------------------------

class Tooltip:
    def __init__(self, app):
        self.app = app
        self.tip = None
        self.text = None

    def show(self, text, x, y):
        if self.tip is not None and self.text == text:
            return
        self.hide()
        self.text = text
        self.tip = tw = tk.Toplevel(self.app)
        tw.wm_overrideredirect(True)
        frame = tk.Frame(tw, background=SEPARATOR, bd=1)
        label = tk.Label(frame, text=text, justify="left", background="#2a2f3a",
                         foreground=FG, font=("Segoe UI", 9), padx=8, pady=5)
        label.pack(padx=1, pady=1)
        frame.pack()
        tw.update_idletasks()
        sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
        tw_w, tw_h = tw.winfo_width(), tw.winfo_height()
        if x + tw_w + 18 > sw:
            x = sw - tw_w - 8
        if y + tw_h + 18 > sh:
            y = sh - tw_h - 8
        tw.wm_geometry("+%d+%d" % (x + 12, y + 14))

    def hide(self):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        self.text = None


# --------------------------------------------------------------------------
# OS helpers
# --------------------------------------------------------------------------

def reveal_in_explorer(path):
    path = os.path.normpath(path)
    try:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])
    except Exception:
        try:
            os.startfile(os.path.dirname(path))  # noqa
        except Exception:
            pass


# --------------------------------------------------------------------------
# Treemap view
# --------------------------------------------------------------------------

class TreemapView(tk.Frame):
    MIN_RECT = 3      # px, below this children are not drawn
    LABEL_STRIP = 15  # px reserved at the top of a folder for its label

    def __init__(self, master, app):
        super().__init__(master, background=CANVAS_BG)
        self.app = app
        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.items = {}      # canvas id -> node
        self.by_path = {}    # node path -> canvas id
        self.hl = None       # hovered item id
        self.node = None     # node currently displayed (the "view root")
        self._hl_item = None  # "locate" highlight item id
        self._hl_tag = None   # extra label drawn for the located item
        self._labeled = set() # item ids that already show a text label

        self.font_name = tkfont.Font(family="Segoe UI", size=9)
        self.font_size = tkfont.Font(family="Segoe UI", size=8)
        self.font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<Leave>", self._on_leave)

    # -- rendering -----------------------------------------------------------

    def render(self):
        c = self.canvas
        c.delete("all")
        self.items.clear()
        self.by_path.clear()
        self._labeled.clear()
        self.hl = None
        self._hl_item = None
        self._hl_tag = None
        if self.node is None:
            return
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        pad = 2
        self._draw_children(self.node, (pad, pad, w - 2 * pad, h - 2 * pad))
        if not self.items:
            c.create_text(w // 2, h // 2, text="(empty folder)",
                          fill=FG_DIM, font=("Segoe UI", 11))
        self.apply_highlight()

    def _draw_children(self, node, rect):
        x, y, w, h = rect
        kids = [k for k in node.children if k.size > 0]
        if not kids or w < self.MIN_RECT or h < self.MIN_RECT or node.size <= 0:
            return
        area = w * h
        sizes = [k.size / node.size * area for k in kids]
        rects = squarify(sizes, x, y, w, h)
        c = self.canvas
        for kid, (rx, ry, rw, rh) in zip(kids, rects):
            if rw < 2 or rh < 2:
                continue
            iid = c.create_rectangle(rx, ry, rx + rw, ry + rh,
                                     fill=color_for(kid),
                                     outline=CANVAS_BG, width=1)
            self.items[iid] = kid
            self.by_path[kid.path] = iid
            c.tag_bind(iid, "<Enter>", self._make_enter(iid, kid))
            c.tag_bind(iid, "<Leave>", self._on_leave)
            c.tag_bind(iid, "<Button-1>", self._make_click(kid))
            c.tag_bind(iid, "<Double-Button-1>", self._make_double(kid))
            c.tag_bind(iid, "<Button-3>", self._make_right(kid))
            if kid.is_dir:
                if rh > 34 and rw > 40:
                    self._draw_folder_label(kid, iid, rx, ry, rw)
                    self._draw_children(kid, (rx + 1, ry + self.LABEL_STRIP + 1,
                                              rw - 2, rh - self.LABEL_STRIP - 2))
                else:
                    self._draw_children(kid, (rx + 1, ry + 1, rw - 2, rh - 2))
            else:
                self._draw_file_label(kid, iid, rx, ry, rw, rh)

    def _draw_folder_label(self, kid, iid, rx, ry, rw):
        if self.font_bold.measure(kid.name) <= rw - 6:
            self.canvas.create_text(rx + 3, ry + 1, text=kid.name, anchor="nw",
                                    fill="#ffffff", font=self.font_bold)
            self._labeled.add(iid)

    def _draw_file_label(self, kid, iid, x, y, w, h):
        if w < 30 or h < 16:
            return
        size_txt = human_size(kid.size)
        if self.font_name.measure(kid.name) <= w - 6:
            if h >= 30 and self.font_size.measure(size_txt) <= w - 6:
                c = self.canvas
                c.create_text(x + w / 2, y + h / 2 - 7, text=kid.name,
                              fill="#ffffff", font=self.font_name)
                c.create_text(x + w / 2, y + h / 2 + 7, text=size_txt,
                              fill="#e8eaee", font=self.font_size)
            else:
                self.canvas.create_text(x + w / 2, y + h / 2, text=kid.name,
                                        fill="#ffffff", font=self.font_name)
            self._labeled.add(iid)

    # -- interaction ----------------------------------------------------------

    def _make_enter(self, iid, kid):
        def handler(event):
            if self.hl != iid:
                self._clear_highlight()
                self.hl = iid
                if iid == self._hl_item:
                    # hovering the located file: emphasize, don't hide it
                    self.canvas.itemconfigure(iid, outline=HL_COLOR, width=4)
                else:
                    self.canvas.itemconfigure(iid, outline="#ffffff", width=2)
            self.app.show_details(kid)
            self.app.tooltip.show(self._tip_text(kid), event.x_root, event.y_root)
            return "break"
        return handler

    def _on_leave(self, event=None):
        self._clear_highlight()
        self.app.tooltip.hide()

    def _clear_highlight(self):
        if self.hl is not None:
            try:
                if self.hl == self._hl_item:
                    # don't erase the yellow "locate" highlight underneath
                    self.canvas.itemconfigure(self.hl, outline=HL_COLOR, width=3)
                else:
                    self.canvas.itemconfigure(self.hl, outline=CANVAS_BG, width=1)
            except tk.TclError:
                pass
        self.hl = None

    def apply_highlight(self):
        """Outline the located file's rectangle. Returns True if it is visible."""
        if self._hl_item is not None and self._hl_item in self.items:
            try:
                self.canvas.itemconfigure(self._hl_item, outline=CANVAS_BG, width=1)
            except tk.TclError:
                pass
        self._hl_item = None
        if self._hl_tag is not None:
            try:
                self.canvas.delete(self._hl_tag)
            except tk.TclError:
                pass
            self._hl_tag = None
        path = getattr(self.app, "highlight_path", None)
        if path is None:
            return False
        iid = self.by_path.get(path)
        if iid is None:
            return False
        self._hl_item = iid
        self.canvas.itemconfigure(iid, outline=HL_COLOR, width=3)
        self.canvas.tag_raise(iid)
        if iid not in self._labeled:
            # small rect without a label -> tag it so the user can find it
            node = self.items.get(iid)
            x0, y0, x1, y1 = self.canvas.coords(iid)
            label = node.name if len(node.name) <= 44 else node.name[:42] + "…"
            self._hl_tag = self.canvas.create_text(
                x0 + 2, y0 + 2, anchor="nw",
                text="%s (%s)" % (label, human_size(node.size)),
                fill=HL_COLOR, outline="#101317", width=2,
                font=("Segoe UI", 9, "bold"))
        return True

    def _item_clicked(self, kid):
        if isinstance(kid, _MergedNode):
            return
        if kid.is_dir:
            self.app.zoom(kid)   # single click on a folder -> analyse it
        else:
            self.app.select(kid)

    def _make_click(self, kid):
        def handler(event):
            self._item_clicked(kid)
            return "break"
        return handler

    def _make_double(self, kid):
        def handler(event):
            if kid.is_dir:
                self.app.zoom(kid)
            else:
                reveal_in_explorer(kid.path)
            return "break"
        return handler

    def _make_right(self, kid):
        def handler(event):
            self.app.select(kid)
            self.app.popup_menu(kid, event)
            return "break"
        return handler

    def _tip_text(self, kid):
        pct = ""
        if self.node and self.node.size:
            pct = "  (%.1f%%)" % (100.0 * kid.size / self.node.size)
        return "%s\n%s\n%s%s" % (kid.name, kid.path, human_size(kid.size), pct)

    def set_root(self, node):
        self.node = node
        self.render()


# --------------------------------------------------------------------------
# Sunburst view (DaisyDisk-style rings)
# --------------------------------------------------------------------------

class SunburstView(tk.Frame):
    MAX_RINGS = 5
    MIN_SPAN_DEG = 1.2      # don't draw segments narrower than this
    MIN_RECURSE_DEG = 2.8   # don't descend into smaller segments
    GAP_DEG = 0.35

    def __init__(self, master, app):
        super().__init__(master, background=CANVAS_BG)
        self.app = app
        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.items = {}      # canvas id -> (node, original fill)
        self.by_path = {}    # node path -> canvas id
        self.node = None
        self._geom = None    # (cx, cy, r0)
        self._hl_item = None
        self.font_label = tkfont.Font(family="Segoe UI", size=8)

        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<Double-Button-1>", self._on_double)

    # -- rendering -----------------------------------------------------------

    def render(self):
        c = self.canvas
        c.delete("all")
        self.items.clear()
        self.by_path.clear()
        self._hl_item = None
        node = self.node
        if node is None:
            self._geom = None
            return
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            self._geom = None
            return
        cx, cy = w / 2, h / 2
        R = min(w, h) / 2 - 6
        r0 = max(34.0, R * 0.18)
        self._geom = (cx, cy, r0)
        rings = min(self.MAX_RINGS, max(1, self._depth(node)))
        ring_w = (R - r0) / rings

        if node.size > 0:
            for kid, a0, a1 in self._segments(node, -90.0, 360.0):
                self._draw_segment(kid, cx, cy, a0, a1, r0, ring_w, 0, rings)
        if not self.items:
            c.create_text(cx, cy, text="(empty folder)",
                          fill=FG_DIM, font=("Segoe UI", 11))
        self.apply_highlight()

        # center hole: name + size of the current folder
        c.create_oval(cx - r0 + 2, cy - r0 + 2, cx + r0 - 2, cy + r0 - 2,
                      fill=CANVAS_BG, outline=FOLDER_SHADES[0])
        c.create_text(cx, cy - 8, text=node.name, fill=FG,
                      font=("Segoe UI", 11, "bold"),
                      width=max(40, int(r0 * 1.6)), justify="center")
        c.create_text(cx, cy + 12, text=human_size(node.size), fill=ACCENT,
                      font=("Segoe UI", 10))

    def _segments(self, node, a0, span):
        """Split `span` degrees among node's children proportional to size.

        Children too small to see are merged into one synthetic
        'N small items' segment so nothing is silently invisible.
        Returns a list of (node, seg_a0, seg_a1).
        """
        kids = [k for k in node.children if k.size > 0]
        if not kids or node.size <= 0 or span <= 0:
            return []
        out = []
        tail_count = 0
        tail_size = 0
        for k in kids:  # children are sorted by size, small ones come last
            s = span * k.size / node.size
            if s < self.MIN_SPAN_DEG:
                tail_count += 1
                tail_size += k.size
            else:
                out.append((k, s))
        if tail_count and tail_size:
            s = span * tail_size / node.size
            if s >= self.MIN_SPAN_DEG:
                out.append((_MergedNode(tail_count, node.path, tail_size), s))
        result = []
        cur = a0
        for k, s in out:
            result.append((k, cur, cur + s))
            cur += s
        return result

    def _depth(self, node, left=None):
        if left is None:
            left = self.MAX_RINGS
        if left <= 0 or not node.is_dir:
            return 0
        best = 0
        for k in node.children:
            if k.is_dir and best < left:
                best = max(best, 1 + self._depth(k, left - 1))
        return best

    def _draw_segment(self, kid, cx, cy, a0, a1, r_in, ring_w, depth, rings):
        span = a1 - a0
        if span < self.MIN_SPAN_DEG:
            return
        gap = self.GAP_DEG if span > 2.5 else 0.0
        aa0, aa1 = a0 + gap / 2, a1 - gap / 2
        r_out = r_in + ring_w
        fill = FOLDER_SHADES[min(depth, len(FOLDER_SHADES) - 1)] if kid.is_dir \
            else color_for(kid)
        pts = []
        steps = max(2, int(math.ceil((aa1 - aa0) / 2.0)))
        for i in range(steps + 1):
            t = math.radians(aa0 + (aa1 - aa0) * i / steps)
            pts.append(cx + r_out * math.cos(t))
            pts.append(cy + r_out * math.sin(t))
        for i in range(steps, -1, -1):
            t = math.radians(aa0 + (aa1 - aa0) * i / steps)
            pts.append(cx + r_in * math.cos(t))
            pts.append(cy + r_in * math.sin(t))
        c = self.canvas
        iid = c.create_polygon(pts, fill=fill, outline=CANVAS_BG, width=1)
        self.items[iid] = (kid, fill)
        self.by_path[kid.path] = iid
        c.tag_bind(iid, "<Enter>", self._make_enter(iid, kid))
        c.tag_bind(iid, "<Leave>", self._on_leave_seg)
        c.tag_bind(iid, "<Button-1>", self._make_click(kid))
        c.tag_bind(iid, "<Double-Button-1>", self._make_double(kid))
        c.tag_bind(iid, "<Button-3>", self._make_right(kid))
        # label big segments
        if span > 7 and ring_w > 26 and not isinstance(kid, _MergedNode):
            mid = math.radians(a0 + span / 2)
            lr = (r_in + r_out) / 2
            label = kid.name if len(kid.name) <= 22 else kid.name[:20] + "…"
            if self.font_label.measure(label) < (r_out - r_in) * 2.6:
                c.create_text(cx + lr * math.cos(mid), cy + lr * math.sin(mid),
                              text=label, fill="#f2f4f7", font=self.font_label)
        # recurse into this folder's children in the next ring
        if kid.is_dir and depth + 1 < rings and span >= self.MIN_RECURSE_DEG:
            for child, ca0, ca1 in self._segments(kid, a0, span):
                self._draw_segment(child, cx, cy, ca0, ca1,
                                   r_out, ring_w, depth + 1, rings)

    # -- interaction ----------------------------------------------------------

    def _make_enter(self, iid, kid):
        def handler(event):
            self.canvas.itemconfigure(iid, fill=ACCENT)
            self.app.show_details(kid)
            pct = ""
            if self.node and self.node.size:
                pct = "  (%.1f%%)" % (100.0 * kid.size / self.node.size)
            if isinstance(kid, _MergedNode):
                tip = "%d small items\n%s\n%s%s" % (
                    kid.count, kid.path, human_size(kid.size), pct)
            else:
                tip = "%s\n%s\n%s%s" % (
                    kid.name, kid.path, human_size(kid.size), pct)
            self.app.tooltip.show(tip, event.x_root, event.y_root)
            return "break"
        return handler

    def _on_leave_seg(self, event=None):
        for iid, (_kid, fill) in self.items.items():
            try:
                self.canvas.itemconfigure(iid, fill=fill)
            except tk.TclError:
                pass
        self.app.tooltip.hide()

    def apply_highlight(self):
        """Outline the located file's segment. Returns True if it is visible."""
        if self._hl_item is not None:
            try:
                self.canvas.itemconfigure(self._hl_item, outline=CANVAS_BG, width=1)
            except tk.TclError:
                pass
        self._hl_item = None
        path = getattr(self.app, "highlight_path", None)
        if path is None:
            return False
        iid = self.by_path.get(path)
        if iid is None:
            return False
        self._hl_item = iid
        self.canvas.itemconfigure(iid, outline=HL_COLOR, width=3)
        self.canvas.tag_raise(iid)
        return True

    def _item_clicked(self, kid):
        if isinstance(kid, _MergedNode):
            return
        if kid.is_dir:
            self.app.zoom(kid)   # single click on a folder -> analyse it
        else:
            self.app.select(kid)

    def _make_click(self, kid):
        def handler(event):
            self._item_clicked(kid)
            return "break"
        return handler

    def _make_double(self, kid):
        def handler(event):
            if kid.is_dir:
                self.app.zoom(kid)
            else:
                reveal_in_explorer(kid.path)
            return "break"
        return handler

    def _on_double(self, event):
        # double-click on the center hole -> go up one level
        if self._geom and self.app.view_root is not self.app.tree_root:
            cx, cy, r0 = self._geom
            if math.hypot(event.x - cx, event.y - cy) < r0:
                self.app.zoom_up()

    def _make_right(self, kid):
        def handler(event):
            self.app.select(kid)
            self.app.popup_menu(kid, event)
            return "break"
        return handler

    def set_root(self, node):
        self.node = node
        self.render()


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class App(tk.Tk):
    MIN_W, MIN_H = 980, 620

    def __init__(self):
        super().__init__()
        self.title(APP_NAME + " — Disk Usage Explorer")
        self.geometry("1200x740")
        self.minsize(self.MIN_W, self.MIN_H)
        self.configure(background=BG)
        self._set_icon()

        self.tooltip = Tooltip(self)
        self.tree_root = None   # scan result root Node
        self.view_root = None   # node the views currently display
        self.selected = None    # pinned selection
        self.highlight_path = None  # path of file to locate (from the list)
        self.history = []       # back stack
        self.scanner = None
        self.msg_queue = queue.Queue()
        self.view_mode = "treemap"

        self._build_style()
        self._build_ui()
        self._poll_queue()
        self._bind_keys()

    # -- icon -----------------------------------------------------------------

    def _set_icon(self):
        try:
            size = 32
            img = tk.PhotoImage(width=size, height=size)
            cx = cy = (size - 1) / 2.0
            hues = (CATEGORY_COLORS["video"], CATEGORY_COLORS["image"],
                    CATEGORY_COLORS["documents"], CATEGORY_COLORS["archives"])
            for y in range(size):
                x = 0
                while x < size:
                    dx, dy = x - cx, y - cy
                    r = math.hypot(dx, dy)
                    if r > 15.5 or r < 3.5:
                        x += 1
                        continue
                    # find a run of opaque pixels in this row
                    x0 = x
                    row = []
                    while x < size:
                        dx, dy = x - cx, y - cy
                        r = math.hypot(dx, dy)
                        if r > 15.5 or r < 3.5:
                            break
                        ang = (math.degrees(math.atan2(dy, dx)) + 112.5) % 360
                        base = hues[int(ang // 90)]
                        row.append(blend(base, "#ffffff", 0.13 * min(3, int((r - 3.5) / 3))))
                        x += 1
                    if row:
                        img.put("{" + " ".join(row) + "}", to=(x0, y, x, y + 1))
            self.iconphoto(True, img)
            self._icon_img = img  # keep reference alive
        except Exception:
            pass

    # -- ttk style --------------------------------------------------------------

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG, foreground=FG,
                        fieldbackground=PANEL, bordercolor=SEPARATOR,
                        lightcolor=PANEL, darkcolor=PANEL,
                        troughcolor=PANEL, font=("Segoe UI", 9))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
        style.configure("Treeview", background="#1a1e26", foreground=FG,
                        fieldbackground="#1a1e26", rowheight=22,
                        bordercolor=SEPARATOR, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL, foreground=FG_DIM,
                        relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#2c4a75")],
                  foreground=[("selected", "#ffffff")])
        style.configure("TProgressbar", background=ACCENT,
                        troughcolor="#2a2f3a", bordercolor=PANEL,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Vertical.TScrollbar", background=PANEL,
                        troughcolor=BG, bordercolor=BG, arrowcolor=FG_DIM)

    # -- widgets -------------------------------------------------------------------

    def _flat_button(self, parent, text, command, width=None):
        b = tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                      bg=BUTTON_BG, fg=FG, activebackground=ACCENT,
                      activeforeground="#ffffff", disabledforeground="#565d69",
                      font=("Segoe UI", 9, "bold"), padx=12, pady=4, cursor="hand2")
        if width:
            b.configure(width=width)
        return b

    def _build_ui(self):
        # ---------- toolbar ----------
        tb = tk.Frame(self, background=PANEL, padx=8, pady=6)
        tb.pack(fill="x", side="top")

        self.btn_scan = self._flat_button(tb, "📂 Scan folder…", self.choose_and_scan)
        self.btn_scan.pack(side="left")
        self.btn_drive = self._flat_button(tb, "💽 C:\\", lambda: self.scan("C:\\"))
        self.btn_drive.pack(side="left", padx=(6, 0))
        self.btn_home = self._flat_button(tb, "🏠 Home",
                                          lambda: self.scan(os.path.expanduser("~")))
        self.btn_home.pack(side="left", padx=(6, 0))
        self.btn_stop = self._flat_button(tb, "⏹ Stop", self.cancel_scan)
        self.btn_stop.pack(side="left", padx=(6, 0))

        self.btn_back = self._flat_button(tb, "←", self.go_back, width=3)
        self.btn_back.pack(side="left", padx=(14, 0))
        self.btn_up = self._flat_button(tb, "↑ Up", self.zoom_up)
        self.btn_up.pack(side="left", padx=(6, 0))
        self.btn_refresh = self._flat_button(tb, "↻", self.rescan, width=3)
        self.btn_refresh.pack(side="left", padx=(6, 0))

        # view toggle (right side)
        toggle = tk.Frame(tb, background="#242a36")
        toggle.pack(side="right")
        self.btn_vtree = tk.Button(toggle, text="▤ Treemap",
                                   command=lambda: self.set_view("treemap"),
                                   relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
                                   padx=12, pady=4, cursor="hand2")
        self.btn_vsun = tk.Button(toggle, text="◍ Sunburst",
                                  command=lambda: self.set_view("sunburst"),
                                  relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
                                  padx=12, pady=4, cursor="hand2")
        self.btn_vtree.pack(side="left")
        self.btn_vsun.pack(side="left")

        # ---------- breadcrumb ----------
        self.crumb_bar = tk.Frame(self, background=BG, padx=10, pady=4)
        self.crumb_bar.pack(fill="x", side="top")

        # ---------- main area ----------
        pane = tk.PanedWindow(self, orient="horizontal", sashwidth=6,
                              sashrelief="flat", background=BG, bd=0, sashpad=2)
        pane.pack(fill="both", expand=True, padx=8, pady=(2, 4))

        view_holder = tk.Frame(pane, background=CANVAS_BG)
        self.treemap = TreemapView(view_holder, self)
        self.sunburst = SunburstView(view_holder, self)
        self.treemap.pack(fill="both", expand=True)

        side = tk.Frame(pane, background=PANEL, width=310)
        side.pack_propagate(False)

        tk.Label(side, text="LARGEST FILES", background=PANEL, foreground=FG_DIM,
                 font=("Segoe UI", 8, "bold"), anchor="w"
                 ).pack(fill="x", padx=10, pady=(10, 2))
        self.top_files = ttk.Treeview(side, columns=("size", "name", "path"),
                                      show="headings", selectmode="browse")
        self.top_files.heading("size", text="Size")
        self.top_files.heading("name", text="Name")
        self.top_files.column("size", width=70, anchor="ne", stretch=False)
        self.top_files.column("name", width=160, anchor="w", stretch=True)
        self.top_files.column("path", width=0, stretch=False)  # hidden, for reveal
        vsb = ttk.Scrollbar(side, orient="vertical", command=self.top_files.yview)
        self.top_files.configure(yscrollcommand=vsb.set)
        self.top_files.bind("<<TreeviewSelect>>", self._on_topfile_select)
        self.top_files.bind("<Double-Button-1>", self._on_topfile_double)
        self.top_files.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=0)
        vsb.pack(side="left", fill="y", padx=(2, 0))

        # details + legend stacked under the file list
        bottom = tk.Frame(side, background=PANEL)
        bottom.pack(side="bottom", fill="x")
        tk.Label(bottom, text="DETAILS", background=PANEL, foreground=FG_DIM,
                 font=("Segoe UI", 8, "bold"), anchor="w"
                 ).pack(fill="x", padx=10, pady=(10, 2))
        self.details = tk.Text(bottom, height=9, background="#1a1e26", foreground=FG,
                               relief="flat", font=("Consolas", 9), wrap="word",
                               padx=8, pady=6, state="disabled")
        self.details.pack(fill="x", padx=8)
        self.details.tag_configure("k", foreground=FG_DIM)
        self.details.tag_configure("v", foreground="#ffffff")
        legend = tk.Frame(bottom, background=PANEL)
        legend.pack(fill="x", padx=10, pady=(6, 10))
        for i, (cat, col) in enumerate(CATEGORY_COLORS.items()):
            cell = tk.Frame(legend, background=PANEL)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 6), pady=1)
            tk.Frame(cell, width=10, height=10, background=col).pack(
                side="left", padx=(0, 4))
            tk.Label(cell, text=cat, background=PANEL, foreground=FG_DIM,
                     font=("Segoe UI", 8)).pack(side="left")

        pane.add(view_holder, stretch="always", minsize=420)
        pane.add(side, minsize=260, width=310)

        # ---------- status bar ----------
        sb = tk.Frame(self, background=PANEL, padx=10, pady=3)
        sb.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready — pick a folder to scan.")
        tk.Label(sb, textvariable=self.status_var, background=PANEL,
                 foreground=FG_DIM, font=("Segoe UI", 9), anchor="w"
                 ).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=140)
        self.progress.pack(side="right", padx=(8, 0))

        self._set_scanning(False)
        self._style_toggle()

    def _style_toggle(self):
        active = dict(bg=ACCENT, fg="#ffffff")
        inactive = dict(bg="#242a36", fg=FG_DIM)
        if self.view_mode == "treemap":
            self.btn_vtree.configure(**active)
            self.btn_vsun.configure(**inactive)
        else:
            self.btn_vtree.configure(**inactive)
            self.btn_vsun.configure(**active)

    # -- views / navigation ---------------------------------------------------

    def set_view(self, mode):
        if mode == self.view_mode:
            return
        self.view_mode = mode
        if mode == "treemap":
            self.sunburst.pack_forget()
            self.treemap.pack(fill="both", expand=True)
        else:
            self.treemap.pack_forget()
            self.sunburst.pack(fill="both", expand=True)
        self._style_toggle()
        self._apply_root()

    def choose_and_scan(self):
        path = filedialog.askdirectory(title="Choose a folder to scan")
        if path:
            self.scan(path)

    def scan(self, path):
        if not os.path.isdir(path):
            return
        if self.scanner is not None:
            self.scanner.cancel()
        self.tree_root = None
        self.view_root = None
        self.history.clear()
        self.selected = None
        self.highlight_path = None
        self.msg_queue = queue.Queue()
        self.scanner = Scanner(path, self.msg_queue)
        self._set_scanning(True)
        self._set_status("Scanning %s …" % path)
        self._clear_views()
        self._clear_top_files()
        self._clear_details()
        self.scanner.start()

    def rescan(self):
        if self.tree_root is not None and self.scanner is None:
            self.scan(self.tree_root.path)

    def cancel_scan(self):
        if self.scanner is not None:
            self.scanner.cancel()
            self._set_status("Cancelling…")

    def _set_scanning(self, on):
        state = "disabled" if on else "normal"
        for b in (self.btn_scan, self.btn_drive, self.btn_home):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if on else "disabled")
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, files, total, path = msg
                    self._set_status("Scanning…  %s files • %s   (%s)" % (
                        human_count(files), human_size(total), _short(path, 60)))
                elif kind == "done":
                    self._scan_finished(*msg[1:])
                elif kind == "cancelled":
                    self._set_scanning(False)
                    self._set_status("Scan cancelled.")
                elif kind == "error":
                    self._set_scanning(False)
                    self._set_status("Scan failed: %s" % msg[1])
        except queue.Empty:
            pass
        self.after(60, self._poll_queue)

    def _scan_finished(self, root, stats):
        self.scanner = None
        self._set_scanning(False)
        self.tree_root = root
        self.view_root = root
        self._apply_root()
        self._populate_top_files(root)
        self._set_status(
            "%s  •  %s files, %s folders  •  %d unreadable items  •  %.1fs" % (
                human_size(root.size), human_count(stats.files),
                human_count(stats.dirs), stats.errors, stats.elapsed))

    def _clear_views(self):
        self.treemap.set_root(None)
        self.sunburst.set_root(None)
        self._update_breadcrumb()

    def _apply_root(self):
        self.treemap.set_root(self.view_root)
        self.sunburst.set_root(self.view_root)
        self._update_breadcrumb()
        can_up = self._parent_of(self.view_root) is not None
        self.btn_up.configure(state="normal" if can_up else "disabled")
        self.btn_back.configure(state="normal" if self.history else "disabled")
        self.btn_refresh.configure(
            state="normal" if self.tree_root is not None else "disabled")

    def _parent_of(self, node):
        """Find the parent of `node` within the scanned tree (None at root)."""
        if node is None or self.tree_root is None or node is self.tree_root:
            return None
        root = self.tree_root
        try:
            rel = os.path.relpath(node.path, root.path)
        except ValueError:
            return None
        if rel == "." or rel.startswith(".."):
            return None
        parts = rel.split(os.sep)
        cur = root
        for part in parts[:-1]:
            nxt = None
            for c in cur.children:
                if c.is_dir and c.name == part:
                    nxt = c
                    break
            if nxt is None:
                return None
            cur = nxt
        return cur

    def zoom(self, node):
        if node is None or not node.is_dir:
            return
        if self.view_root is not None and self.view_root is not node:
            self.history.append(self.view_root)
            if len(self.history) > 200:
                self.history.pop(0)
        self.view_root = node
        self._apply_root()

    def zoom_up(self):
        parent = self._parent_of(self.view_root)
        if parent is not None:
            self.history.append(self.view_root)
            self.view_root = parent
            self._apply_root()

    def go_back(self):
        if self.history:
            self.view_root = self.history.pop()
            self._apply_root()

    # -- selection / details -----------------------------------------------------

    def select(self, node):
        # click pins the details and highlights a file; clicking again unpins
        if self.selected is node:
            self.selected = None
            if self.highlight_path == node.path:
                self.highlight_path = None
        elif isinstance(node, _MergedNode):
            self.selected = None
        else:
            self.selected = node
            if not node.is_dir:
                self.highlight_path = node.path
        self.show_details(node)
        self.treemap.apply_highlight()
        self.sunburst.apply_highlight()

    def show_details(self, node):
        if self.selected is not None and self.selected is not node:
            return  # a pinned selection wins over hover
        d = self.details
        d.configure(state="normal")
        d.delete("1.0", "end")
        base = self.view_root.size if self.view_root and self.view_root.size else 0
        pct = ("%.2f%%" % (100.0 * node.size / base)) if base else "—"
        rows = [
            ("Name", node.name),
            ("Type", ("Folder" if node.is_dir else "File (%s)" % category_of(node))
             if not isinstance(node, _MergedNode)
             else "Merged (%d items)" % node.count),
            ("Size", "%s  (%s B)" % (human_size(node.size), human_count(node.size))),
        ]
        if node.is_dir:
            rows.append(("Contains", "%s items" % human_count(len(node.children))))
        rows += [
            ("Share", pct + " of view"),
            ("Modified", fmt_mtime(node.mtime)),
            ("Path", node.path),
        ]
        pinned = "  📌" if self.selected is node else ""
        for i, (k, v) in enumerate(rows):
            if i:
                d.insert("end", "\n")
            d.insert("end", k.ljust(9), "k")
            d.insert("end", " " + v, "v")
        if pinned:
            d.insert("end", pinned, "v")
        d.configure(state="disabled")

    def _clear_details(self):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.configure(state="disabled")

    # -- top files ----------------------------------------------------------------

    def _populate_top_files(self, root, limit=25):
        def files_iter(node):
            stack = [node]
            while stack:
                n = stack.pop()
                for c in n.children:
                    if c.is_dir:
                        stack.append(c)
                    else:
                        yield c
        top = heapq.nlargest(limit, files_iter(root), key=lambda n: n.size)
        tv = self.top_files
        tv.delete(*tv.get_children())
        for f in top:
            tv.insert("", "end", values=(human_size(f.size), f.name, f.path))

    def _clear_top_files(self):
        self.top_files.delete(*self.top_files.get_children())

    def _on_topfile_double(self, event):
        sel = self.top_files.selection()
        if sel:
            reveal_in_explorer(self.top_files.item(sel[0], "values")[2])

    # -- locate a file from the Largest Files list ---------------------------------

    def find_node(self, path):
        """Return the Node for `path` within the scanned tree, or None."""
        root = self.tree_root
        if root is None or not path:
            return None
        try:
            rel = os.path.relpath(path, root.path)
        except ValueError:
            return None
        if rel == ".":
            return root
        if rel.startswith(".."):
            return None
        cur = root
        for part in rel.split(os.sep):
            cur = next((c for c in cur.children if c.name == part), None)
            if cur is None:
                return None
        return cur

    def locate_file(self, path):
        """Highlight `path` in the visualization; zoom to its folder if needed."""
        if self.tree_root is None or self.scanner is not None:
            return
        node = self.find_node(path)
        if node is None or node.is_dir:
            return
        self.selected = None           # the list pick wins over any pinned item
        self.highlight_path = node.path
        self.show_details(node)
        if self._visible_highlight():
            return
        # not visible in the current view -> zoom into the folder containing it
        parent = self.find_node(os.path.dirname(node.path))
        if parent is not None and parent is not self.view_root:
            self.zoom(parent)         # re-render re-applies the highlight
            if self._visible_highlight():
                return
        self.highlight_path = None
        self._set_status("“%s” is too small to display in this view" % node.name)

    def _visible_highlight(self):
        """True if the highlight is applied on a view the user can actually see
        (a hidden view's canvas may hold stale items)."""
        return any(v.winfo_ismapped() and v.apply_highlight()
                   for v in (self.treemap, self.sunburst))

    def _on_topfile_select(self, event):
        sel = self.top_files.selection()
        if sel:
            self.locate_file(self.top_files.item(sel[0], "values")[2])

    # -- breadcrumb -----------------------------------------------------------------

    def _update_breadcrumb(self):
        for w in self.crumb_bar.winfo_children():
            w.destroy()
        node = self.view_root
        if node is None:
            return
        chain = []
        parent = self._parent_of(node)
        while parent is not None:
            chain.append(parent)
            parent = self._parent_of(parent)
        chain.reverse()
        chain.append(self.view_root)
        if len(chain) > 4:
            chain = chain[-4:]
            tk.Label(self.crumb_bar, text="…", background=BG, foreground=FG_DIM,
                     font=("Segoe UI", 9)).pack(side="left")
        for i, n in enumerate(chain):
            is_cur = n is self.view_root
            if i:
                tk.Label(self.crumb_bar, text="›", background=BG, foreground=FG_DIM,
                         font=("Segoe UI", 9)).pack(side="left", padx=2)
            lbl = tk.Label(self.crumb_bar, text=n.name, background=BG,
                           foreground=FG if is_cur else ACCENT,
                           font=("Segoe UI", 9, "bold" if is_cur else "normal"),
                           cursor="hand2")
            if not is_cur:
                lbl.bind("<Button-1>", lambda e, nn=n: self.zoom(nn))
            lbl.pack(side="left")

    # -- context menu ----------------------------------------------------------------

    def popup_menu(self, node, event):
        m = tk.Menu(self, tearoff=0, bg="#242a36", fg=FG,
                    activebackground=ACCENT, activeforeground="#ffffff",
                    font=("Segoe UI", 9))
        if node.is_dir:
            m.add_command(label="Zoom in", command=lambda: self.zoom(node))
            if self._parent_of(node) is not None:
                m.add_command(label="Zoom out to parent", command=self.zoom_up)
            m.add_separator()
        m.add_command(label="Reveal in Explorer",
                      command=lambda: reveal_in_explorer(node.path))
        m.add_command(label="Copy path", command=lambda: self._copy(node.path))
        m.add_command(label="Copy size",
                      command=lambda: self._copy(human_size(node.size)))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

    # -- misc -------------------------------------------------------------------------

    def _set_status(self, text):
        self.status_var.set(text)

    def _bind_keys(self):
        self.bind("<BackSpace>", lambda e: self.zoom_up())
        self.bind("<Escape>", lambda e: self.cancel_scan())
        self.bind("<Left>", lambda e: self.go_back())

    # -- external hook for tests --------------------------------------------------------

    def load_tree(self, root):
        self.tree_root = root
        self.view_root = root
        self._set_scanning(False)
        self._apply_root()
        self._populate_top_files(root)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def run_selftest():
    import shutil
    import tempfile

    failures = []

    def check(name, cond, info=""):
        print(("PASS" if cond else "FAIL"), "-", name, ("" if cond else info))
        if not cond:
            failures.append(name)

    # ---- human_size ----
    check("human_size B", human_size(0) == "0 B")
    check("human_size KB", human_size(2048) == "2.0 KB")
    check("human_size MB", human_size(1024 * 1024) == "1.0 MB")
    check("human_size GB", human_size(3 * 1024 ** 3) == "3.0 GB")

    # ---- squarify ----
    sizes = [40, 25, 15, 10, 5, 3, 2]
    W = H = 100.0
    areas = [s / sum(sizes) * W * H for s in sizes]
    rects = squarify(areas, 0, 0, W, H)
    check("squarify count", len(rects) == len(sizes))
    covered = sum(r[2] * r[3] for r in rects)
    check("squarify coverage", abs(covered - W * H) < 1e-6, "covered=%s" % covered)
    check("squarify descending", all(
        rects[i][2] * rects[i][3] >= rects[i + 1][2] * rects[i + 1][3] - 1e-9
        for i in range(len(rects) - 1)))
    check("squarify contained", all(
        r[0] >= -1e-9 and r[1] >= -1e-9 and
        r[0] + r[2] <= W + 1e-6 and r[1] + r[3] <= H + 1e-6 for r in rects))
    aspect = max(max(r[2] / r[3], r[3] / r[2]) for r in rects)
    check("squarify aspect", aspect < 3.5, "max aspect %.2f" % aspect)
    r2 = squarify([1000.0], 0, 0, 50, 20)  # single item fills the whole box
    check("squarify single", abs(r2[0][2] - 50) < 1e-9 and abs(r2[0][3] - 20) < 1e-9)
    r3 = squarify([30.0, 30.0, 30.0, 10.0], 0, 0, 10, 9)
    check("squarify multi-row", abs(sum(x[2] * x[3] for x in r3) - 90) < 1e-6)
    r4 = squarify([10.0] * 50, 0, 0, 100, 100)
    check("squarify many equal", abs(sum(x[2] * x[3] for x in r4) - 500) < 1e-6)

    # ---- scanner on a temp tree ----
    tmp = tempfile.mkdtemp(prefix="sizescope_test_")
    try:
        os.makedirs(os.path.join(tmp, "a", "b"))
        os.makedirs(os.path.join(tmp, "empty"))
        with open(os.path.join(tmp, "a", "f1.bin"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(tmp, "a", "b", "f2.bin"), "wb") as f:
            f.write(b"y" * 3000)
        with open(os.path.join(tmp, "f3.txt"), "wb") as f:
            f.write(b"z" * 10)
        q = queue.Queue()
        Scanner(tmp, q).run()  # run synchronously in this thread
        kind, root, stats = None, None, None
        for _ in range(100):  # drain progress messages until the final one
            msg = q.get_nowait()
            if msg[0] == "done":
                kind, root, stats = msg
                break
        check("scan done msg", kind == "done")
        check("scan total size", root.size == 4010, "size=%s" % root.size)
        check("scan file count", stats.files == 3, "files=%s" % stats.files)
        check("scan dir count", stats.dirs == 3, "dirs=%s" % stats.dirs)
        a = next(c for c in root.children if c.name == "a")
        check("scan folder size", a.size == 4000, "a=%s" % a.size)
        check("scan sorted", root.children == sorted(
            root.children, key=lambda c: c.size, reverse=True))
        empty = next(c for c in root.children if c.name == "empty")
        check("scan empty dir kept", empty is not None and empty.size == 0)
        walk_total = 0
        for dirpath, _dirs, filenames in os.walk(tmp):
            for fn in filenames:
                walk_total += os.path.getsize(os.path.join(dirpath, fn))
        check("scan matches os.walk", walk_total == root.size)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- sunburst angle math (children spans must fill the parent span) ----
    spans = [360.0 * c / 1000 for c in (500, 300, 150, 50)]
    check("sunburst spans", abs(sum(spans) - 360.0) < 1e-9)

    # ---- GUI smoke test ----
    root = Node("testroot", os.path.join(os.path.expanduser("~"), "testroot"), True)
    for i, (name, size) in enumerate(
            (("Videos", 500), ("Photos", 300), ("Docs", 150), ("Misc", 50))):
        d = Node(name, root.path + os.sep + name, True, size)
        root.children.append(d)
        for j in range(4):
            d.children.append(Node(
                "file%d_%d.dat" % (i, j),
                d.path + os.sep + "file%d_%d.dat" % (i, j),
                False, size // 4))
    root.size = sum(c.size for c in root.children)
    _sort_children(root)

    app = App()
    # keep the smoke-test window off-screen so a real mouse pointer can't
    # hover items and make <Enter>-driven checks non-deterministic
    app.geometry("1200x740+4000+4000")
    app.load_tree(root)
    app.set_view("treemap")
    app.update()
    check("treemap rendered", len(app.treemap.items) > 0)
    app.zoom(root.children[0])
    app.update()
    check("treemap zoom", app.treemap.node is root.children[0])
    check("history push", app.history and app.history[-1] is root)
    app.go_back()
    app.update()
    check("treemap back", app.treemap.node is root)
    app.set_view("sunburst")
    app.update()
    check("sunburst rendered", len(app.sunburst.items) > 0)
    app.zoom(root.children[1])
    app.update()
    check("sunburst zoom", app.sunburst.node is root.children[1])
    app.zoom_up()
    app.update()
    check("sunburst zoom up", app.sunburst.node is root)
    app.select(root.children[0])
    app.update()
    check("details text", "Videos" in app.details.get("1.0", "end"))
    check("top files", len(app.top_files.get_children()) == 16)
    app.select(root.children[0])  # unpin
    app.show_details(root.children[2])
    app.update()
    check("unpin works", "Docs" in app.details.get("1.0", "end"))

    # ---- single-click zoom on folders ----
    app.set_view("treemap")          # section below clicks in the treemap
    app.update()
    app.selected = None
    app.treemap._item_clicked(root.children[0])       # click a folder -> zoom
    app.update()
    check("click folder zooms", app.view_root is root.children[0])
    app.go_back(); app.update()
    vid_file = root.children[0].children[0]
    app.treemap._item_clicked(vid_file)               # click a file -> pins
    app.update()
    check("click file pins", app.selected is vid_file)
    check("click file highlights", app.highlight_path == vid_file.path and
          app.treemap._hl_item is not None)
    app.treemap._item_clicked(vid_file)               # click again -> unpin
    app.update()
    check("click again unpins", app.selected is None and
          app.highlight_path is None and app.treemap._hl_item is None)
    app.selected = None

    # ---- locate from the Largest Files list ----
    # giant file dominates; nested tiny file is sub-pixel at root view
    big = Node("giant.iso", "C:/loc/giant.iso", False, 1_000_000_000)
    tinydir = Node("tinydir", "C:/loc/tinydir", True)
    tinydir.children.append(Node("needle.bin", "C:/loc/tinydir/needle.bin",
                                 False, 1000))
    tinydir.size = 1000
    loc_root = Node("loc", "C:/loc", True, big.size + 1000)
    loc_root.children = [big, tinydir]
    _sort_children(loc_root)

    app.load_tree(loc_root)
    app.set_view("treemap")
    app.update()
    check("needle not drawn at root", "needle.bin" not in app.treemap.by_path)
    needle_path = "C:/loc/tinydir/needle.bin"
    app.locate_file(needle_path)
    app.update()
    check("locate zooms to folder", app.view_root is tinydir)
    check("locate highlights", app.treemap._hl_item is not None and
          app.highlight_path == needle_path)
    outline = app.treemap.canvas.itemcget(app.treemap._hl_item, "outline")
    check("locate outline color", outline == HL_COLOR, outline)
    # highlight survives a re-render (resize)
    app.treemap.render(); app.update()
    check("highlight survives render", app.treemap._hl_item is not None)
    # sunburst highlight too
    app.set_view("sunburst"); app.update()
    check("sunburst locate", app.sunburst._hl_item is not None)
    # list row selection triggers locate
    app.set_view("treemap"); app.update()
    kids = app.top_files.get_children()
    app.top_files.selection_set(kids[0])   # giant.iso
    app.update()
    check("list select locates", app.highlight_path == "C:/loc/giant.iso" and
          app.view_root is loc_root and app.treemap._hl_item is not None)

    app.after(1200, app.destroy)
    app.mainloop()
    print("GUI smoke test window closed.")

    if failures:
        print("\n%d FAILURE(S): %s" % (len(failures), ", ".join(failures)))
        sys.exit(1)
    print("\nAll self-tests passed.")


def main():
    if "--selftest" in sys.argv:
        run_selftest()
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
