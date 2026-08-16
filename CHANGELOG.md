# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
