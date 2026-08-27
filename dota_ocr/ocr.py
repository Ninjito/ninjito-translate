"""Tesseract OCR wrapper for Dota 2 chat translation.

Dota 2 chat is bright text on a semi-transparent dark panel overlaid on
the game scene.  When the chat fades or the region captures terrain
instead, there's no text — just noisy game textures.

Preprocessing strategy:
  1. Upscale 2× for small text.
  2. Grayscale.
  3. High-pass threshold (keep only bright pixels — the chat text).
     This cleanly ignores dark terrain/background.
  4. Invert for Tesseract (dark text on white).
  5. Light morphological cleanup.

A "text presence" check rejects frames where the bright-pixel ratio
is too low (pure terrain) or too high (all-white / sun glare).

Requires:
- Tesseract installed: https://github.com/UB-Mannheim/tesseract/wiki
  (during install, check "Russian" under Additional language data)
- pip install pytesseract
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Tuple

import cv2
import numpy as np

from dota_ocr.postprocess import normalize_cyrillic

_CYRILLIC_LANGS = {"ru", "uk", "be", "bg", "kk", "mk", "sr"}

def _bundled_tesseract_paths() -> list[str]:
    """Look for Tesseract bundled next to the app (PyInstaller / portable)."""
    paths = []
    # PyInstaller onedir: EXE is in the same folder as Tesseract-OCR/
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        paths.append(os.path.join(exe_dir, "Tesseract-OCR", "tesseract.exe"))
    # Dev mode: project folder
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    paths.append(os.path.join(project_root, "Tesseract-OCR", "tesseract.exe"))
    return paths


# Common install paths on Windows (fallback if not bundled).
_TESSERACT_PATHS = _bundled_tesseract_paths() + [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


class OCRReader:
    def __init__(
        self,
        lang: str = "ru",
        min_confidence: float = 0.0,
        preprocess: bool = True,
        debug: bool = False,
        **_kwargs,
    ):
        self.lang = lang
        self.min_confidence = min_confidence
        self.preprocess_enabled = preprocess
        self.debug = debug
        self._normalize_cyrillic = lang.lower() in _CYRILLIC_LANGS

        import pytesseract
        self._pytesseract = pytesseract

        # Auto-detect tesseract binary.  Prefer bundled over system install.
        found = None
        for p in _TESSERACT_PATHS:
            try:
                if os.path.isfile(p):
                    found = p
                    break
            except Exception:
                pass
        if found:
            pytesseract.pytesseract.tesseract_cmd = found
            # Make sure tessdata is found (alongside the binary).
            tessdata = os.path.join(os.path.dirname(found), "tessdata")
            if os.path.isdir(tessdata):
                os.environ["TESSDATA_PREFIX"] = tessdata
        elif not shutil.which("tesseract"):
            pass  # pytesseract will raise below

        # Verify it works.
        try:
            ver = pytesseract.get_tesseract_version()
            print(f"[ocr] Tesseract {ver} found.", flush=True)
        except Exception as e:
            raise RuntimeError(
                "Tesseract not found. Install from:\n"
                "  https://github.com/UB-Mannheim/tesseract/wiki\n"
                "Make sure to check 'Russian' during install.\n"
                f"Error: {e}"
            ) from e

        # Build the lang string: try rus+eng, fall back to eng.
        self._tess_lang = self._detect_lang(lang)
        print(f"[ocr] Using language: {self._tess_lang}", flush=True)

    def _detect_lang(self, lang: str) -> str:
        """Map our config lang to a Tesseract lang string."""
        available = set()
        try:
            available = set(self._pytesseract.get_languages())
        except Exception:
            pass

        if self.debug:
            print(f"[ocr] Tesseract languages available: {available}", flush=True)

        mapping = {
            "ru": "rus", "russian": "rus", "eslav": "rus",
            "en": "eng", "english": "eng",
            "uk": "ukr", "de": "deu", "fr": "fra",
            "es": "spa", "pt": "por", "ja": "jpn",
            "ch": "chi_sim", "chinese": "chi_sim",
            "korean": "kor", "arabic": "ara",
        }
        primary = mapping.get(lang.lower(), lang)

        # Always include English for mixed chat (player names, tags).
        parts = []
        if primary in available:
            parts.append(primary)
        if "eng" in available and "eng" != primary:
            parts.append("eng")
        if not parts:
            parts = [primary]

        return "+".join(parts)

    @staticmethod
    def _has_text(gray: np.ndarray, bright_thresh: int = 170) -> bool:
        """Check if the image likely contains text vs. just terrain/noise.

        Dota chat text is bright (white/colored) on a dark semi-transparent
        panel. Pure terrain has very few very-bright pixels in a structured
        pattern. We check the ratio of bright pixels:
          - Too few (<0.5%): no text, just dark terrain
          - Too many (>40%): glare, white screen, or bad capture
          - Just right: likely has text lines
        """
        bright = (gray > bright_thresh).sum()
        total = gray.size
        ratio = bright / total
        return 0.005 < ratio < 0.40

    @staticmethod
    def _preprocess(img: np.ndarray) -> np.ndarray:
        """Preprocess Dota chat for Tesseract.

        Dota 2 chat = bright text on dark semi-transparent panel.
        Strategy:
          1. 2× upscale (helps Tesseract with small text).
          2. Grayscale.
          3. Global threshold at ~170 — keeps only bright text pixels,
             kills dark terrain/background completely.
          4. Invert → dark text on white (Tesseract's preferred format).
          5. Small morphological close to bridge thin gaps in letters.
        """
        h, w = img.shape[:2]
        img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Global threshold: keep only bright pixels (the chat text).
        # Dota chat text is typically 140-255 brightness; terrain is 40-130.
        # Lowered from 170 -> 140 so colored player names (blue Ninjito,
        # red enemy names) aren't filtered out.
        _, binary = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)

        # Invert: Tesseract expects dark text on light background.
        binary = cv2.bitwise_not(binary)

        # Close tiny gaps in letter strokes.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

        return binary

    @staticmethod
    def _is_meaningful(text: str) -> bool:
        """Accept anything with at least 1 character (no empty lines)."""
        return len(text.strip()) > 0

    def read(self, img: np.ndarray) -> List[Tuple[str, float]]:
        """Return [(text, confidence), ...] for each detected line."""
        if self.preprocess_enabled:
            # Check for text presence BEFORE preprocessing.
            gray_check = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
            if not self._has_text(gray_check):
                if self.debug:
                    print("[ocr-debug] No text detected in frame (too dark / "
                          "no bright text pixels). Chat might not be visible.",
                          flush=True)
                return []
            processed = self._preprocess(img)
        else:
            processed = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

        # PSM 4 = single column of text of variable sizes.
        # Better for Dota chat layout (icon | [Allies] | : message) than PSM 6.
        # -c preserve_interword_spaces=1 keeps the spacing info.
        #
        # OEM 1 = LSTM only. The default (3) loads the legacy engine's
        # data as well as the LSTM's, and rus.traineddata is 20 MB, so
        # that is most of the ~195ms of startup Tesseract pays on every
        # single capture. Dropping the half we never recognise with is
        # both faster and more accurate: over 25 randomized Cyrillic chat
        # frames, 249ms vs 335ms (-26%) at 0.951 vs 0.915 accuracy,
        # better on 22 frames and worse on 2.
        config = "--psm 4 --oem 1 -c preserve_interword_spaces=1"
        try:
            raw_text = self._pytesseract.image_to_string(
                processed, lang=self._tess_lang, config=config
            )
        except Exception as e:
            if self.debug:
                print(f"[ocr-debug] Tesseract error: {e}", flush=True)
            return []

        out: List[Tuple[str, float]] = []
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if not self._is_meaningful(line):
                continue
            if self._normalize_cyrillic:
                line = normalize_cyrillic(line)
            out.append((line, 1.0))
        return out
