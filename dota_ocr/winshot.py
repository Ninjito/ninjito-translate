"""PrintWindow-based capture of a single Win32 window.

Why this exists: mss (and any screen-coord capture) grabs whatever
pixels are visible at that screen rectangle. If another window is on
top of Dota, you'll capture that other window — we saw this happen
with a VS Code terminal showing `python` being OCR'd as "chat".

PrintWindow pulls from the target window's own render buffer. The flag
`PW_RENDERFULLCONTENT` (Windows 8.1+) is required to get hardware-
accelerated / DirectX content such as Dota 2. It works while:

* the target window is in **windowed** or **borderless windowed** mode
* the target window exists and is not minimized

Exclusive fullscreen often produces black bitmaps. For that case the
caller should fall back to screen capture.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, Structure, c_int, c_ubyte, c_uint, wintypes
from typing import Optional

import numpy as np

PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", c_uint),
        ("biWidth", c_int),
        ("biHeight", c_int),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", c_uint),
        ("biSizeImage", c_uint),
        ("biXPelsPerMeter", c_int),
        ("biYPelsPerMeter", c_int),
        ("biClrUsed", c_uint),
        ("biClrImportant", c_uint),
    ]


class _BITMAPINFO(Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", c_uint * 3),
    ]


if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32

    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = ctypes.c_bool
    _user32.GetClientRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
    _user32.PrintWindow.restype = ctypes.c_bool

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, c_int, c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = ctypes.c_bool
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = ctypes.c_bool
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, c_uint, c_uint,
        ctypes.c_void_p, POINTER(_BITMAPINFO), c_uint,
    ]
    _gdi32.GetDIBits.restype = c_int


# --- Reused GDI resources ---------------------------------------------
#
# The DC, the bitmap and the pixel buffer depend only on the window and
# its size, so recreating them per frame bought nothing and cost real
# time: allocating the 8 MB ctypes buffer alone measured 2.4 ms, on top
# of four GDI object create/destroy round trips. We keep one set alive
# and rebuild it only when the window or its client size changes.
_gdi_cache: dict = {
    "hwnd": None, "size": None, "hwnd_dc": None,
    "mem_dc": None, "bitmap": None, "old": None,
    "buf": None, "arr": None, "bmi": None,
}


def release_cache() -> None:
    """Drop the cached GDI objects. Safe to call repeatedly."""
    c = _gdi_cache
    try:
        if c["mem_dc"] and c["old"]:
            _gdi32.SelectObject(c["mem_dc"], c["old"])
        if c["bitmap"]:
            _gdi32.DeleteObject(c["bitmap"])
        if c["mem_dc"]:
            _gdi32.DeleteDC(c["mem_dc"])
        if c["hwnd"] and c["hwnd_dc"]:
            _user32.ReleaseDC(c["hwnd"], c["hwnd_dc"])
    except Exception:
        pass
    c.update({"hwnd": None, "size": None, "hwnd_dc": None, "mem_dc": None,
              "bitmap": None, "old": None, "buf": None, "arr": None,
              "bmi": None})


def _ensure_cache(hwnd: int, width: int, height: int) -> bool:
    """Point the cache at this window/size, rebuilding it if needed."""
    c = _gdi_cache
    if (c["hwnd"] == hwnd and c["size"] == (width, height)
            and c["mem_dc"] and _user32.IsWindow(hwnd)):
        return True
    release_cache()

    hwnd_dc = _user32.GetDC(hwnd)
    if not hwnd_dc:
        return False
    mem_dc = _gdi32.CreateCompatibleDC(hwnd_dc)
    if not mem_dc:
        _user32.ReleaseDC(hwnd, hwnd_dc)
        return False
    bitmap = _gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    if not bitmap:
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(hwnd, hwnd_dc)
        return False

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bmi.bmiHeader.biSizeImage = width * height * 4

    buf = (c_ubyte * (width * height * 4))()
    c.update({
        "hwnd": hwnd, "size": (width, height), "hwnd_dc": hwnd_dc,
        "mem_dc": mem_dc, "bitmap": bitmap,
        "old": _gdi32.SelectObject(mem_dc, bitmap),
        "buf": buf, "bmi": bmi,
        "arr": np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4),
    })
    return True


def grab_window(hwnd: int,
                crop: Optional[tuple] = None) -> Optional[np.ndarray]:
    """Capture the *client area* of the given window as a BGR numpy array.

    ``crop`` is an optional ``(left, top, width, height)`` rectangle in
    client coordinates. Passing it is much cheaper than cropping the
    result: the caller's region is copied straight out of the reused
    pixel buffer, skipping an 8 MB full-frame copy that measured 8.2 ms
    per capture and was discarded immediately afterwards.

    Returns None on any failure (minimized, GetDC failed, PrintWindow
    refused, DirectX exclusive-fullscreen) so the caller can fall back
    to screen capture.
    """
    if sys.platform != "win32":
        return None

    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    if not _ensure_cache(hwnd, width, height):
        return None
    c = _gdi_cache

    if not _user32.PrintWindow(hwnd, c["mem_dc"], PW_RENDERFULLCONTENT):
        return None

    scanlines = _gdi32.GetDIBits(
        c["mem_dc"], c["bitmap"], 0, height, c["buf"],
        ctypes.byref(c["bmi"]), DIB_RGB_COLORS
    )
    if scanlines == 0:
        return None

    arr = c["arr"]
    # PrintWindow on exclusive-fullscreen DX returns an all-black bitmap.
    # Sampled every 16th pixel: averaging the whole frame cost 6.8 ms and
    # answered the same yes/no question.
    if arr[::16, ::16, :3].mean() < 1.0:
        return None

    if crop is None:
        # BGRA -> BGR (match mss contract)
        return arr[:, :, :3].copy()

    left, top, cw, ch = (int(v) for v in crop)
    left = max(0, left)
    top = max(0, top)
    right = min(left + cw, width)
    bottom = min(top + ch, height)
    if right - left <= 5 or bottom - top <= 5:
        return None
    return arr[top:bottom, left:right, :3].copy()
