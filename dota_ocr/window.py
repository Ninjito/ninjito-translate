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
import time
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
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = ctypes.c_bool
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    try:
        _dwmapi = ctypes.windll.dwmapi
        _dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except Exception:
        _dwmapi = None
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.ClientToScreen.restype = ctypes.c_bool
    _user32.GetForegroundWindow.restype = wintypes.HWND

    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = ctypes.c_bool


# --- HWND cache -------------------------------------------------------
#
# EnumWindows walks every top-level window on the desktop and calls back
# into Python for each one, so a single lookup costs hundreds of
# syscalls plus hundreds of ctypes trampoline crossings. Four separate
# timers ask for this handle a few times a second each, which made the
# sweep one of the app's largest idle costs.
#
# A window handle stays valid for the life of the window, so we keep the
# last one and re-validate it with four cheap syscalls instead. The full
# sweep only runs when that validation fails — i.e. Dota just started or
# just closed — and is rate-limited so a closed Dota doesn't put us back
# to sweeping on every tick.
_ENUM_MIN_INTERVAL = 1.0

_hwnd_cache: dict = {"hwnd": None, "last_enum": 0.0}

_DOTA_TITLES_LOWER = frozenset(t.lower() for t in DOTA_TITLES)


def _title_is_dota(hwnd: int) -> bool:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return False
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.lower() in _DOTA_TITLES_LOWER


def _still_dota(hwnd: Optional[int]) -> bool:
    """Is this cached handle still the visible Dota window?"""
    if not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(hwnd)
                    and _user32.IsWindowVisible(hwnd)
                    and _title_is_dota(hwnd))
    except Exception:
        return False


def _enum_cb(hwnd: int, _lparam: int) -> bool:
    if not _user32.IsWindowVisible(hwnd):
        return True
    if _title_is_dota(hwnd):
        _enum_found.append(hwnd)
        return False   # stop the sweep; we only ever want the first hit
    return True


if sys.platform == "win32":
    _enum_found: list[int] = []
    # Built once. Rebuilding the trampoline per call allocated an
    # executable thunk on every lookup.
    _ENUM_PROC = _WNDENUMPROC(_enum_cb)


def _sweep_for_dota() -> Optional[int]:
    _enum_found.clear()
    _user32.EnumWindows(_ENUM_PROC, 0)
    return _enum_found[0] if _enum_found else None


def find_dota_hwnd(force: bool = False) -> Optional[int]:
    """Return the HWND of the Dota 2 window, or None if not running.

    Cached: a valid handle is re-validated rather than re-discovered.
    Pass ``force=True`` to bypass the cache entirely.
    """
    if sys.platform != "win32":
        return None

    cached = _hwnd_cache["hwnd"]
    if not force and _still_dota(cached):
        return cached

    now = time.monotonic()
    if not force and cached is None:
        # Dota wasn't running last time we looked. Don't re-sweep the
        # whole desktop on every heartbeat just to learn that again.
        if (now - _hwnd_cache["last_enum"]) < _ENUM_MIN_INTERVAL:
            return None

    _hwnd_cache["last_enum"] = now
    hwnd = _sweep_for_dota()
    _hwnd_cache["hwnd"] = hwnd
    return hwnd


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


# --- Occlusion ---------------------------------------------------------
#
# Screen capture is far cheaper than PrintWindow (0.62ms of CPU against
# 5.0ms, and it doesn't force a readback of the game's render surface),
# but it grabs whatever is *visually* at those coordinates. That is the
# bug PrintWindow was brought in to fix: a VS Code terminal sitting over
# the chat area got OCR'd as chat. So screen capture is only usable when
# we can prove nothing is drawn on top of the region we want.
#
# The test walks the Z-order above Dota and intersects rectangles. It
# deliberately does NOT use WindowFromPoint: this app's own overlay sets
# WS_EX_TRANSPARENT when locked, so hit-testing looks straight through it
# to Dota while it still very much renders over the chat.

DWMWA_CLOAKED = 14

# The desktop wallpaper hosts. Both are permanently "visible" and span
# the whole desktop, and both render behind every real window — so they
# match the intersection test while never actually covering anything.
_DESKTOP_CLASSES = frozenset({"Progman", "WorkerW"})


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    if _user32.GetClassNameW(hwnd, buf, 64) <= 0:
        return ""
    return buf.value


def _is_cloaked(hwnd: int) -> bool:
    """DWM-hidden (a suspended UWP app) — visible by flag, not on screen."""
    if _dwmapi is None:
        return False
    val = ctypes.c_int(0)
    try:
        if _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                         ctypes.byref(val),
                                         ctypes.sizeof(val)) != 0:
            return False
    except Exception:
        return False
    return val.value != 0


def _windows_above(hwnd: int) -> list:
    """Top-level windows drawn in front of `hwnd`, front-most first.

    EnumWindows walks the Z-order from the top, so everything it hands
    over before it reaches `hwnd` is above it.
    """
    above: list[int] = []
    hit = [False]

    def cb(h, _lparam):
        if h == hwnd:
            hit[0] = True
            return False        # reached our window; stop
        above.append(h)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return above if hit[0] else []


def region_is_unoccluded(hwnd: int, rect: Tuple[int, int, int, int]) -> bool:
    """Is `rect` (screen coords, l/t/w/h) showing `hwnd` and nothing else?

    Conservative by construction: anything it cannot rule out counts as
    an occluder, and the caller falls back to PrintWindow. Being wrong in
    that direction costs a few milliseconds; being wrong the other way
    OCRs somebody else's window.
    """
    if sys.platform != "win32":
        return False
    if not hwnd or _user32.IsIconic(hwnd):
        return False
    # Anything in front of a background window is not worth reasoning
    # about — alt-tabbed away, the region may not even be on screen.
    if _user32.GetForegroundWindow() != hwnd:
        return False

    left, top, width, height = rect
    right, bottom = left + width, top + height

    for other in _windows_above(hwnd):
        try:
            if not _user32.IsWindowVisible(other) or _user32.IsIconic(other):
                continue
            if _is_cloaked(other):
                continue
            if _class_name(other) in _DESKTOP_CLASSES:
                continue        # the desktop is behind us, not over us
            r = wintypes.RECT()
            if not _user32.GetWindowRect(other, ctypes.byref(r)):
                continue
            if r.right <= r.left or r.bottom <= r.top:
                continue        # zero-size helper window
            if (r.left < right and r.right > left
                    and r.top < bottom and r.bottom > top):
                return False    # overlaps the chat region
        except Exception:
            return False        # couldn't tell -> assume occluded
    return True
