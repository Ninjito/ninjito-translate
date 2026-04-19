"""Borderless, semi-transparent, always-on-top translation overlay.

Controls:
    * Click the "Translate" button (or press F7) to capture + translate.
    * Left-click + drag on the text area to move the window.
    * ESC or right-click to close.
    * Shift + mouse-wheel to adjust transparency.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def _resource_path(name: str) -> str:
    """Locate a bundled resource both in dev and under PyInstaller.

    Looks in sys._MEIPASS (frozen), then next to the executable,
    then next to the project root (source run)."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / name)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / name)
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / name)
    candidates.append(here / name)
    for p in candidates:
        if p.is_file():
            return str(p)
    return name  # last-ditch relative fallback


# Cache for the brand icon, loaded lazily once the Tk root exists.
_BRAND_IMG = {"icon": None, "thumb": None}


def _set_dark_titlebar(win: tk.Misc) -> None:
    """Switch a Tk window's native Windows titlebar to dark mode.

    The HWND sometimes isn't fully realized right after creation, so we
    run once now and again on a short delay, and also force a frame
    redraw so Windows actually repaints the non-client area."""
    def _apply():
        try:
            win.update_idletasks()
            hwnd = int(win.winfo_id())
            GetParent = ctypes.windll.user32.GetParent
            GetParent.restype = ctypes.c_void_p
            GetParent.argtypes = [ctypes.c_void_p]
            parent = GetParent(hwnd)
            if parent:
                hwnd = parent
            dwm = ctypes.windll.dwmapi
            value = ctypes.c_int(1)
            # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / Win11)
            # 19 = pre-20H1 attribute; try both.
            for attr in (20, 19):
                try:
                    dwm.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_int(attr),
                        ctypes.byref(value),
                        ctypes.sizeof(value),
                    )
                except Exception:
                    pass
            # Force the non-client area to redraw so the dark bar shows
            # up immediately (SetWindowPos + SWP_FRAMECHANGED).
            try:
                SWP_NOMOVE = 0x0002; SWP_NOSIZE = 0x0001
                SWP_NOZORDER = 0x0004; SWP_FRAMECHANGED = 0x0020
                ctypes.windll.user32.SetWindowPos(
                    ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
                )
            except Exception:
                pass
        except Exception:
            pass
    _apply()
    # Re-apply once the window is fully mapped so late-created Toplevels
    # (e.g. Paste & Translate) also get the dark bar.
    try:
        win.after(50, _apply)
        win.after(300, _apply)
    except Exception:
        pass


def _load_brand_icon(root: tk.Misc) -> tuple[tk.PhotoImage | None, tk.PhotoImage | None]:
    """Return (full_icon, small_thumb) PhotoImages for the app, or (None, None)."""
    if _BRAND_IMG["icon"] is not None:
        return _BRAND_IMG["icon"], _BRAND_IMG["thumb"]
    path = _resource_path("gg.png")
    if not os.path.isfile(path):
        return None, None
    try:
        # Prefer Pillow for high-quality resize; fall back to tk.PhotoImage.
        try:
            from PIL import Image, ImageTk
            img = Image.open(path).convert("RGBA")
            icon = ImageTk.PhotoImage(img.resize((64, 64), Image.LANCZOS), master=root)
            thumb = ImageTk.PhotoImage(img.resize((20, 20), Image.LANCZOS), master=root)
        except Exception:
            icon = tk.PhotoImage(master=root, file=path)
            thumb = icon.subsample(max(1, icon.width() // 20))
        _BRAND_IMG["icon"] = icon
        _BRAND_IMG["thumb"] = thumb
        return icon, thumb
    except Exception:
        return None, None


# Map common Virtual-Key codes to readable names.
_VK_NAMES = {
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12",
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter",
    0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home",
    0x2D: "Insert", 0x2E: "Delete", 0x1B: "Esc",
}


def _combo_name(vk: int, mods: int) -> str:
    """Render a (vk, modifier-mask) pair as e.g. 'Ctrl+Shift+L'."""
    parts = []
    if mods & 0x0002: parts.append("Ctrl")
    if mods & 0x0004: parts.append("Shift")
    if mods & 0x0001: parts.append("Alt")
    parts.append(_vk_to_name(vk))
    return "+".join(parts)


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
        cfg: dict = None,
    ):
        self._cfg = cfg or {}
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

        # --- Brand icon (window + taskbar) ---
        brand_icon, brand_thumb = _load_brand_icon(self.root)
        if brand_icon is not None:
            try:
                self.root.iconphoto(True, brand_icon)
            except Exception:
                pass
            # Also try the .ico on Windows for a sharper taskbar/titlebar icon.
            try:
                ico_path = _resource_path("gg.ico")
                if os.path.isfile(ico_path):
                    self.root.iconbitmap(default=ico_path)
            except Exception:
                pass
        self._brand_icon = brand_icon    # keep refs alive
        self._brand_thumb = brand_thumb

        # --- Top bar with Translate button ---
        bar = tk.Frame(self.root, bg="#151515", height=28)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        if self._brand_thumb is not None:
            tk.Label(bar, image=self._brand_thumb, bg="#151515",
                     borderwidth=0).pack(side="left", padx=(6, 2), pady=2)

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

        self._logs_btn = tk.Button(
            bar,
            text="📜 Logs",
            bg="#1a2a2a",
            fg="#8fd8d8",
            activebackground="#2a5a5a",
            activeforeground="#aaffff",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_logs_click,
        )
        self._logs_btn.pack(side="left", padx=2, pady=2)
        self._logs_window: tk.Toplevel | None = None

        self._paste_btn = tk.Button(
            bar,
            text="📋 Paste",
            bg="#2a1a2a",
            fg="#d8a8d8",
            activebackground="#5a2a5a",
            activeforeground="#ffaaff",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_paste_click,
        )
        self._paste_btn.pack(side="left", padx=2, pady=2)
        self._paste_window: tk.Toplevel | None = None

        self._settings_btn = tk.Button(
            bar,
            text="⚙️ Settings",
            bg="#2a2a2a",
            fg="#cccccc",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            font=("Consolas", 9, "bold"),
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._on_settings_click,
        )
        self._settings_btn.pack(side="left", padx=2, pady=2)
        self._settings_window: tk.Toplevel | None = None

        self._status = tk.Label(
            bar,
            text=f"Press button or {self._hotkey_name} to translate",
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
        self.text.tag_configure("dst", foreground="#7bd88f")
        # Channel colors — configured LAST so they always win over "src".
        self.text.tag_configure("src", foreground="#8a8a8a")  # dim fallback
        self.text.tag_configure("allies", foreground="#6bb8ff")      # blue
        self.text.tag_configure("all", foreground="#e0e0e0")          # white
        self.text.tag_configure("spectator", foreground="#aaaaaa")    # gray
        self.text.pack(side="left", fill="both", expand=True)

        scrollbar.config(command=self.text.yview)

        # --- Drag to move (on text area) ---
        self._drag_anchor: tuple[int, int] | None = None
        self.text.bind("<ButtonPress-1>", self._on_drag_start)
        self.text.bind("<B1-Motion>", self._on_drag_move)
        self.text.bind("<ButtonRelease-1>", self._on_drag_end)

        # --- Right-click context menu ---
        self.text.bind("<Button-3>", self._on_text_rightclick)

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

        # --- Auto-hide when Dota 2 isn't foreground ---
        # Poll the foreground window; withdraw the overlay (and any open
        # aux windows) while the user is in the browser / desktop, and
        # restore when Dota regains focus.  This runs on the Tk thread.
        self._auto_hidden = False
        self.root.after(400, self._tick_foreground_visibility)

        # --- Global hotkeys via Windows API (multi-action) ---
        # Action map: id -> { "name": label, "vk": int, "mods": int, "handler": cb }
        # IDs: 1=translate, 2=lock, 3=paste, 4=logs, 5=settings
        MOD_CTRL = 0x0002
        MOD_SHIFT = 0x0004
        self._action_defs = {
            1: {"name": "translate", "label": "Translate",
                "vk": hotkey_vk, "mods": 0,
                "handler": lambda: self._trigger_event.set()},
            2: {"name": "lock", "label": "Lock/unlock",
                "vk": 0x4C, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+L
                "handler": lambda: self.root.after(0, self._on_lock_toggle)},
            3: {"name": "paste", "label": "Paste window",
                "vk": 0x50, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+P
                "handler": lambda: self.root.after(0, self._on_paste_click)},
            4: {"name": "logs", "label": "Logs window",
                "vk": 0x48, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+H
                "handler": lambda: self.root.after(0, self._on_logs_click)},
            5: {"name": "settings", "label": "Settings",
                "vk": 0x4F, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+O
                "handler": lambda: self.root.after(0, self._on_settings_click)},
        }
        # Load overrides from cfg["hotkeys"] if present.
        stored = (self._cfg or {}).get("hotkeys", {}) if hasattr(self, "_cfg") else {}
        for aid, info in self._action_defs.items():
            cfg_entry = stored.get(info["name"])
            if isinstance(cfg_entry, dict):
                info["vk"] = int(cfg_entry.get("vk", info["vk"]))
                info["mods"] = int(cfg_entry.get("mods", info["mods"]))

        self._hotkey_vk = self._action_defs[1]["vk"]  # back-compat
        self._hotkey_name = _combo_name(self._action_defs[1]["vk"], self._action_defs[1]["mods"])
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_listener, daemon=True
        )
        self._hotkey_thread.start()

    # ---- hotkey ----
    _HOTKEY_ID = 1  # legacy — translate action id
    _LOCK_HOTKEY_ID = 2

    def _hotkey_listener(self) -> None:
        """Register a system-wide hotkey and listen for it.

        Keeps `self._hotkey_thread_id` so we can post WM_NULL to wake
        GetMessage when rebinding.
        """
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._hotkey_thread_id = kernel32.GetCurrentThreadId()

            def _register_all():
                # Unregister any previously-registered ids, then register
                # the current action definitions.
                for aid in list(self._action_defs.keys()):
                    try: user32.UnregisterHotKey(None, aid)
                    except Exception: pass
                for aid, info in self._action_defs.items():
                    ok = user32.RegisterHotKey(None, aid, info["mods"], info["vk"])
                    if not ok:
                        print(f"[overlay] Could not bind {info['label']}: "
                              f"{_combo_name(info['vk'], info['mods'])} "
                              f"(in use).", flush=True)

            _register_all()

            msg = ctypes.wintypes.MSG()
            while not self._closing:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == 0x0312:  # WM_HOTKEY
                    aid = int(msg.wParam)
                    info = self._action_defs.get(aid)
                    if info is not None:
                        try:
                            info["handler"]()
                        except Exception:
                            pass
                elif msg.message == 0x0400:  # WM_USER — re-register request
                    _register_all()
                elif msg.message == 0x0401:  # WM_USER+1 — unregister all
                    for aid in list(self._action_defs.keys()):
                        try: user32.UnregisterHotKey(None, aid)
                        except Exception: pass
            for aid in list(self._action_defs.keys()):
                try: user32.UnregisterHotKey(None, aid)
                except Exception: pass
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

    def _request_hotkey_unregister_all(self) -> None:
        """Temporarily disable ALL global hotkeys (used while capturing
        a new combo in Settings, so the existing shortcuts don't fire)."""
        try:
            tid = getattr(self, "_hotkey_thread_id", None)
            if tid:
                ctypes.windll.user32.PostThreadMessageW(tid, 0x0401, 0, 0)
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
        # Override the global Escape binding (which closes the app) so
        # that during rebind Escape only cancels the rebind.
        self._rebind_esc_id = self.root.bind(
            "<Escape>", lambda _e: (self._finish_rebind(None), "break")[1],
            add=False,
        )

    def _finish_rebind(self, new_vk: int | None) -> None:
        try:
            self.root.unbind("<Key>", self._rebind_bind_id)
        except Exception:
            pass
        # Restore the original global Escape=close binding.
        try:
            self.root.unbind("<Escape>", self._rebind_esc_id)
        except Exception:
            pass
        self.root.bind(
            "<Escape>",
            lambda _: (None if self._locked else self._close()),
        )
        self._rebinding = False
        if new_vk is None:
            self._hotkey_btn.configure(
                text=f"🎹 {self._hotkey_name}", bg="#2a2a1a"
            )
            self.set_status("Rebind cancelled", "#888")
            # Re-assert the existing hotkey registration in case focus/Esc
            # side-effects disturbed it, so F7 keeps working after cancel.
            self._request_hotkey_reregister()
            return
        self._hotkey_vk = new_vk
        self._hotkey_name = _vk_to_name(new_vk)
        self._hotkey_btn.configure(
            text=f"🎹 {self._hotkey_name}", bg="#2a2a1a"
        )
        self._request_hotkey_reregister()
        # Show confirmation briefly, then fall back to the dynamic
        # placeholder matching the new key.
        self.set_status(f"Hotkey set to {self._hotkey_name}", "#7bd88f")
        placeholder = f"Press button or {self._hotkey_name} to translate"
        self.root.after(
            1800,
            lambda p=placeholder: self._status.configure(text=p, fg="#555"),
        )
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
        # Close any auxiliary windows (logs, paste) so they don't linger
        # as orphan Toplevels after the main overlay is destroyed.
        for attr in ("_logs_window", "_paste_window", "_settings_window"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    if w.winfo_exists():
                        w.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)
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
            self.set_status("Locked — clicks pass through. Ctrl+Shift+L to unlock", "#7bd88f")
        else:
            self._lock_btn.configure(text="🔓 Unlocked", bg="#2a1a1a", fg="#d88f8f")
            self.text.configure(cursor="fleur")
            self._apply_noactivate(False)
            self.set_status("Unlocked", "#888")

    def _tick_foreground_visibility(self) -> None:
        """Hide the overlay while Dota 2 is not in the foreground.

        Considers Dota foreground = Dota itself OR our own overlay / its
        aux windows (so clicking the Paste window doesn't hide the app).
        """
        if self._closing:
            return
        try:
            from dota_ocr.window import find_dota_hwnd
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            fg = user32.GetForegroundWindow() or 0

            # Collect our own top-level HWNDs (main + toplevels).
            own_hwnds = set()
            try:
                GA_ROOT = 2
                user32.GetAncestor.restype = ctypes.c_void_p
                user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                for w in (self.root, self._logs_window, self._paste_window, self._settings_window):
                    if w is None:
                        continue
                    try:
                        if hasattr(w, "winfo_exists") and not w.winfo_exists():
                            continue
                        h = int(w.winfo_id())
                        own_hwnds.add(user32.GetAncestor(h, GA_ROOT) or h)
                    except Exception:
                        pass
            except Exception:
                pass

            dota_hwnd = find_dota_hwnd()
            is_dota_fg = bool(dota_hwnd and fg == dota_hwnd)
            is_ours_fg = fg in own_hwnds
            should_show = is_dota_fg or is_ours_fg

            if should_show and self._auto_hidden:
                try:
                    self.root.deiconify()
                except Exception:
                    pass
                for w in (self._logs_window, self._paste_window, self._settings_window):
                    try:
                        if w is not None and w.winfo_exists():
                            w.deiconify()
                    except Exception:
                        pass
                self._auto_hidden = False
            elif not should_show and not self._auto_hidden:
                try:
                    self.root.withdraw()
                except Exception:
                    pass
                for w in (self._logs_window, self._paste_window, self._settings_window):
                    try:
                        if w is not None and w.winfo_exists():
                            w.withdraw()
                    except Exception:
                        pass
                self._auto_hidden = True
        except Exception:
            pass
        # Re-schedule.
        try:
            self.root.after(400, self._tick_foreground_visibility)
        except Exception:
            pass

    def _apply_noactivate(self, enable: bool) -> None:
        """Toggle WS_EX_NOACTIVATE + WS_EX_TRANSPARENT on the overlay.

        When enabled:
          * WS_EX_NOACTIVATE  -> clicking doesn't steal focus from Dota.
          * WS_EX_TRANSPARENT -> mouse clicks pass THROUGH the overlay to
            the window beneath (Dota), so hovering/clicking on the
            overlay behaves exactly like clicking the game underneath.
        When disabled, the overlay becomes clickable again so you can
        use the toolbar buttons, drag, etc.
        """
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            hwnd = self.root.winfo_id()
            GA_ROOT = 2
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enable:
                # WS_EX_TRANSPARENT requires WS_EX_LAYERED; Tk alpha
                # already sets LAYERED but we OR it in defensively.
                style |= WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
            else:
                style &= ~(WS_EX_NOACTIVATE | WS_EX_TRANSPARENT)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    # ---- right-click menu ----
    def _on_text_rightclick(self, event: tk.Event) -> None:
        menu = tk.Menu(self.root, bg="#1a1a1a", fg="#e0e0e0",
                       activebackground="#2a2a2a", activeforeground="#e0e0e0",
                       tearoff=False)
        menu.add_command(label="Copy all", command=lambda: (
            self.root.clipboard_clear(),
            self.root.clipboard_append(self.text.get("1.0", "end")),
            self.root.update(),
        ))
        menu.add_separator()
        menu.add_command(label="Select all", command=lambda: self.text.tag_add("sel", "1.0", "end"))
        menu.add_command(label="Clear", command=self.clear)
        menu.post(event.x_root, event.y_root)

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

    # ---- logs window ----
    def _on_logs_click(self) -> None:
        # Toggle: if already open, close it.
        if self._logs_window is not None and self._logs_window.winfo_exists():
            try:
                self._logs_window.destroy()
            except Exception:
                pass
            self._logs_window = None
            return

        from . import history

        win = tk.Toplevel(self.root)
        self._logs_window = win
        win.title("Translation history")
        # Apply dark title bar FIRST so DWM doesn't wipe our icon after.
        _set_dark_titlebar(win)

        def _apply_icon(w=win):
            try:
                if self._brand_icon is not None:
                    w.iconphoto(False, self._brand_icon)
                ico = _resource_path("gg.ico")
                if os.path.isfile(ico):
                    w.iconbitmap(ico)
            except Exception:
                pass
        _apply_icon()
        # Reapply after DWM finishes its dark-mode repaint so the icon
        # sticks in the title bar corner.
        win.after(150, _apply_icon)
        win.configure(bg="#0a0a0a")
        win.geometry("760x520")
        win.attributes("-topmost", True)

        bar = tk.Frame(win, bg="#151515", height=30)
        bar.pack(fill="x"); bar.pack_propagate(False)

        count_lbl = tk.Label(bar, text="", bg="#151515", fg="#888",
                             font=("Consolas", 9))
        count_lbl.pack(side="left", padx=8)

        def do_refresh():
            self._refresh_logs_text()

        def do_delete():
            from tkinter import messagebox
            if not messagebox.askyesno(
                "Delete history",
                "Delete all saved translations?\nThis cannot be undone.",
                parent=win,
            ):
                return
            history.clear()
            self._refresh_logs_text()

        def do_open_folder():
            try:
                import os
                os.startfile(str(history.file_path().parent))
            except Exception:
                pass

        for txt, cmd, fg in (
            ("🔄 Refresh",        do_refresh,     "#8fd8d8"),
            ("📂 Open folder",    do_open_folder, "#8fa8d8"),
            ("🗑 Delete history", do_delete,      "#ff6b6b"),
        ):
            tk.Button(
                bar, text=txt, command=cmd,
                bg="#1a1a1a", fg=fg, activebackground="#2a2a2a",
                activeforeground=fg, font=("Consolas", 9, "bold"),
                relief="flat", padx=8, cursor="hand2",
            ).pack(side="right", padx=2, pady=3)

        # Text area with scrollbar.
        frame = tk.Frame(win, bg="#0a0a0a")
        frame.pack(fill="both", expand=True)

        sb = ttk.Scrollbar(frame, orient="vertical",
                           style="Dark.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")

        txt = tk.Text(
            frame, bg="#0a0a0a", fg="#e0e0e0",
            insertbackground="#e0e0e0", font=("Consolas", 10),
            wrap="word", borderwidth=0, highlightthickness=0,
            padx=10, pady=6, state="disabled",
            yscrollcommand=sb.set,
        )
        txt.tag_configure("time", foreground="#555")
        txt.tag_configure("src",  foreground="#8a8a8a")
        txt.tag_configure("dst",  foreground="#7bd88f")
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)

        self._logs_text = txt
        self._logs_count = count_lbl

        win.bind("<Escape>", lambda _e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, "_logs_window", None), win.destroy()
        ))

        self._refresh_logs_text()

    def _refresh_logs_text(self) -> None:
        from . import history
        if self._logs_window is None or not self._logs_window.winfo_exists():
            return
        records = history.read_all()
        txt = self._logs_text
        try:
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            for r in records:
                t   = r.get("t", "")
                src = r.get("src", "")
                dst = r.get("dst", "")
                txt.insert("end", f"[{t}]\n", "time")
                txt.insert("end", f"  {src}\n", "src")
                txt.insert("end", f"  {dst}\n\n", "dst")
            txt.configure(state="disabled")
            txt.see("end")
            self._logs_count.config(text=f"{len(records)} entries")
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
            # Detect channel from [Tag] and apply color.
            tag = "all"  # default
            orig_lower = orig.lower()
            if "[allies]" in orig_lower:
                tag = "allies"
            elif "[spectator]" in orig_lower or "[observer]" in orig_lower:
                tag = "spectator"
            # Use channel tag for color; "src" only as fallback for [All].
            self.text.insert("end", f"{orig}\n", tag)
            self.text.insert("end", f"{trans}\n\n", "dst")
        self.text.see("end")
        self.text.configure(state="disabled")

    # ---- paste & copy ----
    @staticmethod
    def _detect_lang(text: str):
        """Return (src, tgt) based on Cyrillic ratio."""
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        lat = sum(1 for c in text if c.isalpha() and not ("\u0400" <= c <= "\u04FF"))
        is_ru = cyr >= 3 and (cyr + lat) > 0 and cyr / (cyr + lat) >= 0.4
        return ("ru", "en") if is_ru else ("en", "ru")

    # ---- Settings window ----
    def _on_settings_click(self) -> None:
        # Toggle: if already open, close it.
        if self._settings_window is not None and self._settings_window.winfo_exists():
            try:
                self._settings_window.destroy()
            except Exception:
                pass
            self._settings_window = None
            return

        win = tk.Toplevel(self.root)
        self._settings_window = win
        win.title("Settings — Hotkeys")
        _set_dark_titlebar(win)
        try:
            if self._brand_icon is not None:
                win.iconphoto(False, self._brand_icon)
            ico = _resource_path("gg.ico")
            if os.path.isfile(ico):
                win.iconbitmap(ico)
        except Exception:
            pass
        win.configure(bg="#0a0a0a")
        win.geometry("440x300")

        tk.Label(win, text="Click a hotkey to rebind it.",
                 bg="#0a0a0a", fg="#bbbbbb",
                 font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(10, 4))

        rows = tk.Frame(win, bg="#0a0a0a")
        rows.pack(fill="both", expand=True, padx=10, pady=4)

        row_widgets: dict[int, tk.Button] = {}

        def _refresh_row(aid):
            info = self._action_defs[aid]
            row_widgets[aid].configure(
                text=_combo_name(info["vk"], info["mods"])
            )

        def _start_capture(aid):
            info = self._action_defs[aid]
            btn = row_widgets[aid]
            btn.configure(text="Press keys...  (Esc = cancel)", bg="#5a2a2a")
            # Disable ALL global hotkeys while capturing so typing e.g.
            # Ctrl+Shift+P here doesn't toggle the Paste window.
            self._request_hotkey_unregister_all()

            def capture(event: tk.Event):
                keysym = event.keysym
                if keysym == "Escape":
                    _refresh_row(aid)
                    btn.configure(bg="#2a2a1a")
                    win.unbind("<Key>", bid)
                    # Restore the previous hotkeys.
                    self._request_hotkey_reregister()
                    return "break"
                # Ignore pure modifier keys.
                if keysym in ("Control_L", "Control_R", "Shift_L",
                              "Shift_R", "Alt_L", "Alt_R"):
                    return "break"
                vk = _KEYSYM_TO_VK.get(keysym)
                if vk is None and len(keysym) == 1 and keysym.isalnum():
                    vk = ord(keysym.upper())
                if vk is None:
                    btn.configure(text=f"Unsupported ({keysym})", bg="#5a2a2a")
                    return "break"
                mods = 0
                if event.state & 0x0004: mods |= 0x0002  # Ctrl
                if event.state & 0x0001: mods |= 0x0004  # Shift
                if event.state & 0x20000: mods |= 0x0001  # Alt
                # Commit
                info["vk"] = vk
                info["mods"] = mods
                _refresh_row(aid)
                btn.configure(bg="#2a2a1a")
                win.unbind("<Key>", bid)
                # Re-register all with Windows.
                self._request_hotkey_reregister()
                # Persist to config.json.
                self._persist_hotkeys()
                # Keep the legacy translate fields in sync.
                if aid == 1:
                    self._hotkey_vk = vk
                    self._hotkey_name = _combo_name(vk, mods)
                    self._hotkey_btn.configure(text=f"🎹 {self._hotkey_name}")
                    self._status.configure(
                        text=f"Press button or {self._hotkey_name} to translate",
                        fg="#555",
                    )
                return "break"

            bid = win.bind("<Key>", capture)

        for aid, info in self._action_defs.items():
            row = tk.Frame(rows, bg="#0a0a0a")
            row.pack(fill="x", pady=3)
            tk.Label(row, text=info["label"] + ":",
                     bg="#0a0a0a", fg="#e0e0e0",
                     font=("Consolas", 10), width=16, anchor="w"
                     ).pack(side="left")
            btn = tk.Button(
                row, text=_combo_name(info["vk"], info["mods"]),
                bg="#2a2a1a", fg="#d8d88f",
                activebackground="#3a3a2a", activeforeground="#ffffaa",
                font=("Consolas", 10, "bold"),
                relief="flat", padx=10, cursor="hand2",
                command=lambda a=aid: _start_capture(a),
            )
            btn.pack(side="left", padx=6)
            row_widgets[aid] = btn

        tk.Label(
            win,
            text="Tip: combos like Ctrl+Shift+L work globally, while\n"
                 "F-keys alone avoid conflicting with other apps.",
            bg="#0a0a0a", fg="#777",
            font=("Consolas", 8), justify="left"
        ).pack(anchor="w", padx=10, pady=(8, 10))

        win.bind("<Escape>", lambda _e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, "_settings_window", None), win.destroy()
        ))

    def _persist_hotkeys(self) -> None:
        """Write current action hotkeys to config.json under 'hotkeys'."""
        try:
            import json
            cfg_path = Path(_resource_path("config.json"))
            # Prefer the real config.json next to the exe / project root.
            here = Path(__file__).resolve().parent.parent
            candidate = here / "config.json"
            if candidate.is_file():
                cfg_path = candidate
            elif getattr(sys, "frozen", False):
                alt = Path(sys.executable).parent / "config.json"
                if alt.is_file():
                    cfg_path = alt
            if not cfg_path.is_file():
                return
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            hk = data.setdefault("hotkeys", {})
            for info in self._action_defs.values():
                hk[info["name"]] = {"vk": info["vk"], "mods": info["mods"]}
            cfg_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[settings] persist failed: {e}", flush=True)

    def _on_paste_click(self) -> None:
        if self._paste_window is not None and self._paste_window.winfo_exists():
            try:
                # Toggle close — or restore if currently minimized.
                if self._paste_window.state() in ("iconic", "withdrawn"):
                    self._paste_window.deiconify()
                    self._paste_window.lift()
                    self._paste_window.focus_force()
                    return
                self._paste_window.destroy()
            except Exception:
                pass
            self._paste_window = None
            return

        win = tk.Toplevel(self.root)
        self._paste_window = win
        win.title("Paste & Translate")
        # Apply dark title bar FIRST so DWM doesn't wipe our icon after.
        _set_dark_titlebar(win)

        def _apply_icon(w=win):
            try:
                if self._brand_icon is not None:
                    w.iconphoto(False, self._brand_icon)
                ico = _resource_path("gg.ico")
                if os.path.isfile(ico):
                    w.iconbitmap(ico)
            except Exception:
                pass
        _apply_icon()
        # Reapply after DWM finishes its dark-mode repaint so the icon
        # sticks in the title bar corner.
        win.after(150, _apply_icon)
        win.configure(bg="#0a0a0a")
        win.geometry("700x340")
        win.resizable(True, True)
        win.attributes("-topmost", True)

        # ── Top: input label + language badge ──────────────────────────────
        top_fr = tk.Frame(win, bg="#0a0a0a")
        top_fr.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top_fr, text="Input text (Russian ↔ English):",
                 bg="#0a0a0a", fg="#e0e0e0", font=("Consolas", 9)
                 ).pack(side="left")
        lang_badge = tk.Label(top_fr, text="", bg="#0a0a0a",
                              fg="#f2c94c", font=("Consolas", 9, "bold"))
        lang_badge.pack(side="right", padx=4)

        # ── Input text (fixed height 5 rows) ───────────────────────────────
        txt = tk.Text(win, bg="#111420", fg="#e0e0e0",
                      insertbackground="#e0e0e0", font=("Consolas", 10),
                      wrap="word", borderwidth=0, highlightthickness=1,
                      highlightbackground="#2a2a3a",
                      padx=6, pady=4, height=5)
        txt.pack(fill="x", padx=8, pady=(0, 4))
        txt.focus_set()

        # Pre-fill from clipboard.
        try:
            txt.insert("1.0", self.root.clipboard_get())
        except Exception:
            pass

        # ── Buttons ────────────────────────────────────────────────────────
        btn_fr = tk.Frame(win, bg="#151515")
        btn_fr.pack(fill="x", padx=8, pady=4)

        result_txt = tk.Text(win, bg="#111420", fg="#7bd88f",
                             insertbackground="#7bd88f", font=("Consolas", 10),
                             wrap="word", borderwidth=0, highlightthickness=1,
                             highlightbackground="#2a2a3a",
                             padx=6, pady=4, state="disabled", height=5)

        def _update_badge(*_):
            text = txt.get("1.0", "end").strip()
            if not text:
                lang_badge.config(text="")
                return
            src, tgt = self._detect_lang(text)
            arrow = "🇷🇺 → 🇬🇧" if src == "ru" else "🇬🇧 → 🇷🇺"
            lang_badge.config(text=arrow)

        # Live-translate debounce state
        self._live_after_id = None

        def _schedule_live(_evt=None):
            _update_badge()
            if self._live_after_id is not None:
                try: win.after_cancel(self._live_after_id)
                except Exception: pass
            self._live_after_id = win.after(450, lambda: do_translate(live=True))

        txt.bind("<KeyRelease>", _schedule_live)
        _update_badge()

        def do_translate(live: bool = False):
            text = txt.get("1.0", "end").strip()
            if not text:
                result_txt.configure(state="normal")
                result_txt.delete("1.0", "end")
                result_txt.configure(state="disabled")
                return
            from dota_ocr.translator import Translator
            src, tgt = self._detect_lang(text)
            _update_badge()
            def _worker(snapshot=text, src=src, tgt=tgt):
                try:
                    result = Translator().translate(snapshot, src=src, target_language=tgt)
                except Exception:
                    return
                if not result: return
                def _apply():
                    # Only apply if input hasn't changed (avoids flicker during live typing)
                    if live and txt.get("1.0", "end").strip() != snapshot:
                        return
                    result_txt.configure(state="normal")
                    result_txt.delete("1.0", "end")
                    result_txt.insert("1.0", result)
                    result_txt.configure(state="disabled")
                try: win.after(0, _apply)
                except Exception: pass
            threading.Thread(target=_worker, daemon=True).start()

        def do_fix_grammar():
            """Round-trip through the opposite language to clean up grammar."""
            text = txt.get("1.0", "end").strip()
            if not text:
                return
            from dota_ocr.translator import Translator
            src, tgt = self._detect_lang(text)
            status_lbl.config(text="Fixing grammar...", fg="#d8c88f")
            def _worker():
                try:
                    t = Translator()
                    pivot = t.translate(text, src=src, target_language=tgt)
                    fixed = t.translate(pivot, src=tgt, target_language=src) if pivot else None
                except Exception:
                    fixed = None
                def _apply():
                    if fixed:
                        txt.delete("1.0", "end")
                        txt.insert("1.0", fixed)
                        status_lbl.config(text="Grammar fixed ✓", fg="#7bd88f")
                        win.after(1500, lambda: status_lbl.config(text=""))
                        do_translate()
                    else:
                        status_lbl.config(text="Grammar fix failed", fg="#ff6b6b")
                try: win.after(0, _apply)
                except Exception: pass
            threading.Thread(target=_worker, daemon=True).start()

        def do_copy_result():
            text = result_txt.get("1.0", "end").strip()
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.root.update()
                status_lbl.config(text="Copied!", fg="#7bd88f")
                win.after(1500, lambda: status_lbl.config(text=""))

        def _ensure_translated():
            """If Result is empty, translate the input first. Returns the
            text to send, or '' if nothing to work with."""
            result = result_txt.get("1.0", "end").strip()
            if result:
                return result
            # Auto-translate before sending.
            do_translate()
            return result_txt.get("1.0", "end").strip()

        def do_send_team():
            text = _ensure_translated()
            if not text:
                status_lbl.config(text="Type something first", fg="#ff6b6b")
                return
            self._paste_to_dota_chat(text, all_chat=False)

        def do_send_all():
            text = _ensure_translated()
            if not text:
                status_lbl.config(text="Type something first", fg="#ff6b6b")
                return
            self._paste_to_dota_chat(text, all_chat=True)

        for label, cmd, bg, fg in (
            ("🔄 Translate",   do_translate, "#1a3a1a", "#7bd88f"),
            ("✨ Fix grammar", do_fix_grammar, "#3a2a1a", "#d8c88f"),
            ("📋 Copy result", do_copy_result, "#1a2a3a", "#8fa8d8"),
            ("👥 Team chat",   do_send_team, "#1a1a3a", "#8fa8ff"),
            ("🌐 All chat",    do_send_all,  "#2a1a1a", "#d88f8f"),
        ):
            tk.Button(btn_fr, text=label, command=cmd,
                      bg=bg, fg=fg, activebackground="#2a2a2a",
                      activeforeground=fg, font=("Consolas", 9, "bold"),
                      relief="flat", padx=8, cursor="hand2",
                      ).pack(side="left", padx=2, pady=3)

        # Status label on its OWN row below buttons so it's never clipped.
        status_lbl = tk.Label(win, text="", bg="#0a0a0a",
                              fg="#888", font=("Consolas", 9),
                              anchor="w")
        status_lbl.pack(fill="x", padx=10, pady=(0, 2))

        # ── Result label + text ────────────────────────────────────────────
        tk.Label(win, text="Result:", bg="#0a0a0a", fg="#e0e0e0",
                 font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(4, 2))

        result_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Enter in input triggers translate; Ctrl+Enter sends to Dota.
        txt.bind("<Return>",         lambda e: (do_translate(), "break"))
        txt.bind("<Control-Return>", lambda e: (do_translate(), win.after(300, do_send_team), "break"))
        txt.bind("<Shift-Return>",   lambda e: (do_translate(), win.after(300, do_send_all),  "break"))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, "_paste_window", None), win.destroy()
        ))

    # Map readable key names -> Virtual Key codes.
    _CHAT_KEY_VK = {
        "Return": 0x0D, "Enter": 0x0D,
        "y": 0x59, "Y": 0x59,
        "t": 0x54, "T": 0x54,
        "u": 0x55, "U": 0x55,
    }

    def _paste_to_dota_chat(self, text: str, all_chat: bool = False) -> None:
        """Focus Dota, open chat, paste translated text, send.

        all_chat=False → Enter        (team chat, Dota default)
        all_chat=True  → Shift+Enter  (all chat)
        """
        import time, threading

        VK_MENU   = 0x12   # Alt
        VK_SHIFT  = 0x10
        VK_CTRL   = 0x11
        VK_V      = 0x56
        VK_RETURN = 0x0D

        def _kd(vk):
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)

        def _ku(vk):
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)

        def _run():
            try:
                from ctypes import wintypes, c_size_t, c_void_p, c_wchar_p
                from dota_ocr.window import find_dota_hwnd
                hwnd = find_dota_hwnd()
                if not hwnd:
                    self.root.after(0, lambda: self.set_status("Dota 2 not found", "#ff4444"))
                    return

                u32 = ctypes.windll.user32
                k32 = ctypes.windll.kernel32

                # Declare proper argtypes/restypes so 64-bit handles don't
                # get truncated to 32-bit ints (root cause of the previous
                # 'access violation writing 0x0' crash).
                k32.GlobalAlloc.argtypes   = [wintypes.UINT, c_size_t]
                k32.GlobalAlloc.restype    = wintypes.HGLOBAL
                k32.GlobalLock.argtypes    = [wintypes.HGLOBAL]
                k32.GlobalLock.restype     = wintypes.LPVOID
                k32.GlobalUnlock.argtypes  = [wintypes.HGLOBAL]
                k32.GlobalUnlock.restype   = wintypes.BOOL
                u32.OpenClipboard.argtypes = [wintypes.HWND]
                u32.OpenClipboard.restype  = wintypes.BOOL
                u32.EmptyClipboard.restype = wintypes.BOOL
                u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
                u32.SetClipboardData.restype  = wintypes.HANDLE
                u32.CloseClipboard.restype    = wintypes.BOOL

                # ── 1) clipboard via raw Win32 (before any focus shuffle) ─────
                CF_UNICODETEXT = 13
                GMEM_MOVEABLE  = 0x0002
                encoded = text.encode("utf-16-le") + b"\x00\x00"  # null-terminated
                hMem = k32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                if not hMem:
                    raise RuntimeError("GlobalAlloc failed")
                pMem = k32.GlobalLock(hMem)
                if not pMem:
                    raise RuntimeError("GlobalLock failed")
                ctypes.memmove(pMem, encoded, len(encoded))
                k32.GlobalUnlock(hMem)
                if not u32.OpenClipboard(0):
                    raise RuntimeError("OpenClipboard failed")
                try:
                    u32.EmptyClipboard()
                    u32.SetClipboardData(CF_UNICODETEXT, hMem)
                finally:
                    u32.CloseClipboard()
                print(f"[send] clipboard set ({len(text)} chars)", flush=True)

                # ── 2) grant foreground privilege via fake Alt tap ────────────
                # Windows blocks SetForegroundWindow from background procs.
                # Sending ANY keybd_event attaches our thread to the input
                # queue for one tick, which bypasses that restriction.
                _kd(VK_MENU); _ku(VK_MENU)
                time.sleep(0.02)

                # ── 3) focus Dota (must be windowed/borderless, NOT exclusive fullscreen) ─
                fg_ok = u32.SetForegroundWindow(hwnd)
                u32.BringWindowToTop(hwnd)
                u32.SetFocus(hwnd)
                time.sleep(0.45)
                cur_fg = u32.GetForegroundWindow()
                print(f"[send] SetForegroundWindow={fg_ok}, fg_hwnd={cur_fg}, dota={hwnd}", flush=True)
                if cur_fg != hwnd:
                    self.root.after(0, lambda: self.set_status(
                        "Focus failed — use borderless window", "#ff4444"))
                    return

                # ── 4) open chat ──────────────────────────────────────────────
                # Team:  Enter    |   All: Shift+Enter
                if all_chat:
                    _kd(VK_SHIFT)
                _kd(VK_RETURN); time.sleep(0.05); _ku(VK_RETURN)
                if all_chat:
                    _ku(VK_SHIFT)
                time.sleep(0.45)

                # ── 5) Ctrl+V ─────────────────────────────────────────────────
                _kd(VK_CTRL); _kd(VK_V); time.sleep(0.05)
                _ku(VK_V);    _ku(VK_CTRL)
                time.sleep(0.25)

                # ── 6) Enter to send ──────────────────────────────────────────
                _kd(VK_RETURN); time.sleep(0.05); _ku(VK_RETURN)

                label = "All chat" if all_chat else "Team chat"
                self.root.after(0, lambda: self.set_status(f"Sent to {label} ✓", "#7bd88f"))
                print(f"[send] done ({label})", flush=True)

            except Exception as e:
                print(f"[send] ERROR: {e}", flush=True)
                self.root.after(0, lambda err=e: self.set_status(f"Send failed: {err}", "#ff4444"))

        threading.Thread(target=_run, daemon=True).start()

    def mainloop(self) -> None:
        self.root.mainloop()
