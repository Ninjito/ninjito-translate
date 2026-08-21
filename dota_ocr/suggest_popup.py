"""The popup that shows suggestions while the user types in Dota.

Two constraints shape this window. It must never take focus, or Dota
loses keyboard input mid-fight — hence WS_EX_NOACTIVATE, the same
treatment Overlay._apply_noactivate gives the main overlay. And it must
not accept clicks, because the user's mouse belongs to the game; every
interaction goes through the keyboard instead.

This is a pure renderer: the controller owns the item list and which
one is highlighted, and passes both in. Keeping the selection out here
matters because the keys that move it arrive on the hook thread, while
these widgets may only be touched from the Tk thread — reading the
highlight back off a widget would be a cross-thread race on the one
value Tab acts on.

All methods must be called on the Tk main thread.
"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk

from dota_ocr import sizes as _sz
from dota_ocr.suggest import Suggestion

_KIND_COLOR = {
    "fix": "#ff9f6b",        # a correction — you typed it wrong
    "complete": "#7bd88f",   # a completion — you're on track
    "sentence": "#8ab4ff",   # whole-line grammar
    "translate": "#f2c94c",  # whole-line translation
}
_KIND_LABEL = {
    "fix": "fix",
    "complete": "word",
    "sentence": "grammar",
    "translate": "translate",
}

MAX_ROWS = 8


class SuggestPopup:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._win: tk.Toplevel | None = None
        self._rows: list[tk.Label] = []
        self._count = 0

    @property
    def visible(self) -> bool:
        return self._win is not None and self._count > 0

    def show(self, items: list[Suggestion], index: int, x: int, y: int) -> None:
        """Render `items` with row `index` highlighted."""
        items = list(items)[:MAX_ROWS]
        if not items:
            self.hide()
            return

        if self._win is None:
            self._build()
        win = self._win
        if win is None:
            return

        self._count = len(items)
        height = len(items) * _sz.SUGGEST_ROW_HEIGHT + _sz.SUGGEST_PAD * 2
        try:
            win.geometry(f"{_sz.SUGGEST_WIDTH}x{height}+{int(x)}+{int(y)}")
            win.deiconify()
            win.lift()
        except Exception:
            pass
        self._render(items, index)

    def hide(self) -> None:
        self._count = 0
        if self._win is not None:
            try:
                self._win.withdraw()
            except Exception:
                pass

    def destroy(self) -> None:
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win = None
        self._rows = []
        self._count = 0

    # ---- internals ----

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        self._win = win
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92)
        win.configure(bg="#0a0a0a", highlightthickness=1,
                      highlightbackground="#2a2a3a")
        self._apply_noactivate(win)

        self._rows = [
            tk.Label(win, text="", bg="#0a0a0a", fg="#e0e0e0",
                     font=("Consolas", 10), anchor="w", padx=8, pady=1)
            for _ in range(MAX_ROWS)
        ]

    def _apply_noactivate(self, win: tk.Toplevel) -> None:
        """Same treatment as the main overlay: no focus theft, no clicks.

        WS_EX_TRANSPARENT needs WS_EX_LAYERED, which Tk's alpha already
        sets; it's OR'd in defensively.
        """
        if sys.platform != "win32":
            return
        try:
            win.update_idletasks()
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            GA_ROOT = 2
            user32 = ctypes.windll.user32
            hwnd = win.winfo_id()
            hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _render(self, items: list[Suggestion], index: int) -> None:
        if self._win is None:
            return
        for lbl in self._rows:
            try:
                lbl.pack_forget()
            except Exception:
                pass
        for i, item in enumerate(items):
            lbl = self._rows[i]
            marker = "▸ " if i == index else "  "
            tag = _KIND_LABEL.get(item.kind, item.kind)
            lbl.configure(
                text=f"{marker}{item.text}    [{tag}]",
                fg=("#ffffff" if i == index
                    else _KIND_COLOR.get(item.kind, "#e0e0e0")),
                bg=("#2a2a1a" if i == index else "#0a0a0a"),
            )
            lbl.pack(fill="x")
