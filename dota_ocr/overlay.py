"""Borderless, semi-transparent, always-on-top translation overlay.

Controls:
    * Click the "Translate" button (or press F7) to capture + translate.
    * Left-click + drag on the text area to move the window.
    * ESC or right-click to close.
    * Shift + mouse-wheel to adjust transparency.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from tkinter import ttk


# Map common Virtual-Key codes to readable names.
_VK_NAMES = {
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12",
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter",
    0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home",
    0x2D: "Insert", 0x2E: "Delete", 0x1B: "Esc",
}


def _vk_to_name(vk: int) -> str:
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x30 <= vk <= 0x39:  # 0-9
        return chr(vk)
    if 0x41 <= vk <= 0x5A:  # A-Z
        return chr(vk)
    return f"VK_{vk:02X}"


# Reverse map: Tkinter keysym -> VK code for capturing a key rebind.
_KEYSYM_TO_VK = {
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    "space": 0x20, "Tab": 0x09, "Return": 0x0D,
    "Prior": 0x21, "Next": 0x22, "End": 0x23, "Home": 0x24,
    "Insert": 0x2D, "Delete": 0x2E,
}


class Overlay:
    def __init__(
        self,
        x: int = 50,
        y: int = 50,
        width: int = 640,
        height: int = 400,  # Taller to fit more messages
        alpha: float = 0.55,
        font_size: int = 11,
        max_messages: int = 50,  # Show many messages, user can scroll
        hotkey_vk: int = 0x76,  # F7
        on_recalibrate=None,  # callback(new_relative_bbox: dict) when user resizes
        on_hotkey_changed=None,  # callback(new_vk: int, name: str) on rebind
    ):
        self.max_messages = max_messages
        self._msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._trigger_event = threading.Event()
        self._messages: list[tuple[str, str]] = []
        self._alpha = max(0.15, min(1.0, alpha))
        self._on_recalibrate = on_recalibrate
        self._on_hotkey_changed = on_hotkey_changed
        self._hotkey_name = _vk_to_name(hotkey_vk)

        self.root = tk.Tk()
        self.root.title("Dota 2 Translate")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha)
        self.root.configure(bg="#0a0a0a")
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # --- Top bar with Translate button ---
        bar = tk.Frame(self.root, bg="#151515", height=28)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        self._btn = tk.Button(
            bar,
            text="📷 Translate",
            bg="#1a3a1a",
            fg="#7bd88f",
            activebackground="#2a5a2a",
            activeforeground="#aaffaa",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_translate_click,
        )
        self._btn.pack(side="left", padx=4, pady=2)

        self._resize_btn = tk.Button(
            bar,
            text="📐 Resize",
            bg="#1a1a3a",
            fg="#8fa8d8",
            activebackground="#2a2a5a",
            activeforeground="#aaccff",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_resize_click,
        )
        self._resize_btn.pack(side="left", padx=2, pady=2)

        self._lock_btn = tk.Button(
            bar,
            text="🔓 Unlocked",
            bg="#2a1a1a",
            fg="#d88f8f",
            activebackground="#5a2a2a",
            activeforeground="#ffaaaa",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_lock_toggle,
        )
        self._lock_btn.pack(side="left", padx=2, pady=2)
        self._locked = False

        self._hotkey_btn = tk.Button(
            bar,
            text=f"🎹 {self._hotkey_name}",
            bg="#2a2a1a",
            fg="#d8d88f",
            activebackground="#5a5a2a",
            activeforeground="#ffffaa",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_hotkey_rebind,
        )
        self._hotkey_btn.pack(side="left", padx=2, pady=2)
        self._rebinding = False

        self._status = tk.Label(
            bar,
            text="Press button or F7 to translate",
            bg="#151515",
            fg="#555",
            font=("Consolas", 8),
        )
        self._status.pack(side="left", padx=6)

        # --- Text display with scrollbar ---
        text_frame = tk.Frame(self.root, bg="#0a0a0a")
        text_frame.pack(fill="both", expand=True)

        # Use ttk + clam theme so the scrollbar actually respects custom
        # colors (native Win theme ignores them on plain tk.Scrollbar).
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.layout(
            "Dark.Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {
                "children": [("Vertical.Scrollbar.thumb", {
                    "expand": "1", "sticky": "nswe"
                })],
                "sticky": "ns",
            })],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#0a0a0a",     # thumb color when idle (invisible)
            troughcolor="#0a0a0a",    # track color (invisible)
            bordercolor="#0a0a0a",
            arrowcolor="#0a0a0a",
            darkcolor="#0a0a0a",
            lightcolor="#0a0a0a",
            gripcount=0,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[
                ("pressed", "#6a6a6a"),  # only visible while dragging
                ("active", "#3a3a3a"),   # slightly visible on hover
            ],
        )
        scrollbar = ttk.Scrollbar(
            text_frame, style="Dark.Vertical.TScrollbar", orient="vertical"
        )
        scrollbar.pack(side="right", fill="y")

        self.text = tk.Text(
            text_frame,
            bg="#0a0a0a",
            fg="#e0e0e0",
            insertbackground="#e0e0e0",
            font=("Consolas", font_size),
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            padx=8,
            pady=4,
            state="disabled",
            cursor="fleur",
            yscrollcommand=scrollbar.set,
        )
        self.text.tag_configure("src", foreground="#8a8a8a")
        self.text.tag_configure("dst", foreground="#7bd88f")
        self.text.pack(side="left", fill="both", expand=True)

        scrollbar.config(command=self.text.yview)

        # --- Drag to move (on text area) ---
        self._drag_anchor: tuple[int, int] | None = None
        self.text.bind("<ButtonPress-1>", self._on_drag_start)
        self.text.bind("<B1-Motion>", self._on_drag_move)
        self.text.bind("<ButtonRelease-1>", self._on_drag_end)

        # --- Close / transparency ---
        # Only ESC closes the app (and only when the overlay has focus).
        # Right-click does NOT close anymore.
        self.root.bind(
            "<Escape>",
            lambda _: (None if self._locked else self._close()),
        )
        self.text.bind("<Shift-MouseWheel>", self._on_alpha_wheel)
        self.root.bind("<Shift-MouseWheel>", self._on_alpha_wheel)

        self._closing = False
        self.root.after(120, self._drain)

        # --- Global hotkey (F7) via Windows API ---
        self._hotkey_vk = hotkey_vk
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_listener, daemon=True
        )
        self._hotkey_thread.start()

    # ---- hotkey ----
    _HOTKEY_ID = 1

    def _hotkey_listener(self) -> None:
        """Register a system-wide hotkey and listen for it.

        Keeps `self._hotkey_thread_id` so we can post WM_NULL to wake
        GetMessage when rebinding.
        """
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._hotkey_thread_id = kernel32.GetCurrentThreadId()
            MOD_NONE = 0
            current_vk = self._hotkey_vk
            if not user32.RegisterHotKey(None, self._HOTKEY_ID, MOD_NONE, current_vk):
                print(f"[overlay] Could not register global hotkey "
                      f"{_vk_to_name(current_vk)} (may be in use).", flush=True)
                current_vk = None

            msg = ctypes.wintypes.MSG()
            while not self._closing:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == 0x0312:  # WM_HOTKEY
                    self._trigger_event.set()
                elif msg.message == 0x0400:  # WM_USER — rebind request
                    new_vk = self._hotkey_vk
                    if current_vk is not None:
                        user32.UnregisterHotKey(None, self._HOTKEY_ID)
                    ok = user32.RegisterHotKey(None, self._HOTKEY_ID, MOD_NONE, new_vk)
                    if ok:
                        current_vk = new_vk
                        print(f"[overlay] Hotkey rebound to {_vk_to_name(new_vk)}",
                              flush=True)
                    else:
                        current_vk = None
                        print(f"[overlay] Failed to bind {_vk_to_name(new_vk)} "
                              "(in use by another app)", flush=True)
            if current_vk is not None:
                user32.UnregisterHotKey(None, self._HOTKEY_ID)
        except Exception:
            pass

    def _request_hotkey_reregister(self) -> None:
        """Post WM_USER to the hotkey thread so it re-registers."""
        try:
            tid = getattr(self, "_hotkey_thread_id", None)
            if tid:
                ctypes.windll.user32.PostThreadMessageW(tid, 0x0400, 0, 0)
        except Exception:
            pass

    # ---- hotkey rebind ----
    def _on_hotkey_rebind(self) -> None:
        if self._rebinding:
            return
        self._rebinding = True
        self._hotkey_btn.configure(text="🎹 Press a key...", bg="#5a2a2a")
        self.set_status("Press any key (Esc = cancel)", "#ffa500")

        # Grab focus so we receive the keystroke.
        self.root.focus_force()

        def capture(event: tk.Event) -> str:
            keysym = event.keysym
            if keysym == "Escape":
                self._finish_rebind(None)
                return "break"
            vk = _KEYSYM_TO_VK.get(keysym)
            if vk is None and len(keysym) == 1 and keysym.isalnum():
                vk = ord(keysym.upper())
            if vk is None:
                self.set_status(f"Unsupported key ({keysym}) — try F1-F12",
                                "#ff4444")
                return "break"
            self._finish_rebind(vk)
            return "break"

        # Bind globally (on root) with a unique tag so we can unbind it.
        self._rebind_bind_id = self.root.bind("<Key>", capture)

    def _finish_rebind(self, new_vk: int | None) -> None:
        try:
            self.root.unbind("<Key>", self._rebind_bind_id)
        except Exception:
            pass
        self._rebinding = False
        if new_vk is None:
            self._hotkey_btn.configure(
                text=f"🎹 {self._hotkey_name}", bg="#2a2a1a"
            )
            self.set_status("Rebind cancelled", "#888")
            return
        self._hotkey_vk = new_vk
        self._hotkey_name = _vk_to_name(new_vk)
        self._hotkey_btn.configure(
            text=f"🎹 {self._hotkey_name}", bg="#2a2a1a"
        )
        self._request_hotkey_reregister()
        self.set_status(f"Hotkey set to {self._hotkey_name}", "#7bd88f")
        if self._on_hotkey_changed is not None:
            try:
                self._on_hotkey_changed(new_vk, self._hotkey_name)
            except Exception:
                pass

    # ---- translate trigger ----
    def _on_translate_click(self) -> None:
        self._status.configure(text="Capturing...", fg="#ffa500")
        self._trigger_event.set()

    # ---- resize / recalibrate ----
    def _on_resize_click(self) -> None:
        """Open a fullscreen picker to re-select the chat region."""
        from dota_ocr.window import find_dota_hwnd, get_client_screen_rect

        hwnd = find_dota_hwnd()
        if hwnd is None:
            self.set_status("Dota 2 not running", "#ff4444")
            return
        client = get_client_screen_rect(hwnd)
        if client is None:
            self.set_status("Dota minimized — bring it forward", "#ff4444")
            return
        cx, cy, cw, ch = client

        # Hide the overlay temporarily so it doesn't block selection.
        self.root.withdraw()
        self.set_status("Draw chat region (ESC to cancel)", "#ffa500")

        # Build picker as a Toplevel of this Tk root.
        picker = tk.Toplevel(self.root)
        picker.attributes("-fullscreen", True)
        picker.attributes("-alpha", 0.3)
        picker.attributes("-topmost", True)
        picker.configure(bg="black")
        picker.config(cursor="cross")

        canvas = tk.Canvas(picker, bg="black", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)

        tk.Label(
            picker,
            text="Drag a tight box around the Dota 2 chat messages.   ESC = cancel",
            bg="#ffd84d",
            fg="black",
            font=("Arial", 14, "bold"),
            padx=12,
            pady=6,
        ).place(relx=0.5, y=30, anchor="n")

        state = {"start": None, "rect_id": None, "bbox": None}

        def on_press(e):
            state["start"] = (e.x_root, e.y_root)
            state["rect_id"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#ff3b3b", width=3
            )

        def on_drag(e):
            if state["rect_id"] is None or state["start"] is None:
                return
            sx = state["start"][0] - picker.winfo_rootx()
            sy = state["start"][1] - picker.winfo_rooty()
            canvas.coords(state["rect_id"], sx, sy, e.x, e.y)

        def on_release(e):
            if state["start"] is None:
                picker.destroy()
                return
            x1, y1 = state["start"]
            x2, y2 = e.x_root, e.y_root
            left, top = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)
            if w >= 20 and h >= 10:
                state["bbox"] = {"left": int(left), "top": int(top),
                                 "width": int(w), "height": int(h)}
            picker.destroy()

        def on_cancel(_e=None):
            picker.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        picker.bind("<Escape>", on_cancel)
        picker.grab_set()
        picker.wait_window()

        # Restore overlay
        self.root.deiconify()

        bbox = state["bbox"]
        if not bbox:
            self.set_status("Resize cancelled", "#888")
            return

        # Convert screen -> Dota-client-relative.
        rel = {
            "left": max(0, bbox["left"] - cx),
            "top": max(0, bbox["top"] - cy),
            "width": bbox["width"],
            "height": bbox["height"],
        }

        self.set_status(f"New region: {rel['width']}x{rel['height']}", "#7bd88f")
        if self._on_recalibrate is not None:
            try:
                self._on_recalibrate(rel)
            except Exception as e:
                self.set_status(f"Resize save failed: {e}", "#ff4444")

    def wait_for_trigger(self, timeout: float = 0.5) -> bool:
        """Block until the translate button/hotkey is pressed."""
        fired = self._trigger_event.wait(timeout=timeout)
        if fired:
            self._trigger_event.clear()
        return fired

    def set_status(self, text: str, color: str = "#555") -> None:
        try:
            self._status.configure(text=text, fg=color)
        except Exception:
            pass

    # ---- close ----
    def _close(self) -> None:
        self._closing = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def is_closing(self) -> bool:
        return self._closing

    # ---- dragging ----
    def _on_drag_start(self, event: tk.Event) -> None:
        if self._locked:
            return
        self._drag_anchor = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _on_drag_move(self, event: tk.Event) -> None:
        if self._locked or self._drag_anchor is None:
            return
        nx = event.x_root - self._drag_anchor[0]
        ny = event.y_root - self._drag_anchor[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _on_drag_end(self, _event: tk.Event) -> None:
        self._drag_anchor = None

    # ---- lock / unlock ----
    def _on_lock_toggle(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._lock_btn.configure(text="🔒 Locked", bg="#1a2a1a", fg="#8fd88f")
            self.text.configure(cursor="arrow")
            # Make the window non-focusable (WS_EX_NOACTIVATE) so clicking
            # on it never steals focus from Dota 2.
            self._apply_noactivate(True)
            self.set_status("Locked (drag disabled)", "#7bd88f")
        else:
            self._lock_btn.configure(text="🔓 Unlocked", bg="#2a1a1a", fg="#d88f8f")
            self.text.configure(cursor="fleur")
            self._apply_noactivate(False)
            self.set_status("Unlocked", "#888")

    def _apply_noactivate(self, enable: bool) -> None:
        """Toggle WS_EX_NOACTIVATE on the overlay window via Win32.

        When enabled, clicking the window doesn't make it the foreground
        window — focus stays on Dota 2.  Mouse wheel / buttons still
        work, just no focus stealing.
        """
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            hwnd = self.root.winfo_id()
            # Walk up to the top-level HWND (Tk's winfo_id is the inner
            # canvas/frame). Use GetAncestor(GA_ROOT).
            GA_ROOT = 2
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enable:
                style |= WS_EX_NOACTIVATE
            else:
                style &= ~WS_EX_NOACTIVATE
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    # ---- alpha ----
    def _on_alpha_wheel(self, event: tk.Event) -> None:
        step = 0.05 if event.delta > 0 else -0.05
        self._alpha = max(0.15, min(1.0, self._alpha + step))
        self.root.attributes("-alpha", self._alpha)

    # ---- message queue / render ----
    def push(self, original: str, translated: str) -> None:
        self._msg_queue.put((original, translated))

    def clear(self) -> None:
        """Clear all shown translations (called before each F7 batch)."""
        self._messages = []
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except Exception:
            pass

    def _drain(self) -> None:
        drained = False
        try:
            while True:
                orig, trans = self._msg_queue.get_nowait()
                self._messages.append((orig, trans))
                drained = True
        except queue.Empty:
            pass

        if drained:
            if len(self._messages) > self.max_messages:
                self._messages = self._messages[-self.max_messages:]
            self._render()

        if not self._closing:
            self.root.after(120, self._drain)

    def _render(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for orig, trans in self._messages:
            self.text.insert("end", f"{orig}\n", "src")
            self.text.insert("end", f"{trans}\n\n", "dst")
        self.text.see("end")
        self.text.configure(state="disabled")

    def mainloop(self) -> None:
        self.root.mainloop()
