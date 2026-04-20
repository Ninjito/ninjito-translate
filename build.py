"""Build a portable distribution of Dota 2 Translate.

Produces `dist/NinjitoTranslate/` which contains:
    - NinjitoTranslate.exe          (the app, no Python needed)
    - Tesseract-OCR/                (bundled OCR engine)
    - config.json                   (editable settings)
    - _internal/                    (Python libs — leave alone)

Zip the whole folder and send it to friends.  They extract & run the EXE.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "NinjitoTranslate"
APP_TITLE = "Ninjito Translate"
SPEC_CANDIDATES = [
    ROOT / f"{APP_NAME}.spec",
    ROOT / "Dota2Translate.spec",
]

# Paths where Tesseract might live on the build machine.
TESS_CANDIDATES = [
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    ROOT / "Tesseract-OCR",
]


def find_tesseract_dir() -> Path:
    for p in TESS_CANDIDATES:
        if (p / "tesseract.exe").is_file():
            return p
    raise SystemExit(
        "Tesseract-OCR install not found. Install it first or drop a "
        "Tesseract-OCR/ folder next to this script."
    )


def _kill_running_app() -> None:
    """Kill any running NinjitoTranslate.exe so we can overwrite its files."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{APP_NAME}.exe", "/T"],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def clean() -> None:
    _kill_running_app()
    for p in (DIST, BUILD):
        if p.exists():
            print(f"  cleaning {p}")
            # Retry a couple of times: Windows sometimes keeps file handles
            # around for a moment after the process exits.
            import time
            for attempt in range(5):
                try:
                    shutil.rmtree(p)
                    break
                except PermissionError:
                    time.sleep(0.6)
                    _kill_running_app()
            else:
                shutil.rmtree(p, ignore_errors=True)
    # IMPORTANT: do NOT delete the .spec — it holds our icon + bundled
    # assets (gg.png / gg.ico) config.  We keep it and build from it.


def run_pyinstaller() -> None:
    spec = next((p for p in SPEC_CANDIDATES if p.exists()), None)
    if spec is not None and spec.exists():
        # Build from spec so icon + datas (gg.png / gg.ico) are honored.
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--clean", "--noconfirm",
            str(spec),
        ]
    else:
        # First-ever build: generate a spec inline.  Next run will reuse it.
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--name", APP_NAME,
            "--noconsole",
            "--onedir",
            "--clean", "--noconfirm",
            "--icon", str(ROOT / "gg.ico"),
            "--add-data", f"{ROOT / 'gg.png'}{os.pathsep}.",
            "--add-data", f"{ROOT / 'gg.ico'}{os.pathsep}.",
            "--hidden-import", "pytesseract",
            "--hidden-import", "deep_translator",
            "--hidden-import", "PIL",
            "--hidden-import", "cv2",
            "--hidden-import", "mss",
            str(ROOT / "main.py"),
        ]
    print("[build] Running PyInstaller...")
    subprocess.run(cmd, check=True, cwd=ROOT)


def copy_tesseract() -> None:
    tess_src = find_tesseract_dir()
    tess_dst = DIST / APP_NAME / "Tesseract-OCR"
    print(f"[build] Copying Tesseract from {tess_src} -> {tess_dst}")
    if tess_dst.exists():
        shutil.rmtree(tess_dst)
    shutil.copytree(tess_src, tess_dst)
    # Ensure Russian language data is present.
    rus = tess_dst / "tessdata" / "rus.traineddata"
    if not rus.is_file():
        print(
            "\n[build] WARNING: Russian language data not found at\n"
            f"    {rus}\n"
            "The app will not translate Russian!  Download rus.traineddata from\n"
            "    https://github.com/tesseract-ocr/tessdata/raw/main/rus.traineddata\n"
            "and place it in the tessdata/ folder."
        )
    else:
        print(f"[build] OK: Russian language data present ({rus.stat().st_size//1024} KB)")


def copy_config() -> None:
    src = ROOT / "config.json"
    dst = DIST / APP_NAME / "config.json"
    if src.exists():
        shutil.copy2(src, dst)
        print(f"[build] Copied config.json -> {dst}")
    else:
        print("[build] WARNING: config.json not found — friends will need to calibrate.")


def write_readme() -> None:
    readme = DIST / APP_NAME / "README.txt"
    readme.write_text(
        f"{APP_TITLE}\n"
        "==================\n\n"
        "Dota 2 Russian -> English Chat Translator\n\n"
        "HOW TO USE:\n"
        "  1. Launch Dota 2 and enter any game (demo / lobby / real match).\n"
        f"  2. Double-click {APP_NAME}.exe.\n"
        "  3. Click the 'Resize' button to draw a box around the chat area.\n"
        "  4. Press F8 (or the Translate button) whenever you want to\n"
        "     translate the Russian chat currently visible on screen.\n\n"
        "CONTROLS:\n"
        "  F8                     - Translate chat right now\n"
        "  Left-drag on overlay   - Move the window\n"
        "  Shift + mouse wheel    - Adjust transparency\n"
        "  ESC (overlay focused)  - Close the app\n\n"
        "TROUBLESHOOTING:\n"
        "  * 'Dota 2 not found'  - Make sure Dota 2 is running.\n"
        "  * Garbled translations - Click Resize and re-select the chat area.\n"
        "  * Antivirus warning    - False positive from PyInstaller. You can\n"
        "                           whitelist the EXE.\n",
        encoding="utf-8",
    )
    print(f"[build] Wrote {readme}")


def main() -> None:
    os.chdir(ROOT)
    clean()
    run_pyinstaller()
    copy_tesseract()
    copy_config()
    write_readme()
    final = DIST / APP_NAME
    print(f"\n[build] DONE.  Portable app at: {final}")
    print(f"[build] Zip that folder and send it to your friends.")
    print(f"[build] They just extract and run {APP_NAME}.exe.")


if __name__ == "__main__":
    main()
