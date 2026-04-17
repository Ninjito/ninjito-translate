"""Locate the Dota 2 window and compute its client-area screen rect.

We store chat region coordinates *relative to Dota's client area* (not
relative to the desktop). That way, moving or resizing the window, or
toggling windowed-borderless, doesn't break calibration — as long as the
chat stays in the same spot relative to the Dota UI.

Pure ctypes so we don't need pywin32.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional, Tuple

# Common window titles. Dota 2's main window is literally "Dota 2".
DOTA_TITLES = ("Dota 2",)


if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.ClientToScreen.restype = ctypes.c_bool
    _user32.GetForegroundWindow.restype = wintypes.HWND

    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = ctypes.c_bool


def find_dota_hwnd() -> Optional[int]:
    """Return the HWND of the Dota 2 window, or None if not running."""
    if sys.platform != "win32":
        return None
    found: list[int] = []

    def cb(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if any(t.lower() == title.lower() for t in DOTA_TITLES):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found[0] if found else None


def get_client_screen_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return (left, top, width, height) of the window's client area in
    screen coordinates, or None if the window is minimized / gone."""
    if sys.platform != "win32":
        return None
    if _user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(rect.left, rect.top)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return (pt.x, pt.y, width, height)


def is_foreground(hwnd: int) -> bool:
    if sys.platform != "win32":
        return True
    return _user32.GetForegroundWindow() == hwnd
