# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-08-16

### Added
- High-DPI support up to 300% scaling (4K screens): all pixel-based sizes
  (window, list rows, paddings, panes) now scale with the display DPI, treemap
  folder-label strips and file labels derive their geometry from actual font
  metrics, and the sunburst center hole is sized to fit its text — nothing is
  cropped or overlapping at any scaling.
- The **Largest Files list** and the **Details panel** are now separate panes
  with a draggable sash, independently resizable; the whole side column still
  resizes horizontally via the main sash.
- Details content is anchored at the top of its pane.
- shields.io badges on the README (total & latest-release downloads, version,
  platform, license).
- 28 new self-test checks simulating 100/150/200/300% display scaling
  (70 checks total).

## [1.0.0] — 2026-08-16

First public release.

### Added
- Background folder scanning with live progress, stop support (`Esc`), and
  graceful handling of unreadable system folders.
- **Treemap view** — squarified treemap (Bruls/Huizing/van Wijk), color-coded
  by file category, labels on large items, click-to-zoom.
- **Sunburst view** — up to 5 concentric rings; too-small segments merged into
  a visible "N small items" wedge; click-to-zoom.
- Largest-files top-25 list with locate & yellow highlight (auto-zooms to the
  containing folder) and double-click reveal in Explorer.
- Hover tooltips, pinnable details panel, breadcrumb + back/up navigation,
  category legend, dark theme, runtime-drawn app icon.
- Right-click menu: reveal in Explorer, copy path, copy size (no destructive
  actions by design).
- `--selftest` mode: 42 automated checks (layout math, scanner vs `os.walk`,
  GUI smoke test).
- Portable single-file `SizeScope.exe` (PyInstaller) with embedded Python and
  generated multi-size icon; `make_icon.py` and `screenshot.py` developer tools.
- `THIRD_PARTY_NOTICES.md` — verbatim licenses for everything bundled in the
  portable exe (CPython PSF-2, Tcl/Tk, PyInstaller bootloader exception,
  VC++ runtime), making the exe a self-contained, properly attributed
  distribution.

[1.0.0]: https://github.com/crxdnl-cell/sizescope/releases/tag/v1.0.0
