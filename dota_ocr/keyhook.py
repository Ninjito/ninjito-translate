"""A low-level Windows keyboard hook, kept deliberately dumb.

Windows silently removes a WH_KEYBOARD_LL hook whose callback takes
longer than LowLevelHooksTimeout (~300ms). Once removed there is no
notification and no error — suggestions just stop appearing forever.
So the callback here does the absolute minimum: decode the key, ask a
caller-supplied predicate whether to swallow it, hand the event off,
and return. Anything expensive belongs on the consumer's own thread.

The hook must live on a thread that pumps messages, which is why this
class owns one rather than borrowing the existing hotkey thread in
overlay.py — a slow hotkey handler there would stall the hook and get
it uninstalled.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable

from dota_ocr.typing_buffer import KeyEvent

# Marks keystrokes this app injects (see typer.py) so the hook can
# ignore its own output instead of feeding it back into the buffer.
SYNTHETIC_TAG = 0x4E4A5447  # 'NJTG'

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14

# ToUnicodeEx flag: do not disturb the kernel's keyboard state. Without
# it we would consume dead keys out from under the app the user is
# really typing into.
_TOUNICODE_NO_STATE_CHANGE = 0x04

_IS_WIN = sys.platform == "win32"


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class KeyboardHook:
    def __init__(
        self,
        on_event: Callable[[KeyEvent], None],
        should_swallow: Callable[[KeyEvent], bool],
    ) -> None:
        self._on_event = on_event
        self._should_swallow = should_swallow
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None
        self._running = False
        self._proc = None          # keep the trampoline alive
        self.last_error = ""

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        if not _IS_WIN:
            self.last_error = "not Windows"
            return False
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), daemon=True,
            name="ninjito-keyhook",
        )
        self._thread.start()
        ready.wait(timeout=3.0)
        return self._running

    def stop(self) -> None:
        if not self._running and self._thread_id == 0:
            return
        self._running = False
        tid = self._thread_id
        if tid:
            try:
                # WM_QUIT breaks the message loop, which then unhooks.
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread_id = 0
        self._thread = None

    # ---- hook thread ----

    def _run(self, ready: threading.Event) -> None:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_long
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

            self._thread_id = kernel32.GetCurrentThreadId()
            self._proc = _HOOKPROC(self._callback)
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc, None, 0)

            if not self._hook:
                err = kernel32.GetLastError()
                # Error 5 is ACCESS_DENIED, which in practice means Dota
                # is running elevated and this app is not.
                self.last_error = (
                    "access denied - run this app as administrator"
                    if err == 5 else f"SetWindowsHookEx failed ({err})"
                )
                print(f"[keyhook] {self.last_error}", flush=True)
                ready.set()
                return

            self._running = True
            print("[keyhook] installed", flush=True)
            ready.set()

            msg = wintypes.MSG()
            while self._running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self.last_error = str(e)
            print(f"[keyhook] thread error: {e}", flush=True)
            ready.set()
        finally:
            self._running = False
            try:
                if self._hook:
                    ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
                    print("[keyhook] removed", flush=True)
            except Exception:
                pass
            self._hook = None

    def _callback(self, ncode: int, wparam: int, lparam: int) -> int:
        """Runs on every keystroke system-wide. Must stay trivial."""
        try:
            if ncode != 0:
                return ctypes.windll.user32.CallNextHookEx(
                    self._hook, ncode, wparam, lparam)

            kb = ctypes.cast(lparam,
                             ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents

            # Ignore the keys we injected ourselves (typer.py), or they
            # would loop straight back into the buffer.
            if kb.dwExtraInfo == SYNTHETIC_TAG:
                return ctypes.windll.user32.CallNextHookEx(
                    self._hook, ncode, wparam, lparam)

            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            ev = KeyEvent(
                vk=int(kb.vkCode),
                down=down,
                char=_decode(int(kb.vkCode), int(kb.scanCode)) if down else "",
                shift=_is_down(VK_SHIFT),
                ctrl=_is_down(VK_CONTROL),
                alt=_is_down(VK_MENU),
            )

            try:
                swallow = bool(self._should_swallow(ev))
            except Exception:
                swallow = False

            try:
                self._on_event(ev)
            except Exception:
                pass

            if swallow:
                return 1  # consumed; Dota never sees this key
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(
            self._hook, ncode, wparam, lparam)


def _is_down(vk: int) -> bool:
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _decode(vk: int, scan: int) -> str:
    """Map a virtual key to the character it produces right now.

    Goes through the foreground window's keyboard layout rather than
    assuming US English, so a Russian layout yields Cyrillic instead of
    mis-mapped Latin letters.
    """
    if not _IS_WIN:
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        layout = user32.GetKeyboardLayout(tid)

        state = (ctypes.c_ubyte * 256)()
        if not user32.GetKeyboardState(ctypes.byref(state)):
            return ""
        # GetKeyboardState reflects the *processing* thread, which is
        # ours, not the one that generated the key. Patch in the two
        # modifiers that change which character a key produces.
        state[VK_SHIFT] = 0x80 if _is_down(VK_SHIFT) else 0
        state[VK_CONTROL] = 0x80 if _is_down(VK_CONTROL) else 0
        state[VK_MENU] = 0x80 if _is_down(VK_MENU) else 0
        state[VK_CAPITAL] = user32.GetKeyState(VK_CAPITAL) & 0x0001

        buf = ctypes.create_unicode_buffer(8)
        n = user32.ToUnicodeEx(vk, scan, ctypes.byref(state), buf,
                               len(buf), _TOUNICODE_NO_STATE_CHANGE, layout)
        if n > 0:
            ch = buf.value[:n]
            return ch if ch.isprintable() else ""
    except Exception:
        pass
    return ""
