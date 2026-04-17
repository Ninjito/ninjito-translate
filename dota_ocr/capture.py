"""Chat region capture.

Two strategies:

* **printwindow** (default, Dota-only): pull the full Dota client area
  via `PrintWindow` — this works even when VS Code / a browser / the
  translate overlay itself is on top of Dota. It only fails when Dota
  is running in exclusive fullscreen.

* **screen** (mss): grab desktop pixels. Fast, but captures whatever
  is visually on screen at that coord (including the wrong window).

`grab()` starts in printwindow mode and auto-falls-back to screen if
PrintWindow produces nothing for several consecutive frames (e.g. the
user is in exclusive fullscreen).
"""

from __future__ import annotations

from typing import Optional

import mss
import numpy as np

from dota_ocr.window import find_dota_hwnd, get_client_screen_rect, is_foreground
from dota_ocr.winshot import grab_window

_MODE_PRINTWINDOW = "printwindow"
_MODE_SCREEN = "screen"


class RegionCapture:
    def __init__(
        self,
        bbox: Optional[dict] = None,
        relative_bbox: Optional[dict] = None,
        require_foreground: bool = False,
        capture_mode: str = _MODE_PRINTWINDOW,
    ):
        if not bbox and not relative_bbox:
            raise ValueError("Provide bbox or relative_bbox")
        self._absolute_bbox = bbox
        self._relative_bbox = relative_bbox
        self._require_foreground = require_foreground
        self._mode = capture_mode if capture_mode in (_MODE_PRINTWINDOW, _MODE_SCREEN) else _MODE_PRINTWINDOW
        self._sct = mss.mss()
        self._cached_hwnd: Optional[int] = None
        self._printwindow_failures = 0

    # ---------------- hwnd helpers ----------------
    def _resolve_hwnd(self) -> Optional[int]:
        if self._cached_hwnd is not None:
            if get_client_screen_rect(self._cached_hwnd) is not None:
                return self._cached_hwnd
            self._cached_hwnd = None
        self._cached_hwnd = find_dota_hwnd()
        return self._cached_hwnd

    # ---------------- absolute-bbox path (legacy) ----------------
    def _grab_absolute(self) -> Optional[np.ndarray]:
        raw = np.array(self._sct.grab(self._absolute_bbox))
        return raw[:, :, :3].copy()

    # ---------------- Dota-relative paths ----------------
    def _grab_relative_printwindow(self, hwnd: int) -> Optional[np.ndarray]:
        img = grab_window(hwnd)
        if img is None:
            return None
        # Sanity: PrintWindow on fullscreen DX sometimes returns an all-
        # black bitmap. Detect that so we can fall back.
        if img.mean() < 1.0:
            return None
        return self._crop_relative(img)

    def _grab_relative_screen(self, hwnd: int) -> Optional[np.ndarray]:
        client = get_client_screen_rect(hwnd)
        if client is None:
            return None
        cx, cy, cw, ch = client
        rb = self._relative_bbox or {}
        left = cx + int(rb.get("left", 0))
        top = cy + int(rb.get("top", 0))
        width = int(rb.get("width", 0))
        height = int(rb.get("height", 0))
        right = min(left + width, cx + cw)
        bottom = min(top + height, cy + ch)
        left = max(left, cx)
        top = max(top, cy)
        width = right - left
        height = bottom - top
        if width <= 5 or height <= 5:
            return None
        bbox = {"left": left, "top": top, "width": width, "height": height}
        raw = np.array(self._sct.grab(bbox))
        return raw[:, :, :3].copy()

    def _crop_relative(self, client_img: np.ndarray) -> Optional[np.ndarray]:
        rb = self._relative_bbox or {}
        h_img, w_img = client_img.shape[:2]
        left = max(0, int(rb.get("left", 0)))
        top = max(0, int(rb.get("top", 0)))
        width = int(rb.get("width", 0))
        height = int(rb.get("height", 0))
        right = min(left + width, w_img)
        bottom = min(top + height, h_img)
        if right - left <= 5 or bottom - top <= 5:
            return None
        return client_img[top:bottom, left:right].copy()

    # ---------------- public ----------------
    def grab(self) -> Optional[np.ndarray]:
        if self._relative_bbox is None:
            return self._grab_absolute()

        hwnd = self._resolve_hwnd()
        if hwnd is None:
            return None
        if self._require_foreground and not is_foreground(hwnd):
            return None

        if self._mode == _MODE_PRINTWINDOW:
            img = self._grab_relative_printwindow(hwnd)
            if img is not None:
                self._printwindow_failures = 0
                return img
            self._printwindow_failures += 1
            # After ~5 consecutive failures we assume exclusive fullscreen
            # or a driver that blocks PrintWindow and switch to screen
            # capture permanently for this session.
            if self._printwindow_failures >= 5:
                print("[capture] PrintWindow failed repeatedly (likely exclusive "
                      "fullscreen). Falling back to screen capture for this session.",
                      flush=True)
                self._mode = _MODE_SCREEN

        return self._grab_relative_screen(hwnd)
