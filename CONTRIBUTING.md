# Contributing to SizeScope

Thanks for your interest! This is a small, deliberately simple project —
one main file, no external dependencies. Please keep it that way. 🙂

## Getting started

```bash
git clone https://github.com/crxdnl-cell/sizescope.git
cd sizescope
python sizescope.py          # run the app (Python 3.8+ with tkinter)
```

## Before you submit a PR

1. **Run the test suite** — it must pass completely:

   ```bash
   python sizescope.py --selftest
   ```

   (42 checks: size formatting, squarified-treemap math, scanner totals vs
   `os.walk`, GUI smoke test with both views. The test window opens
   off-screen and closes itself.)

2. **Keep the zero-dependency rule** — the app (`sizescope.py`) must stay
   pure-stdlib (tkinter is fine). Build tooling (PyInstaller) lives in a venv,
   never in the app.

3. **Keep `sizescope.py` single-file** — new features should fit the existing
   structure (Scanner / views / App). If something genuinely needs a new
   module, open an issue first to discuss.

## Useful developer tools

| Script | What it does |
|---|---|
| `python make_icon.py` | Regenerates `sizescope.ico` (16/32/48 BMP + 256 PNG entries, pure stdlib) |
| `python screenshot.py` | Captures the running app into `docs/screenshot-*.png` for the README |
| `python sizescope.py --selftest` | Full test suite |

### Building the portable exe

```bash
python -m venv .venv-build
.venv-build/Scripts/pip install pyinstaller
.venv-build/Scripts/python.exe -m PyInstaller --onefile --windowed --name SizeScope ^
    --icon sizescope.ico --distpath dist --workpath build --specpath build sizescope.py
```

Attach `dist/SizeScope.exe` to a GitHub release — don't commit it.

## Guidelines

- Match the existing code style (plain classes, no type-checker ceremony,
  comments where the math is tricky).
- Bug fixes: add a check to `run_selftest()` that would have caught the bug.
- Features: open an issue first describing the use case.
- The app is **read-only by design** (reveal/copy only, no deleting) — PRs
  adding destructive file operations won't be accepted.
