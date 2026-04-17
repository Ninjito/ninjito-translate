"""Build a portable distribution of Dota 2 Translate.

Produces `dist/Dota2Translate/` which contains:
    - Dota2Translate.exe            (the app, no Python needed)
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
APP_NAME = "Dota2Translate"

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


def clean() -> None:
    for p in (DIST, BUILD):
        if p.exists():
            print(f"  cleaning {p}")
            shutil.rmtree(p, ignore_errors=True)
    spec = ROOT / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()


def run_pyinstaller() -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--noconsole",          # no black console window
        "--onedir",             # folder, not single-file (faster startup)
        "--clean",
        "--noconfirm",
        # Hidden imports that PyInstaller might miss
        "--hidden-import", "pytesseract",
        "--hidden-import", "deep_translator",
        "--hidden-import", "PIL",
        "--hidden-import", "cv2",
        "--hidden-import", "mss",
        # Entry point
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
        "Dota 2 Russian -> English Chat Translator\n"
        "==========================================\n\n"
        "HOW TO USE:\n"
        "  1. Launch Dota 2 and enter any game (demo / lobby / real match).\n"
        "  2. Double-click Dota2Translate.exe.\n"
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
    print(f"[build] They just extract and run Dota2Translate.exe.")


if __name__ == "__main__":
    main()
