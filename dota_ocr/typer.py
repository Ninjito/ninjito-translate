"""Type an accepted suggestion into Dota's chat box.

Uses SendInput with KEYEVENTF_UNICODE rather than keybd_event with
virtual key codes, because the replacement word has to arrive intact no
matter which keyboard layout is active — a VK-based path would produce
Cyrillic on a Russian layout.

Every event carries SYNTHETIC_TAG in dwExtraInfo so keyhook.py can
recognise our own output and skip it. Without that the injected
characters would be captured as if the user had typed them, and the
buffer would double up.

Two ctypes traps are load-bearing here, both of which make SendInput
fail with ERROR_INVALID_PARAMETER while looking perfectly correct:

  * INPUT's union must contain MOUSEINPUT, not just KEYBDINPUT. Windows
    validates cbSize against the full 40-byte structure; a keyboard-only
    union is 32 bytes and every call is rejected.
  * argtypes must be declared or the 64-bit array pointer is truncated
    to 32 bits. overlay.py hit the same trap in _paste_to_dota_chat.

Note that _paste_to_dota_chat uses a different mechanism (clipboard +
Ctrl+V) for sending a whole finished message. This module is for the
in-place edits that happen mid-typing, where the clipboard would
clobber whatever the user has in it.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from dota_ocr.keyhook import SYNTHETIC_TAG

_IS_WIN = sys.platform == "win32"

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_BACK = 0x08


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MOUSEINPUT(ctypes.Structure):
    """Never sent — present only so INPUT reaches its real size."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if _IS_WIN:
    _user32 = ctypes.windll.user32
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                                  ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
else:  # pragma: no cover - the app only runs on Windows
    _user32 = None


def _key(vk: int, scan: int, flags: int) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD,
                  ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                 time=0, dwExtraInfo=SYNTHETIC_TAG))


def _send(inputs: list) -> None:
    """Push a batch of key events. Replaced in tests."""
    if not _IS_WIN or not inputs:
        return
    try:
        arr = (_INPUT * len(inputs))(*inputs)
        sent = _user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
        if sent != len(arr):
            print(f"[typer] SendInput sent {sent}/{len(arr)}", flush=True)
    except Exception as e:
        print(f"[typer] SendInput failed: {e}", flush=True)


def send_backspaces(n: int) -> None:
    """Erase `n` characters to the left of the caret."""
    if n <= 0:
        return
    events = []
    for _ in range(n):
        events.append(_key(VK_BACK, 0, 0))
        events.append(_key(VK_BACK, 0, KEYEVENTF_KEYUP))
    _send(events)


def send_text(text: str) -> None:
    """Type `text` as Unicode, independent of keyboard layout."""
    if not text:
        return
    events = []
    for ch in text:
        code = ord(ch)
        events.append(_key(0, code, KEYEVENTF_UNICODE))
        events.append(_key(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(events)


def replace_word(backspaces: int, replacement: str) -> None:
    """Erase the half-typed word and type the chosen one in its place."""
    send_backspaces(backspaces)
    send_text(replacement)
