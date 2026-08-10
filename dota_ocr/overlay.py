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

from dota_ocr import sizes as _sz

# Marker written into a message's `original` field to flag it as coming
# from voice chat rather than text chat.  Using a sentinel in the existing
# (original, translated) tuple keeps the message-queue shape unchanged,
# which matters because clear()/_render()/_autosize() all consume it.
VOICE_PREFIX = "🔊"


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


def _attach_tooltip(widget, text, delay_ms: int = 400) -> None:
    """Lightweight hover-tooltip. `text` may be a str or a zero-arg
    callable — when callable, it is evaluated at show-time so the
    tooltip can reflect live state (e.g. the current hotkey binding)."""
    state = {"after": None, "tip": None}

    def _show():
        state["after"] = None
        try:
            msg = text() if callable(text) else text
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            try:
                tip.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(
                tip, text=msg,
                bg="#1f1f1f", fg="#e0e0e0",
                font=("Consolas", 8),
                padx=6, pady=2,
                borderwidth=1, relief="solid",
            ).pack()
            state["tip"] = tip
        except Exception:
            state["tip"] = None

    def _enter(_e=None):
        _hide()
        try:
            state["after"] = widget.after(delay_ms, _show)
        except Exception:
            pass

    def _hide(_e=None):
        if state["after"] is not None:
            try:
                widget.after_cancel(state["after"])
            except Exception:
                pass
            state["after"] = None
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except Exception:
                pass
            state["tip"] = None

    widget.bind("<Enter>", _enter, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<ButtonPress>", _hide, add="+")


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
        x: int = _sz.MAIN_X,
        y: int = _sz.MAIN_Y,
        width: int = _sz.MAIN_WIDTH,
        height: int = _sz.MAIN_HEIGHT,  # initial; grows to fit messages
        alpha: float = 0.55,
        font_size: int = 11,
        max_messages: int = 50,  # Show many messages, user can scroll
        hotkey_vk: int = 0x76,  # F7
        on_recalibrate=None,  # callback(new_relative_bbox: dict) when user resizes
        on_hotkey_changed=None,  # callback(new_vk: int, name: str) on rebind
        on_voice_toggle=None,  # callback(enabled: bool) -> bool (actual state)
        cfg: dict = None,
    ):
        self._cfg = cfg or {}
        self._on_voice_toggle = on_voice_toggle
        self.max_messages = max_messages
        self._msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._trigger_event = threading.Event()
        self._messages: list[tuple[str, str]] = []
        self._alpha = max(0.15, min(1.0, alpha))
        self._on_recalibrate = on_recalibrate
        self._on_hotkey_changed = on_hotkey_changed
        self._hotkey_name = _vk_to_name(hotkey_vk)

        self.root = tk.Tk()
        self.root.title("Ninjito Translate")
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
            text=f"📷 Translate ({self._hotkey_name})",
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
        _attach_tooltip(self._btn,
                        lambda: f"Translate — capture & translate chat now ({self._hk_name('translate')})")

        self._resize_btn = tk.Button(
            bar,
            text="📐",
            bg="#1a1a3a",
            fg="#8fa8d8",
            activebackground="#2a2a5a",
            activeforeground="#aaccff",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_resize_click,
        )
        self._resize_btn.pack(side="left", padx=2, pady=2)
        _attach_tooltip(self._resize_btn,
                        lambda: "Resize — draw a new chat capture region")

        self._lock_btn = tk.Button(
            bar,
            text="🔓",
            bg="#2a1a1a",
            fg="#d88f8f",
            activebackground="#5a2a2a",
            activeforeground="#ffaaaa",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_lock_toggle,
        )
        self._lock_btn.pack(side="left", padx=2, pady=2)
        self._locked = False
        _attach_tooltip(self._lock_btn,
                        lambda: f"Lock — make overlay click-through ({self._hk_name('lock')})")

        # Hotkey rebind UI moved to Settings window.  Keep an invisible
        # placeholder so legacy code that references self._hotkey_btn
        # (e.g. _on_hotkey_rebind, _finish_rebind) doesn't explode.
        self._hotkey_btn = tk.Button(bar, text="", borderwidth=0)
        self._rebinding = False

        self._logs_btn = tk.Button(
            bar,
            text="📜",
            bg="#1a2a2a",
            fg="#8fd8d8",
            activebackground="#2a5a5a",
            activeforeground="#aaffff",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_logs_click,
        )
        self._logs_btn.pack(side="left", padx=2, pady=2)
        self._logs_window: tk.Toplevel | None = None
        _attach_tooltip(self._logs_btn,
                        lambda: f"Logs — translation history ({self._hk_name('logs')})")

        self._paste_btn = tk.Button(
            bar,
            text="📋",
            bg="#2a1a2a",
            fg="#d8a8d8",
            activebackground="#5a2a5a",
            activeforeground="#ffaaff",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_paste_click,
        )
        self._paste_btn.pack(side="left", padx=2, pady=2)
        self._paste_window: tk.Toplevel | None = None
        self._paste_input = None
        self._paste_send_team = None
        self._paste_send_all = None
        self._paste_stop_btn = None
        _attach_tooltip(self._paste_btn,
                        lambda: f"Paste — type & translate to Russian ({self._hk_name('paste')})")

        self._voice_btn = tk.Button(
            bar,
            text="🎙",
            bg="#2a2a2a",
            fg="#777777",
            activebackground="#3a3a3a",
            activeforeground="#7bd8b0",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_voice_toggle_click,
        )
        self._voice_btn.pack(side="left", padx=2, pady=2)
        _attach_tooltip(self._voice_btn,
                        lambda: f"Voice — translate Russian voice chat "
                                f"({self._hk_name('voice_toggle')})")

        self._settings_btn = tk.Button(
            bar,
            text="⚙️",
            bg="#2a2a2a",
            fg="#cccccc",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            font=("Consolas", 10, "bold"),
            relief="flat",
            padx=6,
            cursor="hand2",
            command=self._on_settings_click,
        )
        self._settings_btn.pack(side="left", padx=2, pady=2)
        self._settings_window: tk.Toplevel | None = None
        _attach_tooltip(self._settings_btn,
                        lambda: f"Settings — hotkeys, capture, theme ({self._hk_name('settings')})")

        # --- Spam / burst-send state (lives on self so it survives the
        # Paste window closing).  The main-bar Stop button is hidden until
        # a burst is running, then shows "⏹ Stop (N)" with the live count.
        self._spam = {
            "active": False, "after": None, "remaining": 0,
            "count_var": None,  # IntVar from paste window (if open)
            "status_lbl": None, # status label in paste window (if open)
            "win": None,        # paste win ref (if open)
        }
        self._spam_stop_btn = tk.Button(
            bar, text="⏹ Stop",
            bg="#3a1a1a", fg="#ff8888",
            activebackground="#5a2a2a", activeforeground="#ffaaaa",
            font=("Consolas", 9, "bold"),
            relief="flat", padx=8, cursor="hand2",
            command=self._spam_stop,
        )
        # not packed yet — shown only while a burst is active
        _attach_tooltip(self._spam_stop_btn,
                        lambda: f"Stop the in-progress chat-spam burst ({self._hk_name('spam_stop')})")

        # Close (X) button — pinned to the far right of the button bar.
        # Disabled while the app is locked so click-through users can't
        # accidentally kill the overlay.
        self._close_btn = tk.Button(
            bar,
            text="✕",
            bg="#2a1010", fg="#ff8888",
            activebackground="#5a1010", activeforeground="#ff6666",
            font=("Consolas", 10, "bold"),
            relief="flat", padx=8, cursor="hand2",
            command=self._close,
        )
        self._close_btn.pack(side="right", padx=(2, 4), pady=2)
        _attach_tooltip(self._close_btn, "Close the app")

        # --- Status label (inline, to the right of the buttons) ---
        self._status = tk.Label(
            bar,
            text=f"Press button or {self._hotkey_name} to translate",
            bg="#151515", fg="#888",
            font=("Consolas", 8),
            anchor="w",
        )
        self._status.pack(side="left", fill="x", expand=True, padx=8)

        # --- Text display with scrollbar ---
        text_frame = tk.Frame(self.root, bg="#0a0a0a")
        text_frame.pack(fill="both", expand=True)
        self._text_frame = text_frame

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

        self._font_size = font_size
        self.text = tk.Text(
            text_frame,
            bg="#0a0a0a",
            fg="#e0e0e0",
            insertbackground="#e0e0e0",
            font=("Consolas", font_size),
            # stored for theme code (transparent mode needs bold override)
            # – see _apply_theme
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
        self.text.tag_configure("voice", foreground="#ffb86c")        # orange
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
        # Escape no longer closes the app — only the ✕ button does.
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

        # Auto-OCR / auto-translate-on-chat / theme init from config.
        self._auto_ocr_after = None
        self._chat_watch_after = None
        self._chat_key_was_down = False
        try:
            self._apply_theme(str((self._cfg or {}).get("theme", "dark")))
        except Exception:
            pass
        try:
            self._apply_auto_ocr()
            self._apply_auto_chat_watch()
        except Exception:
            pass

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
            6: {"name": "team_send", "label": "Send to team chat",
                "vk": 0x54, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+T
                "handler": lambda: self.root.after(0, self._hotkey_send_team)},
            7: {"name": "all_send", "label": "Send to all chat",
                "vk": 0x59, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+Y
                "handler": lambda: self.root.after(0, self._hotkey_send_all)},
            8: {"name": "spam_stop", "label": "Stop chat-spam burst",
                "vk": 0x51, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+Q
                "handler": lambda: self.root.after(0, self._spam_stop)},
            9: {"name": "voice_toggle", "label": "Voice translation on/off",
                "vk": 0x56, "mods": MOD_CTRL | MOD_SHIFT,   # Ctrl+Shift+V
                "handler": lambda: self.root.after(0, self._on_voice_toggle_click)},
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
        # Labels built earlier used the raw constructor default; refresh
        # them now that config overrides have been applied so the status
        # text and Translate button match the real bound hotkey.
        try:
            self._btn.configure(text=f"📷 Translate ({self._hotkey_name})")
        except Exception:
            pass
        try:
            self._status.configure(
                text=f"Press button or {self._hotkey_name} to translate"
            )
        except Exception:
            pass
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_listener, daemon=True
        )
        self._hotkey_thread.start()

        # Hotkeys are registered system-wide by RegisterHotKey, which
        # means Windows eats the combo *before* any other app sees it.
        # To stop us from hijacking Ctrl+Shift+T in Brave etc., we
        # dynamically unregister when the foreground app is not Dota
        # (or one of our own windows) and re-register when it is.
        self._hotkeys_active = True
        try:
            self.root.after(300, self._poll_hotkey_gate)
        except Exception:
            pass

        # --- First-run bootstrap -------------------------------------------
        # On the very first launch the user's config.json has no "hotkeys"
        # dict and may be missing auto-mode/theme keys.  Write the current
        # effective defaults back to disk so (a) Settings always reflects
        # what's really registered, and (b) the file is ready to be
        # hand-edited if desired.
        try:
            self._bootstrap_defaults()
        except Exception as e:
            print(f"[cfg] bootstrap defaults failed: {e}", flush=True)

        # Re-sync auto-mode UI now that cfg is guaranteed to be hydrated.
        try:
            self._update_translate_visibility()
        except Exception:
            pass
        try:
            self._update_voice_button()
        except Exception:
            pass

    def _hk_name(self, action_name: str) -> str:
        """Return the current human-readable combo for `action_name`
        (e.g. 'translate' -> 'F7', 'paste' -> 'Ctrl+Shift+P'). Used by
        dynamic tooltips so they update after a rebind."""
        try:
            for info in self._action_defs.values():
                if info.get("name") == action_name:
                    return _combo_name(info["vk"], info["mods"])
        except Exception:
            pass
        return "?"

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
                        # ── Dota-or-our-app gate ──────────────────────────
                        # Only fire when Dota 2 OR one of our own windows is
                        # the foreground app.  This prevents hotkeys like
                        # Ctrl+Shift+T / Ctrl+Shift+P from hijacking Chrome
                        # or any other app the user is in.
                        try:
                            _fg = user32.GetForegroundWindow() or 0
                            _relevant = False
                            # Check our own windows first (fast path).
                            GA_ROOT = 2
                            user32.GetAncestor.restype = ctypes.c_void_p
                            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                            for _w in (self.root, self._logs_window,
                                       self._paste_window, self._settings_window):
                                if _w is None:
                                    continue
                                try:
                                    _h = int(_w.winfo_id())
                                    if (user32.GetAncestor(_h, GA_ROOT) or _h) == _fg:
                                        _relevant = True
                                        break
                                except Exception:
                                    pass
                            if not _relevant:
                                # Check if Dota 2 is fg.
                                from dota_ocr.window import find_dota_hwnd as _fdh
                                _dota = _fdh()
                                if _dota and _fg == _dota:
                                    _relevant = True
                            if not _relevant:
                                continue  # drop — user is in another app
                        except Exception:
                            pass  # if check fails, allow through
                        # Source-level per-action cooldown: drops WM_HOTKEY
                        # auto-repeat AND rapid mashing that would otherwise
                        # queue up N callbacks on Tk's after() queue and fire
                        # them all at once when the mainloop unblocks.
                        import time as _time
                        now_ms = _time.monotonic() * 1000.0
                        if not hasattr(self, "_hk_last_fire"):
                            self._hk_last_fire = {}
                        if now_ms - self._hk_last_fire.get(aid, 0.0) < 250:
                            continue
                        # Also drain any WM_HOTKEY with the same aid that
                        # arrived while we were blocked — those are mashes
                        # that must not replay later.
                        try:
                            peek = ctypes.wintypes.MSG()
                            PM_REMOVE = 0x0001
                            while user32.PeekMessageW(
                                ctypes.byref(peek), None,
                                0x0312, 0x0312, PM_REMOVE
                            ):
                                if int(peek.wParam) != aid:
                                    # Re-post non-matching hotkey so we
                                    # still handle it next iteration.
                                    user32.PostThreadMessageW(
                                        self._hotkey_thread_id,
                                        0x0312, peek.wParam, peek.lParam)
                                    break
                        except Exception:
                            pass
                        self._hk_last_fire[aid] = now_ms
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

    def _poll_hotkey_gate(self) -> None:
        """Register/unregister global hotkeys based on foreground window.

        Runs on the Tk thread every ~250ms. If Dota or one of our own
        windows is foreground we keep hotkeys registered; otherwise we
        unregister them so Brave/Chrome/etc receive their native combos
        (Ctrl+Shift+T to reopen tab, etc.).

        While the user is rebinding a hotkey we suspend gating so we
        don't fight with the Settings capture path.
        """
        try:
            if getattr(self, "_closing", False):
                return
            if getattr(self, "_rebinding", False):
                # Leave state as-is; capture path manages registration.
                return
            import ctypes as _ct
            user32 = _ct.windll.user32
            fg = user32.GetForegroundWindow() or 0
            relevant = False
            GA_ROOT = 2
            user32.GetAncestor.restype = _ct.c_void_p
            user32.GetAncestor.argtypes = [_ct.c_void_p, _ct.c_uint]
            for _w in (self.root, self._logs_window,
                       self._paste_window, self._settings_window):
                if _w is None:
                    continue
                try:
                    _h = int(_w.winfo_id())
                    if (user32.GetAncestor(_h, GA_ROOT) or _h) == fg:
                        relevant = True
                        break
                except Exception:
                    pass
            if not relevant:
                try:
                    from dota_ocr.window import find_dota_hwnd as _fdh
                    _dota = _fdh()
                    if _dota and fg == _dota:
                        relevant = True
                except Exception:
                    pass

            if relevant and not self._hotkeys_active:
                self._request_hotkey_reregister()
                self._hotkeys_active = True
            elif not relevant and self._hotkeys_active:
                self._request_hotkey_unregister_all()
                self._hotkeys_active = False
        except Exception:
            pass
        finally:
            try:
                if not getattr(self, "_closing", False):
                    self.root.after(250, self._poll_hotkey_gate)
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
        # Drop the temporary rebind-cancel Escape binding.  We no longer
        # re-install a global Escape=close handler — the ✕ button is the
        # only way to close the app now.
        try:
            self.root.unbind("<Escape>", self._rebind_esc_id)
        except Exception:
            pass
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
    # ---- voice translation ----
    def _on_voice_toggle_click(self) -> None:
        """Flip voice translation on/off and persist the choice.

        The callback owns the listener and returns the state it actually
        reached — starting can fail (no loopback device, model download
        blocked), and the UI must show what really happened rather than
        what was requested.
        """
        want = not bool((self._cfg or {}).get("voice", {}).get("enabled", False))
        actual = want
        if self._on_voice_toggle is not None:
            try:
                actual = bool(self._on_voice_toggle(want))
            except Exception as e:
                print(f"[voice] toggle failed: {e}", flush=True)
                actual = False
        self._set_voice_cfg("enabled", actual)
        self._update_voice_button()
        if actual:
            self.set_status("Voice: starting...", "#ffa500")
        else:
            self.set_status("Voice: off" if want == actual
                            else "Voice: failed to start", "#888")

    def _update_voice_button(self) -> None:
        """Repaint the mic button to match the current voice state."""
        try:
            on = bool((self._cfg or {}).get("voice", {}).get("enabled", False))
            self._voice_btn.configure(
                text="🎙" if on else "🎙",
                bg="#1a3a2a" if on else "#2a2a2a",
                fg="#7bd8b0" if on else "#777777",
            )
        except Exception:
            pass

    def _set_voice_cfg(self, key: str, value) -> None:
        """Update one key inside the nested cfg['voice'] block on disk."""
        if self._cfg is None:
            self._cfg = {}
        block = dict(self._cfg.get("voice") or {})
        block[key] = value
        self._cfg["voice"] = block
        self._set_cfg("voice", block)

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
        """Update the status line from any thread.

        The OCR worker and the voice capture/process threads all call
        this. Tk widgets may only be touched from the thread running
        mainloop — doing it elsewhere can raise "main thread is not in
        main loop", corrupt Tk's state, or deadlock the UI — so calls
        from other threads are marshalled through root.after.
        """
        def _apply():
            try:
                self._status.configure(text=text, fg=color)
            except Exception:
                pass

        if threading.current_thread() is threading.main_thread():
            _apply()
            return
        try:
            self.root.after(0, _apply)
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
        # Hard-kill the whole process so daemon threads (PaddleOCR, translator
        # HTTP sessions, hotkey listener) don't keep running in the background.
        # os._exit skips atexit handlers and __del__ finalizers intentionally —
        # we want an instant, clean stop.
        import os as _os
        _os._exit(0)

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
        # Same WM_HOTKEY auto-repeat debounce as the aux-window toggles.
        import time as _time
        now = _time.monotonic() * 1000.0
        if not hasattr(self, "_toggle_last_ms"):
            self._toggle_last_ms = {}
        if now - self._toggle_last_ms.get("_lock", 0.0) < 180:
            return
        self._toggle_last_ms["_lock"] = now
        self._locked = not self._locked
        if self._locked:
            self._lock_btn.configure(text="🔒", bg="#1a2a1a", fg="#8fd88f")
            self.text.configure(cursor="arrow")
            # Make the window non-focusable (WS_EX_NOACTIVATE) so clicking
            # on it never steals focus from Dota 2.
            self._apply_noactivate(True)
            try:
                self._close_btn.configure(state="disabled")
            except Exception:
                pass
            self.set_status("Locked — clicks pass through. Ctrl+Shift+L to unlock", "#7bd88f")
        else:
            self._lock_btn.configure(text="🔓", bg="#2a1a1a", fg="#d88f8f")
            self.text.configure(cursor="fleur")
            self._apply_noactivate(False)
            try:
                self._close_btn.configure(state="normal")
            except Exception:
                pass
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

    def push_voice(self, russian: str, english: str) -> None:
        """Push a translated voice-chat line.

        Called from the voice worker thread — safe because the queue is
        the only thing touched here, and Tk work happens in _drain().
        """
        self._msg_queue.put((f"{VOICE_PREFIX} {russian}",
                             f"{VOICE_PREFIX} {english}"))

    @staticmethod
    def _is_voice(original: str) -> bool:
        return original.lstrip().startswith(VOICE_PREFIX)

    def clear(self) -> None:
        """Clear shown chat translations before each F7 batch.

        Voice lines survive: they arrive on their own schedule and aren't
        part of the OCR batch, so wiping them here would make spoken
        translations vanish the instant the user pressed Translate.
        """
        self._messages = [m for m in self._messages if self._is_voice(m[0])]
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except Exception:
            pass
        # Repaint whatever voice lines were kept.
        if self._messages:
            try:
                self._render()
                return
            except Exception:
                pass
        # Shrink overlay back to the default height now that it's empty.
        try:
            self._autosize_to_messages(visible=_sz.AUTOSIZE_MESSAGES)
        except Exception:
            pass

    # ---- logs window ----
    def _toggle_aux_window(self, attr_name: str, *, _debounce_ms: int = 180) -> bool:
        """Debounce rapid hotkey auto-repeat: ignore toggle calls that
        arrive within _debounce_ms of the previous one for the same
        window. Without this, holding the hotkey briefly fires WM_HOTKEY
        multiple times — the first press closes, the next reopens, and
        the user sees "hotkey doesn't close the window"."""
        import time as _time
        now = _time.monotonic() * 1000.0
        if not hasattr(self, "_toggle_last_ms"):
            self._toggle_last_ms = {}
        last = self._toggle_last_ms.get(attr_name, 0.0)
        if now - last < _debounce_ms:
            # Swallow this call — it's a repeat, not a real press.
            return True
        self._toggle_last_ms[attr_name] = now
        return self._toggle_aux_window_impl(attr_name)

    def _toggle_aux_window_impl(self, attr_name: str) -> bool:
        """Shared toggle logic for Logs/Settings/Paste.

        Returns True if the toggle *closed or restored* the existing
        window and the caller should bail out; False if the window
        needs to be created fresh.

        Handles every edge-case we've hit:
          * winfo_exists() false positives after race with auto-hide
          * state() raising (Tk interpreter already torn down)
          * window withdrawn by auto-hide → restore + focus, don't
            destroy
          * stale reference after manual X close
        """
        w = getattr(self, attr_name, None)
        if w is None:
            return False
        # Is it still a live Tk widget?
        try:
            alive = bool(w.winfo_exists())
        except Exception:
            alive = False
        if not alive:
            setattr(self, attr_name, None)
            return False
        # Mapped? (visible vs withdrawn / iconic)
        try:
            st = w.state()
        except Exception:
            st = "normal"
        if st in ("withdrawn", "iconic"):
            # Auto-hidden — restore instead of destroying.
            # Clear _auto_hidden so the foreground ticker doesn't
            # immediately re-withdraw us while Dota is still fg.
            self._auto_hidden = False

            # Deiconify + lift immediately on Tk thread (fast, no stall).
            try:
                w.deiconify()
                w.lift()
                try: w.attributes("-topmost", True)
                except Exception: pass
                # Restore siblings too so the whole UI comes back together.
                try: self.root.deiconify()
                except Exception: pass
                for sib in (self._logs_window, self._paste_window,
                            self._settings_window):
                    if sib is None or sib is w:
                        continue
                    try:
                        if sib.winfo_exists() and sib.state() in ("withdrawn", "iconic"):
                            sib.deiconify()
                    except Exception:
                        pass
            except Exception:
                try: w.destroy()
                except Exception: pass
                setattr(self, attr_name, None)
                return False

            # Win32 foreground grab runs off-thread so Tk mainloop keeps
            # pumping — SetForegroundWindow / focus_force can stall the
            # main thread if Windows refuses to yield focus (e.g. Dota
            # is still the fg app), which would freeze all hotkey handling.
            def _grab(wref=w):
                try:
                    import ctypes as _ct
                    u32 = _ct.windll.user32
                    u32.keybd_event(0x12, 0, 0, 0)       # Alt down
                    u32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
                    try:
                        hwnd = u32.GetParent(wref.winfo_id()) or wref.winfo_id()
                        u32.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    self.root.after(0, lambda: _finish(wref))
                except Exception:
                    pass
            def _finish(wref=w):
                try:
                    if wref.winfo_exists():
                        wref.focus_force()
                        wref.attributes("-topmost", False)
                except Exception:
                    pass
            threading.Thread(target=_grab, daemon=True).start()
            return True
        # Normal visible toggle → close.
        try: w.destroy()
        except Exception: pass
        setattr(self, attr_name, None)
        return True

    def _on_logs_click(self) -> None:
        if self._toggle_aux_window("_logs_window"):
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
        win.geometry(f"{_sz.LOGS_WIDTH}x{_sz.LOGS_HEIGHT}")
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

        _l_closing = {"done": False}
        def _logs_close(from_destroy: bool = False):
            if _l_closing["done"]:
                return
            _l_closing["done"] = True
            self._logs_window = None
            if not from_destroy:
                try: win.destroy()
                except Exception: pass
        win.bind("<Escape>",  lambda _e: _logs_close(False))
        win.bind("<Destroy>", lambda e: (_logs_close(True) if e.widget is win else None))
        win.protocol("WM_DELETE_WINDOW", lambda: _logs_close(False))

        # Foreground grab off-thread (same pattern as paste/settings).
        def _logs_grab():
            try:
                import ctypes as _ct
                u32 = _ct.windll.user32
                u32.keybd_event(0x12, 0, 0, 0)
                u32.keybd_event(0x12, 0, 0x0002, 0)
                try:
                    hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
                    u32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                self.root.after(0, lambda: (
                    win.lift(), win.focus_force(),
                    win.after(300, lambda: win.attributes("-topmost", False)
                              if win.winfo_exists() else None)
                ) if win.winfo_exists() else None)
            except Exception:
                pass
        win.after(50, lambda: threading.Thread(
            target=_logs_grab, daemon=True).start())

        self._refresh_logs_text()

    def _refresh_logs_text(self) -> None:
        from . import history
        if self._logs_window is None or not self._logs_window.winfo_exists():
            return
        records = history.read_all()
        # Remember original indices so pin/delete ops still reference the
        # right record after sorting.
        indexed = list(enumerate(records))
        # Pinned first (stable by recency within each group).
        indexed.sort(key=lambda p: (not p[1].get("pinned"), p[0]))

        txt = self._logs_text
        try:
            txt.configure(state="normal")
            # Remove any old inline buttons / tags.
            for name in list(txt.window_names()):
                try: txt.window_create  # noop — ensure method exists
                except Exception: pass
            txt.delete("1.0", "end")
            for orig_idx, r in indexed:
                t   = r.get("t", "")
                src = r.get("src", "")
                dst = r.get("dst", "")
                pinned = bool(r.get("pinned"))
                # Row header with star + resend + delete buttons.
                star_char = "★" if pinned else "☆"
                btn_star = tk.Button(
                    txt, text=star_char,
                    bg="#1a1a1a" if not pinned else "#3a2a0a",
                    fg="#ffd84a" if pinned else "#888",
                    activebackground="#2a2a2a", activeforeground="#ffd84a",
                    font=("Consolas", 10, "bold"), relief="flat",
                    borderwidth=0, padx=4, cursor="hand2",
                    command=lambda i=orig_idx: self._on_log_pin(i),
                )
                btn_resend = tk.Button(
                    txt, text="⇪ Send",
                    bg="#1a2a1a", fg="#7bd88f",
                    activebackground="#2a4a2a", activeforeground="#aaffaa",
                    font=("Consolas", 8, "bold"), relief="flat",
                    borderwidth=0, padx=4, cursor="hand2",
                    command=lambda d=dst: self._on_log_resend(d),
                )
                btn_del = tk.Button(
                    txt, text="✕",
                    bg="#2a1a1a", fg="#d88f8f",
                    activebackground="#4a2a2a", activeforeground="#ffaaaa",
                    font=("Consolas", 9, "bold"), relief="flat",
                    borderwidth=0, padx=4, cursor="hand2",
                    command=lambda i=orig_idx: self._on_log_delete(i),
                )
                txt.window_create("end", window=btn_star)
                txt.window_create("end", window=btn_resend)
                txt.window_create("end", window=btn_del)
                txt.insert("end", f"  [{t}]\n", "time")
                txt.insert("end", f"  {src}\n", "src")
                txt.insert("end", f"  {dst}\n\n", "dst")
            txt.configure(state="disabled")
            txt.see("1.0")  # show pinned at top
            pinned_n = sum(1 for r in records if r.get("pinned"))
            self._logs_count.config(
                text=f"{len(records)} entries  •  {pinned_n} pinned"
            )
        except Exception as e:
            print(f"[logs] refresh error: {e}", flush=True)

    def _on_log_pin(self, index: int) -> None:
        from . import history
        history.toggle_pin(index)
        self._refresh_logs_text()

    def _on_log_delete(self, index: int) -> None:
        from . import history
        history.delete_at(index)
        self._refresh_logs_text()

    def _on_log_resend(self, text: str) -> None:
        """One-click re-send: paste this translation into Dota team chat."""
        if not text:
            return
        self._paste_to_dota_chat(text, all_chat=False)

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
        # Only keep the last 5 translations on screen — no source line,
        # just the translated text, channel-colored.
        for orig, trans in self._messages[-5:]:
            tag = "all"
            orig_lower = orig.lower()
            if self._is_voice(orig):
                tag = "voice"
            elif "[allies]" in orig_lower:
                tag = "allies"
            elif "[spectator]" in orig_lower or "[observer]" in orig_lower:
                tag = "spectator"
            self.text.insert("end", f"{trans}\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")
        # Auto-grow the overlay so at least the last 5 messages fit
        # without scrolling — crucial while locked (click-through), when
        # the user can't scroll at all.
        try:
            self._autosize_to_messages(visible=_sz.AUTOSIZE_MESSAGES)
        except Exception:
            pass

    def _autosize_to_messages(self, visible: int = _sz.AUTOSIZE_MESSAGES) -> None:
        """Resize the overlay height so the last `visible` messages
        are fully on-screen.  Width and X/Y position are preserved.

        When there are no messages, shrink back to the configured
        default height (MAIN_HEIGHT) so the empty overlay isn't a
        giant strip of dead space."""
        if not self._messages:
            try:
                cur_geo = self.root.geometry()
                size = cur_geo.split("+", 1)[0]
                w_str, _ = size.split("x")
                w = int(w_str)
                parts = cur_geo.split("+")
                x = int(parts[1]) if len(parts) > 1 else _sz.MAIN_X
                y = int(parts[2]) if len(parts) > 2 else _sz.MAIN_Y
                self.root.geometry(f"{w}x{_sz.MAIN_HEIGHT}+{x}+{y}")
            except Exception:
                pass
            return
        msgs = self._messages[-visible:]
        # Each message renders as 3 lines (orig + trans + blank).
        # Use the text widget's actual wrapped-line count for accuracy.
        try:
            total_display_lines = int(
                self.text.count("1.0", "end", "displaylines")[0]
            )
        except Exception:
            total_display_lines = len(msgs) * 3

        # Line height in px from the widget's font.
        try:
            font = self.text.cget("font")
            # tkinter.font.Font supports metrics("linespace")
            import tkinter.font as tkfont
            if isinstance(font, str):
                f = tkfont.nametofont(font) if font in tkfont.names(self.root) else tkfont.Font(root=self.root, font=font)
            else:
                f = font
            line_h = f.metrics("linespace")
        except Exception:
            line_h = 16

        bar_h = 34  # top bar height + padding
        desired = bar_h + total_display_lines * line_h + 8
        # Clamp: don't make the window absurdly tall.
        screen_h = self.root.winfo_screenheight()
        desired = max(_sz.AUTOSIZE_MIN_HEIGHT,
                      min(desired, _sz.AUTOSIZE_MAX_HEIGHT,
                          int(screen_h * 0.6)))

        cur_geo = self.root.geometry()  # "WxH+X+Y"
        try:
            size, x, y = cur_geo.split("+", 1)[0], *cur_geo.split("+")[1:]
            w_str, _ = size.split("x")
            w = int(w_str)
            x, y = int(x), int(y)
        except Exception:
            w, x, y = 640, 50, 50
        self.root.geometry(f"{w}x{desired}+{x}+{y}")

    # ---- paste & copy ----
    @staticmethod
    def _detect_lang(text: str):
        """Return (src, tgt) based on Cyrillic ratio."""
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        lat = sum(1 for c in text if c.isalpha() and not ("\u0400" <= c <= "\u04FF"))
        is_ru = cyr >= 3 and (cyr + lat) > 0 and cyr / (cyr + lat) >= 0.4
        return ("ru", "en") if is_ru else ("en", "ru")

    # ---- Paste-window send hotkeys ----
    def _hotkey_send_team(self) -> None:
        if self._paste_send_team is None:
            # Window isn't open — nothing to send.  Flash a hint.
            self.set_status("Open Paste first, then send", "#ffa500")
            return
        try:
            self._paste_send_team()
        except Exception:
            pass

    def _hotkey_send_all(self) -> None:
        if self._paste_send_all is None:
            self.set_status("Open Paste first, then send", "#ffa500")
            return
        try:
            self._paste_send_all()
        except Exception:
            pass

    # ---- Settings window ----
    def _on_settings_click(self) -> None:
        # Toggle: if already open (even if withdrawn by auto-hide), toggle/restore.
        if self._toggle_aux_window("_settings_window"):
            return

        win = tk.Toplevel(self.root)
        self._settings_window = win
        win.title("Settings")
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
        win.geometry(f"{_sz.SETTINGS_WIDTH}x{_sz.SETTINGS_HEIGHT}")
        win.resizable(False, False)
        win.attributes("-topmost", True)   # float above Dota on open

        # ── Tabs ──────────────────────────────────────────────────────
        style = ttk.Style(win)
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("TNotebook", background="#0a0a0a", borderwidth=0)
        style.configure("TNotebook.Tab",
                        background="#1a1a1a", foreground="#bbb",
                        padding=[12, 6], font=("Consolas", 9, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", "#2a2a1a")],
                  foreground=[("selected", "#ffd84a")])
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        tab_hk = tk.Frame(nb, bg="#0a0a0a")
        tab_cap = tk.Frame(nb, bg="#0a0a0a")
        tab_voice = tk.Frame(nb, bg="#0a0a0a")
        tab_thm = tk.Frame(nb, bg="#0a0a0a")
        nb.add(tab_hk,  text="Hotkeys")
        nb.add(tab_cap, text="Capture")
        nb.add(tab_voice, text="Voice")
        nb.add(tab_thm, text="Theme")

        # ── Hotkeys tab ───────────────────────────────────────────────
        tk.Label(tab_hk, text="Click a hotkey to rebind it.",
                 bg="#0a0a0a", fg="#bbbbbb",
                 font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(10, 4))

        rows = tk.Frame(tab_hk, bg="#0a0a0a")
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
                # Keep the Translate button label + status hint in sync.
                if aid == 1:
                    self._hotkey_vk = vk
                    self._hotkey_name = _combo_name(vk, mods)
                    try:
                        self._btn.configure(
                            text=f"📷 Translate ({self._hotkey_name})"
                        )
                    except Exception:
                        pass
                    self._status.configure(
                        text=f"Press button or {self._hotkey_name} to translate",
                        fg="#555",
                    )
                return "break"

            bid = win.bind("<Key>", capture)

        for aid, info in self._action_defs.items():
            row = tk.Frame(rows, bg="#0a0a0a")
            row.pack(fill="x", pady=3)
            # Label hugs the left edge and expands so the hotkey button
            # always sits flush against the right edge of the tab.
            tk.Label(row, text=info["label"] + ":",
                     bg="#0a0a0a", fg="#e0e0e0",
                     font=("Consolas", 10), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            btn = tk.Button(
                row, text=_combo_name(info["vk"], info["mods"]),
                bg="#2a2a1a", fg="#d8d88f",
                activebackground="#3a3a2a", activeforeground="#ffffaa",
                font=("Consolas", 10, "bold"),
                relief="flat", padx=10, cursor="hand2",
                command=lambda a=aid: _start_capture(a),
            )
            btn.pack(side="right", padx=(6, 0))
            row_widgets[aid] = btn

        tk.Label(
            tab_hk,
            text="Tip: combos like Ctrl+Shift+L work globally, while\n"
                 "F-keys alone avoid conflicting with other apps.",
            bg="#0a0a0a", fg="#777",
            font=("Consolas", 8), justify="left"
        ).pack(anchor="w", padx=10, pady=(8, 10))

        # ── Capture tab: auto-OCR loop + auto-translate on chat key ──
        cfg = self._cfg or {}
        auto_ocr_var = tk.BooleanVar(value=bool(cfg.get("auto_ocr_enabled", False)))
        auto_int_var = tk.IntVar(value=int(cfg.get("auto_ocr_interval_sec", 5)))
        auto_chat_var = tk.BooleanVar(value=bool(cfg.get("auto_translate_on_chat", False)))

        tk.Label(tab_cap, text="Auto-OCR loop",
                 bg="#0a0a0a", fg="#ffd84a",
                 font=("Consolas", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(14, 2))
        tk.Label(tab_cap,
                 text="When enabled, the app automatically runs OCR on\n"
                      "the chat region every N seconds, with no hotkey press.",
                 bg="#0a0a0a", fg="#888",
                 font=("Consolas", 8), justify="left"
                 ).pack(anchor="w", padx=12)
        row_ocr = tk.Frame(tab_cap, bg="#0a0a0a")
        row_ocr.pack(fill="x", padx=12, pady=(6, 2))
        tk.Checkbutton(
            row_ocr, text="Enable auto-OCR", variable=auto_ocr_var,
            bg="#0a0a0a", fg="#e0e0e0", selectcolor="#2a2a1a",
            activebackground="#0a0a0a", activeforeground="#ffd84a",
            font=("Consolas", 9), borderwidth=0,
            command=lambda: (self._set_cfg("auto_ocr_enabled", auto_ocr_var.get()),
                             self._apply_auto_ocr()),
        ).pack(side="left")
        tk.Label(row_ocr, text="Interval (sec):",
                 bg="#0a0a0a", fg="#bbb",
                 font=("Consolas", 9)).pack(side="left", padx=(12, 2))
        sp = tk.Spinbox(
            row_ocr, from_=2, to=120, width=4, textvariable=auto_int_var,
            bg="#1a1a1a", fg="#e0e0e0", insertbackground="#e0e0e0",
            font=("Consolas", 9), relief="flat",
            command=lambda: (self._set_cfg("auto_ocr_interval_sec", auto_int_var.get()),
                             self._apply_auto_ocr()),
        )
        sp.pack(side="left")

        tk.Label(tab_cap, text="Auto-translate when chat opens",
                 bg="#0a0a0a", fg="#ffd84a",
                 font=("Consolas", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(16, 2))
        tk.Label(tab_cap,
                 text="Triggers OCR automatically the moment you press\n"
                      "the Dota chat key (Enter), no F7 needed.",
                 bg="#0a0a0a", fg="#888",
                 font=("Consolas", 8), justify="left"
                 ).pack(anchor="w", padx=12)
        tk.Checkbutton(
            tab_cap, text="Enable auto-translate on chat open",
            variable=auto_chat_var,
            bg="#0a0a0a", fg="#e0e0e0", selectcolor="#2a2a1a",
            activebackground="#0a0a0a", activeforeground="#ffd84a",
            font=("Consolas", 9), borderwidth=0,
            command=lambda: (self._set_cfg("auto_translate_on_chat", auto_chat_var.get()),
                             self._apply_auto_chat_watch()),
        ).pack(anchor="w", padx=12, pady=(6, 2))

        # ── Voice tab: loopback capture + Whisper transcription ───────
        vblock = dict(cfg.get("voice") or {})
        voice_on_var = tk.BooleanVar(value=bool(vblock.get("enabled", False)))
        model_var = tk.StringVar(value=str(vblock.get("model_size", "small")))

        tk.Label(tab_voice, text="Russian voice chat",
                 bg="#0a0a0a", fg="#ffd84a",
                 font=("Consolas", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(tab_voice,
                 text="Listens to your speakers (not your mic), transcribes\n"
                      "Russian speech and shows it translated in the overlay.",
                 bg="#0a0a0a", fg="#888",
                 font=("Consolas", 8), justify="left"
                 ).pack(anchor="w", padx=12)

        tk.Checkbutton(
            tab_voice, text="Enable voice translation",
            variable=voice_on_var,
            bg="#0a0a0a", fg="#e0e0e0", selectcolor="#2a2a1a",
            activebackground="#0a0a0a", activeforeground="#ffd84a",
            font=("Consolas", 9), borderwidth=0,
            command=lambda: self._settings_voice_enable(voice_on_var),
        ).pack(anchor="w", padx=12, pady=(6, 2))

        # --- Output device ---
        tk.Label(tab_voice, text="Listen to output device:",
                 bg="#0a0a0a", fg="#bbb",
                 font=("Consolas", 9)).pack(anchor="w", padx=12, pady=(10, 2))

        try:
            from dota_ocr import voice as _voice
            devices = _voice.list_loopback_devices()
        except Exception as e:
            print(f"[voice] settings device list failed: {e}", flush=True)
            devices = []

        # Strip the " [Loopback]" suffix for display — it's an artifact of
        # how WASAPI exposes the device, not something the user chose.
        dev_labels = ["Default output (auto)"] + [
            d["name"].replace(" [Loopback]", "") for d in devices
        ]
        cur_name = str(vblock.get("device_name", "") or "")
        cur_label = dev_labels[0]
        for d, label in zip(devices, dev_labels[1:]):
            if d["name"] == cur_name:
                cur_label = label
                break
        dev_var = tk.StringVar(value=cur_label)

        def _on_dev_change(_evt=None):
            picked = dev_var.get()
            if picked == dev_labels[0]:
                self._set_voice_cfg("device_name", "")
                self._set_voice_cfg("device_index", None)
            else:
                for dd, lab in zip(devices, dev_labels[1:]):
                    if lab == picked:
                        self._set_voice_cfg("device_name", dd["name"])
                        self._set_voice_cfg("device_index", dd["index"])
                        break
            self._settings_voice_restart()

        dev_combo = ttk.Combobox(tab_voice, values=dev_labels,
                                 textvariable=dev_var, state="readonly",
                                 font=("Consolas", 9), width=38)
        dev_combo.pack(anchor="w", padx=12)
        dev_combo.bind("<<ComboboxSelected>>", _on_dev_change)
        if not devices:
            tk.Label(tab_voice,
                     text="No loopback device found — voice capture "
                          "is unavailable.",
                     bg="#0a0a0a", fg="#ff8844",
                     font=("Consolas", 8)).pack(anchor="w", padx=12, pady=(2, 0))

        # --- Model size ---
        tk.Label(tab_voice, text="Recognition model:",
                 bg="#0a0a0a", fg="#bbb",
                 font=("Consolas", 9)).pack(anchor="w", padx=12, pady=(10, 2))
        row_model = tk.Frame(tab_voice, bg="#0a0a0a")
        row_model.pack(fill="x", padx=12)
        for val, desc in (("base", "fastest"),
                          ("small", "balanced"),
                          ("medium", "most accurate")):
            tk.Radiobutton(
                row_model, text=f"{val} ({desc})", variable=model_var, value=val,
                bg="#0a0a0a", fg="#e0e0e0", selectcolor="#ffd84a",
                activebackground="#0a0a0a", activeforeground="#ffd84a",
                font=("Consolas", 8), borderwidth=0, indicatoron=True,
                command=lambda v=val: (self._set_voice_cfg("model_size", v),
                                       self._settings_voice_restart()),
            ).pack(anchor="w")

        tk.Label(tab_voice,
                 text="Changing the model downloads it once (~150MB-1.5GB).\n"
                      "The first line after enabling may take a few seconds.",
                 bg="#0a0a0a", fg="#777",
                 font=("Consolas", 8), justify="left"
                 ).pack(anchor="w", padx=12, pady=(8, 4))

        # ── Theme tab ─────────────────────────────────────────────────
        theme_var = tk.StringVar(value=str(cfg.get("theme", "dark")))
        tk.Label(tab_thm, text="Overlay theme",
                 bg="#0a0a0a", fg="#ffd84a",
                 font=("Consolas", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(14, 4))
        for val, label, desc in (
            ("dark",        "Dark",        "Solid dark background (default)."),
            ("light",       "Light",       "Light background with dark text."),
            ("transparent", "Transparent", "Text floats with no background panel."),
        ):
            r = tk.Frame(tab_thm, bg="#0a0a0a")
            r.pack(fill="x", padx=12, pady=2)
            tk.Radiobutton(
                r, text=label, variable=theme_var, value=val,
                bg="#0a0a0a", fg="#e0e0e0",
                selectcolor="#ffd84a",          # filled dot colour when selected
                activebackground="#0a0a0a", activeforeground="#ffd84a",
                font=("Consolas", 10, "bold"), borderwidth=0, width=12, anchor="w",
                indicatoron=True,
                command=lambda v=val: (self._set_cfg("theme", v),
                                       self._apply_theme(v)),
            ).pack(side="left")
            tk.Label(r, text=desc, bg="#0a0a0a", fg="#777",
                     font=("Consolas", 8)).pack(side="left", padx=6)

        _s_closing = {"done": False}
        def _settings_close(from_destroy: bool = False):
            if _s_closing["done"]:
                return
            _s_closing["done"] = True
            self._settings_window = None
            if not from_destroy:
                try: win.destroy()
                except Exception: pass
        win.bind("<Escape>",  lambda _e: _settings_close(False))
        win.bind("<Destroy>", lambda e: (_settings_close(True) if e.widget is win else None))
        win.protocol("WM_DELETE_WINDOW", lambda: _settings_close(False))

        # Bring the window to the foreground without stalling the Tk
        # mainloop (same pattern used by the Paste window).
        def _settings_grab():
            try:
                import ctypes as _ct
                u32 = _ct.windll.user32
                u32.keybd_event(0x12, 0, 0, 0)       # Alt down
                u32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
                try:
                    hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
                    u32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                self.root.after(0, lambda: _settings_finish())
            except Exception:
                pass
        def _settings_finish():
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    win.after(300, lambda: win.attributes("-topmost", False)
                              if win.winfo_exists() else None)
            except Exception:
                pass
        win.after(50, lambda: threading.Thread(
            target=_settings_grab, daemon=True).start())

    def _settings_voice_enable(self, var) -> None:
        """Settings-tab checkbox handler.

        Routes through the same toggle path as the hotkey/button so the
        listener, the config and the mic button can't drift apart, then
        syncs the checkbox back in case the start attempt failed.
        """
        want = bool(var.get())
        cur = bool((self._cfg or {}).get("voice", {}).get("enabled", False))
        if want != cur:
            self._on_voice_toggle_click()
        try:
            var.set(bool((self._cfg or {}).get("voice", {}).get("enabled", False)))
        except Exception:
            pass

    def _settings_voice_restart(self) -> None:
        """Re-apply voice settings that need the listener rebuilt.

        The device and model are bound when the listener starts, so a
        change only takes effect after a stop/start cycle — and only if
        voice is actually on right now.
        """
        if not bool((self._cfg or {}).get("voice", {}).get("enabled", False)):
            return
        if self._on_voice_toggle is None:
            return
        try:
            self._on_voice_toggle(False)
            ok = bool(self._on_voice_toggle(True))
            self._set_voice_cfg("enabled", ok)
            self._update_voice_button()
        except Exception as e:
            print(f"[voice] restart failed: {e}", flush=True)

    def _bootstrap_defaults(self) -> None:
        """Make sure every setting the app reads has a value in cfg +
        on disk.  Runs once at startup after _action_defs is built.

        Without this, a brand-new config.json has no `hotkeys` block and
        no `auto_*`/`theme` keys, so the Settings tab just shows code
        defaults that don't match anything persisted — confusing after a
        restart.  This writes the current effective state back so what
        you see is what's loaded.
        """
        if self._cfg is None:
            self._cfg = {}
        import json
        here = Path(__file__).resolve().parent.parent
        cfg_path = here / "config.json"
        if getattr(sys, "frozen", False):
            alt = Path(sys.executable).parent / "config.json"
            if alt.is_file():
                cfg_path = alt

        data: dict = {}
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        dirty = False

        # 1. Hotkeys: build {name: {vk, mods}} from current action_defs.
        hk_block = dict(data.get("hotkeys") or {})
        for info in self._action_defs.values():
            name = info["name"]
            cur = hk_block.get(name) or {}
            want = {"vk": int(info["vk"]), "mods": int(info["mods"])}
            if cur != want:
                hk_block[name] = want
                dirty = True
        if dirty or "hotkeys" not in data:
            data["hotkeys"] = hk_block
            dirty = True

        # 2. Scalar settings with sensible defaults.
        scalar_defaults = {
            "theme": str((self._cfg or {}).get("theme", "dark")),
            "auto_ocr_enabled": bool((self._cfg or {}).get("auto_ocr_enabled", False)),
            "auto_ocr_interval_sec": int((self._cfg or {}).get("auto_ocr_interval_sec", 5)),
            "auto_translate_on_chat": bool((self._cfg or {}).get("auto_translate_on_chat", False)),
            "dota_chat_key": str((self._cfg or {}).get("dota_chat_key", "Return")),
        }
        for k, v in scalar_defaults.items():
            if k not in data:
                data[k] = v
                dirty = True
            # Also mirror into in-memory cfg so other code paths see it.
            self._cfg.setdefault(k, data[k])

        # 3. Voice block. Merged key-by-key so upgrading an existing
        #    install picks up new voice settings without resetting the
        #    ones the user already chose.
        voice_defaults = {
            "enabled": False,          # opt-in: never load a model unasked
            "device_name": "",         # "" = default output device
            "device_index": None,
            "model_size": "small",
            "compute_device": "auto",  # falls back to CPU if CUDA is absent
            "compute_type": "int8",
            "lang_prob_min": 0.6,      # reject non-Russian below this
            "min_avg_logprob": -1.0,   # reject low-confidence audio
            # Whisper's language confidence falls off on short audio, so
            # utterances at or under this length skip the lang_prob gate
            # and are judged on Cyrillic + no_speech_prob instead. Raise
            # it to catch more short calls, lower it if noise gets in.
            "short_utterance_sec": 2.0,
            "max_no_speech_prob": 0.6,
            "use_dota_prompt": True,
        }
        cur_voice = dict(data.get("voice") or {})
        for k, v in voice_defaults.items():
            if k not in cur_voice:
                cur_voice[k] = v
                dirty = True
        if data.get("voice") != cur_voice:
            data["voice"] = cur_voice
            dirty = True
        self._cfg["voice"] = dict(cur_voice)

        if dirty:
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                cfg_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(f"[cfg] first-run defaults written to {cfg_path}",
                      flush=True)
            except Exception as e:
                print(f"[cfg] write defaults failed: {e}", flush=True)

    def _set_cfg(self, key: str, value) -> None:
        """Update a top-level config key in memory and on disk."""
        if self._cfg is None:
            self._cfg = {}
        self._cfg[key] = value
        try:
            import json
            here = Path(__file__).resolve().parent.parent
            cfg_path = here / "config.json"
            if getattr(sys, "frozen", False):
                alt = Path(sys.executable).parent / "config.json"
                if alt.is_file():
                    cfg_path = alt
            data = {}
            if cfg_path.is_file():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            data[key] = value
            cfg_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[cfg] persist {key} failed: {e}", flush=True)

    # ---- Theme ----
    def _apply_theme(self, theme: str) -> None:
        """Repaint the main overlay with the chosen theme."""
        try:
            if theme == "light":
                bg, text_bg, fg = "#f4f4f4", "#ffffff", "#222"
                self.root.attributes("-alpha", max(0.75, self._alpha))
                tag_colors = {"dst": "#1a5a1a", "allies": "#1a4a8a",
                              "all": "#222222", "spectator": "#555555",
                              "src": "#555555", "voice": "#a05000"}
            elif theme == "transparent":
                # Text floats directly over Dota.  Use black bold text —
                # coloured text (green/blue) disappears against grass /
                # sky / creep textures.  Black + bold reads on almost
                # any background.
                bg, text_bg, fg = "#010101", "#010101", "#000000"
                self.root.attributes("-alpha", self._alpha)
                try:
                    # Make the exact bg color invisible via Tk's
                    # -transparentcolor attribute (Windows-only).
                    self.root.attributes("-transparentcolor", "#010101")
                except Exception:
                    pass
                tag_colors = {"dst": "#000000", "allies": "#000000",
                              "all": "#000000", "spectator": "#000000",
                              "src": "#000000", "voice": "#000000"}
            else:  # dark (default)
                bg, text_bg, fg = "#0a0a0a", "#0a0a0a", "#e0e0e0"
                try:
                    self.root.attributes("-transparentcolor", "")
                except Exception:
                    pass
                self.root.attributes("-alpha", self._alpha)
                tag_colors = {"dst": "#7bd88f", "allies": "#6bb8ff",
                              "all": "#e0e0e0", "spectator": "#aaaaaa",
                              "src": "#8a8a8a", "voice": "#ffb86c"}
            try:
                self.root.configure(bg=bg)
                self.text.configure(bg=text_bg, fg=fg, insertbackground=fg)
                # Scrollbar trough + the frame that holds it must also use
                # the transparent key color, otherwise a dark bar shows on
                # the right edge in transparent mode.
                try:
                    if getattr(self, "_text_frame", None) is not None:
                        self._text_frame.configure(bg=text_bg)
                except Exception:
                    pass
                try:
                    style = ttk.Style(self.root)
                    style.configure(
                        "Dark.Vertical.TScrollbar",
                        background=text_bg,
                        troughcolor=text_bg,
                        bordercolor=text_bg,
                        arrowcolor=text_bg,
                        darkcolor=text_bg,
                        lightcolor=text_bg,
                    )
                except Exception:
                    pass
                for tag, color in tag_colors.items():
                    try:
                        if theme == "transparent":
                            self.text.tag_configure(
                                tag, foreground=color,
                                font=("Consolas", self._font_size, "bold"),
                            )
                        else:
                            self.text.tag_configure(
                                tag, foreground=color, font="",
                            )
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    # ---- Auto-OCR loop ----
    def _update_translate_visibility(self) -> None:
        """Hide the Translate button + status placeholder when an
        automatic translation mode is active (auto-OCR loop or
        auto-translate-on-chat-open).  Manual F7 still works either way;
        this just removes the redundant UI.

        Note: we can't use `winfo_ismapped()` to detect current packing
        because it returns False before the Tk mainloop starts (first
        run bug).  Track state in `self._btn_visible` instead.
        """
        cfg = self._cfg or {}
        auto = bool(cfg.get("auto_ocr_enabled")) or bool(cfg.get("auto_translate_on_chat"))
        if not hasattr(self, "_btn_visible"):
            self._btn_visible = True  # button is packed by default in __init__
        try:
            if auto:
                if self._btn_visible:
                    try: self._btn.pack_forget()
                    except Exception: pass
                    self._btn_visible = False
                cur = self._status.cget("text")
                if cur.startswith("Press button") or cur.strip() == "":
                    self._status.configure(text="Auto mode — translating automatically")
            else:
                if not self._btn_visible:
                    try:
                        self._btn.pack(side="left", padx=4, pady=2,
                                       before=self._resize_btn)
                    except Exception:
                        try:
                            self._btn.pack(side="left", padx=4, pady=2)
                        except Exception:
                            pass
                    self._btn_visible = True
                cur = self._status.cget("text")
                if cur.startswith("Auto mode"):
                    self._status.configure(
                        text=f"Press button or {self._hotkey_name} to translate"
                    )
        except Exception:
            pass

    def _apply_auto_ocr(self) -> None:
        """(Re)start/stop the auto-OCR timer based on current config."""
        # Cancel any pending scheduled trigger.
        if getattr(self, "_auto_ocr_after", None) is not None:
            try: self.root.after_cancel(self._auto_ocr_after)
            except Exception: pass
            self._auto_ocr_after = None
        cfg = self._cfg or {}
        self._update_translate_visibility()
        if not cfg.get("auto_ocr_enabled"):
            return
        interval = max(2, int(cfg.get("auto_ocr_interval_sec", 5)))
        def _tick():
            try:
                # Only fire when Dota is focused (don't waste cycles
                # OCRing the desktop / browser).
                if self._auto_hidden:
                    pass
                else:
                    self._trigger_event.set()
            except Exception:
                pass
            self._auto_ocr_after = self.root.after(interval * 1000, _tick)
        self._auto_ocr_after = self.root.after(interval * 1000, _tick)

    # ---- Auto-translate on Dota chat-key press ----
    def _apply_auto_chat_watch(self) -> None:
        """(Re)start/stop a poll that watches for the Dota chat key
        (Return/Enter by default) being pressed while Dota is focused."""
        if getattr(self, "_chat_watch_after", None) is not None:
            try: self.root.after_cancel(self._chat_watch_after)
            except Exception: pass
            self._chat_watch_after = None
        cfg = self._cfg or {}
        self._update_translate_visibility()
        if not cfg.get("auto_translate_on_chat"):
            self._chat_key_was_down = False
            return
        # Resolve the chat key VK from config string ("Return", "Y", etc.)
        key_name = str(cfg.get("dota_chat_key", "Return"))
        vk = _KEYSYM_TO_VK.get(key_name)
        if vk is None and len(key_name) == 1 and key_name.isalnum():
            vk = ord(key_name.upper())
        if vk is None:
            vk = 0x0D  # VK_RETURN
        self._chat_watch_vk = vk
        self._chat_key_was_down = False

        def _poll():
            try:
                # Only listen while Dota is the foreground window, so
                # pressing Enter in the browser or paste window doesn't
                # trigger a bogus OCR capture.
                if not self._auto_hidden:
                    u32 = ctypes.windll.user32
                    state = u32.GetAsyncKeyState(self._chat_watch_vk)
                    pressed = bool(state & 0x8000)
                    if pressed and not self._chat_key_was_down:
                        # Dota just opened the chat box.  Give it a
                        # tick to render, then trigger OCR.
                        self.root.after(350, lambda: self._trigger_event.set())
                    self._chat_key_was_down = pressed
                else:
                    self._chat_key_was_down = False
            except Exception:
                pass
            self._chat_watch_after = self.root.after(60, _poll)
        self._chat_watch_after = self.root.after(60, _poll)

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

    # ---------- chat-spam burst (survives Paste window close) ----------
    def _spam_update_ui(self) -> None:
        """Show/hide + relabel the main-bar Stop button.

        We can't rely on winfo_ismapped() (it lies before mainloop and
        during rapid show/hide cycles), so pack_forget / pack are called
        unconditionally — both are idempotent.
        """
        try:
            if self._spam.get("active") and self._spam.get("remaining", 0) > 0:
                try:
                    self._spam_stop_btn.configure(
                        text=f"⏹ Stop ({self._spam['remaining']})"
                    )
                except Exception:
                    pass
                try:
                    self._spam_stop_btn.pack(
                        side="left", padx=(6, 2), pady=2, before=self._close_btn
                    )
                except Exception:
                    pass
            else:
                try: self._spam_stop_btn.pack_forget()
                except Exception: pass
        except Exception:
            pass

    def _spam_stop(self) -> None:
        self._spam["active"] = False
        if self._spam.get("after") is not None:
            try: self.root.after_cancel(self._spam["after"])
            except Exception: pass
            self._spam["after"] = None
        self._spam["remaining"] = 0
        # If the paste window is still open, reset its Stop button too.
        win = self._spam.get("win")
        if win is not None:
            try:
                if win.winfo_exists() and getattr(self, "_paste_stop_btn", None):
                    self._paste_stop_btn.configure(state="disabled")
            except Exception:
                pass
        self._spam["count_var"] = None
        self._spam["status_lbl"] = None
        self._spam["win"] = None
        self._spam_update_ui()

    def _spam_send(self, text: str, all_chat: bool,
                   count: int,
                   count_var: tk.IntVar | None = None,
                   status_lbl: tk.Label | None = None,
                   win: tk.Toplevel | None = None) -> None:
        """Send `text` into Dota chat `count` times with a ~900ms gap.
        Safe to call while Paste window is closed."""
        if self._spam["active"]:
            if status_lbl is not None:
                try:
                    status_lbl.config(text="Already sending — press Stop first",
                                      fg="#ff6b6b")
                except Exception: pass
            return
        n = max(1, int(count))
        self._spam.update({
            "active": True, "remaining": n,
            "count_var": count_var, "status_lbl": status_lbl,
            "win": win,
        })
        if getattr(self, "_paste_stop_btn", None) is not None:
            try: self._paste_stop_btn.configure(state="normal")
            except Exception: pass
        self._spam_update_ui()

        def _one():
            if not self._spam["active"] or self._spam["remaining"] <= 0:
                self._spam_stop()
                return
            try:
                self._paste_to_dota_chat(text, all_chat=all_chat)
            except Exception as e:
                if self._spam.get("status_lbl") is not None:
                    try:
                        self._spam["status_lbl"].config(
                            text=f"Send failed: {e}", fg="#ff6b6b")
                    except Exception: pass
                self._spam_stop()
                return
            self._spam["remaining"] -= 1
            cv = self._spam.get("count_var")
            if cv is not None:
                try: cv.set(self._spam["remaining"])
                except Exception: pass
            self._spam_update_ui()
            if self._spam["remaining"] <= 0:
                sl = self._spam.get("status_lbl")
                self._spam_stop()
                if sl is not None:
                    try:
                        sl.config(text="Done ✓", fg="#7bd88f")
                        self.root.after(1500,
                                        lambda: sl.config(text=""))
                    except Exception: pass
                return
            self._spam["after"] = self.root.after(900, _one)

        _one()

    def _on_paste_click(self) -> None:
        # Unified toggle handles destroy/restore across auto-hide edge cases.
        if self._toggle_aux_window("_paste_window"):
            # If we just restored (window still alive), refocus the input.
            w = self._paste_window
            if w is not None:
                try:
                    if self._paste_input is not None:
                        self._paste_input.focus_set()
                except Exception:
                    pass
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
        win.geometry(f"{_sz.PASTE_WIDTH}x{_sz.PASTE_HEIGHT}")
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

        team_count_var = tk.IntVar(value=1)
        all_count_var = tk.IntVar(value=1)

        def do_send_team():
            text = _ensure_translated()
            if not text:
                status_lbl.config(text="Type something first", fg="#ff6b6b")
                return
            try: n = max(1, int(team_count_var.get()))
            except Exception: n = 1
            self._spam_send(text, all_chat=False, count=n,
                            count_var=team_count_var,
                            status_lbl=status_lbl, win=win)

        def do_send_all():
            text = _ensure_translated()
            if not text:
                status_lbl.config(text="Type something first", fg="#ff6b6b")
                return
            try: n = max(1, int(all_count_var.get()))
            except Exception: n = 1
            self._spam_send(text, all_chat=True, count=n,
                            count_var=all_count_var,
                            status_lbl=status_lbl, win=win)

        def _mk_btn(parent, label, cmd, bg, fg):
            return tk.Button(parent, text=label, command=cmd,
                             bg=bg, fg=fg, activebackground="#2a2a2a",
                             activeforeground=fg, font=("Consolas", 9, "bold"),
                             relief="flat", padx=8, cursor="hand2")

        def _mk_count(parent, var: tk.IntVar):
            sp = tk.Spinbox(parent, from_=1, to=99, width=3,
                            textvariable=var,
                            bg="#1a1a1a", fg="#e0e0e0",
                            buttonbackground="#2a2a2a",
                            insertbackground="#e0e0e0",
                            relief="flat", font=("Consolas", 9, "bold"))
            return sp

        _mk_btn(btn_fr, "🔄 Translate",   do_translate,    "#1a3a1a", "#7bd88f"
                ).pack(side="left", padx=2, pady=3)
        _mk_btn(btn_fr, "✨ Fix grammar", do_fix_grammar,  "#3a2a1a", "#d8c88f"
                ).pack(side="left", padx=2, pady=3)
        _mk_btn(btn_fr, "📋 Copy result", do_copy_result,  "#1a2a3a", "#8fa8d8"
                ).pack(side="left", padx=2, pady=3)

        _mk_btn(btn_fr, "👥 Team chat", do_send_team, "#1a1a3a", "#8fa8ff"
                ).pack(side="left", padx=(6, 0), pady=3)
        _mk_count(btn_fr, team_count_var).pack(side="left", padx=(2, 0), pady=3)

        _mk_btn(btn_fr, "🌐 All chat", do_send_all, "#2a1a1a", "#d88f8f"
                ).pack(side="left", padx=(6, 0), pady=3)
        _mk_count(btn_fr, all_count_var).pack(side="left", padx=(2, 0), pady=3)

        stop_btn = _mk_btn(btn_fr, "⏹ Stop", self._spam_stop, "#3a1a1a", "#ff8888")
        stop_btn.pack(side="left", padx=(8, 2), pady=3)
        stop_btn.configure(state="normal" if self._spam["active"] else "disabled")
        self._paste_stop_btn = stop_btn

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
        _closing_flag = {"done": False}
        def _on_close(from_destroy: bool = False):
            # Re-entrant guard: <Destroy> AND WM_DELETE_WINDOW can both
            # fire for the same window; also _toggle_aux_window may have
            # already called destroy(), after which <Destroy> bubbles
            # back here. Never call win.destroy() more than once.
            if _closing_flag["done"]:
                return
            _closing_flag["done"] = True
            try:
                if self._spam.get("win") is win:
                    self._spam["win"] = None
                    self._spam["count_var"] = None
                    self._spam["status_lbl"] = None
            except Exception:
                pass
            self._paste_stop_btn = None
            self._paste_window = None
            self._paste_input = None
            self._paste_send_team = None
            self._paste_send_all = None
            if not from_destroy:
                try: win.destroy()
                except Exception: pass
        win.protocol("WM_DELETE_WINDOW", lambda: _on_close(False))
        win.bind("<Destroy>", lambda e: (_on_close(True) if e.widget is win else None))

        # Expose the input + send functions so global hotkeys can use them.
        self._paste_input = txt
        self._paste_send_team = do_send_team
        self._paste_send_all = do_send_all

        # The input was pre-filled from the clipboard earlier. If that
        # content is non-empty, translate it right away so the user
        # doesn't have to click Translate — matches "open → see result".
        # Delay is long enough that the focus-grab dance and first-run
        # Translator init don't race with us.
        try:
            existing = txt.get("1.0", "end").strip()
            if existing:
                def _auto_tr(_e=None):
                    # Only translate if result is still empty — avoids
                    # re-firing on every Visibility/Map retry after a
                    # successful translation.
                    try:
                        if result_txt.get("1.0", "end").strip():
                            return
                    except Exception:
                        return
                    try:
                        status_lbl.config(text="Translating...", fg="#d8c88f")
                    except Exception: pass
                    do_translate()
                    try:
                        win.after(1800, lambda: status_lbl.config(text=""))
                    except Exception: pass
                # Fire immediately (worker runs in a thread, so even if
                # the focus dance is still in progress, the HTTP call
                # starts now instead of waiting 500ms of Tk idle).
                self.root.after(0, _auto_tr)
                # Backup triggers for the rare case where Tk's after()
                # is starved while the paste window is trying to take
                # foreground (Windows deprioritizes background apps'
                # timers). <Visibility> and <Map> fire as soon as the
                # window is actually shown, regardless of fg status.
                win.bind("<Visibility>", _auto_tr, add="+")
                win.bind("<Map>",        _auto_tr, add="+")
                # Final safety net: longer delay for first-run Translator
                # init (import + first HTTP call can take >1s).
                win.after(1500, _auto_tr)
        except Exception:
            pass

        # Force focus into the input after the window is fully mapped,
        # so the user can just start typing immediately.  When the
        # Paste window is opened from a global hotkey (Dota is the
        # foreground app), Windows blocks SetForegroundWindow unless
        # the calling thread *just* received user input — so we fake
        # an Alt key press to grant our process the foreground
        # privilege, same trick used by _paste_to_dota_chat.
        def _safe_topmost_off():
            try:
                if win.winfo_exists():
                    win.attributes("-topmost", False)
            except Exception:
                pass

        def _focus_input():
            """Bring the paste window to the foreground without blocking Tk.

            The Win32 foreground-grab (Alt-tap + SetForegroundWindow) runs in
            a worker thread so Tk's mainloop keeps pumping timers and events.
            Once the thread succeeds (or gives up), it schedules focus_set
            back on the Tk thread via root.after().
            """
            def _win32_grab():
                try:
                    import ctypes as _ct
                    u32 = _ct.windll.user32
                    # Alt-tap: hands our process the "foreground lock"
                    # so SetForegroundWindow actually works from a
                    # background process (Dota is currently fg).
                    u32.keybd_event(0x12, 0, 0, 0)       # Alt down
                    u32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
                    try:
                        hwnd = u32.GetParent(win.winfo_id()) or win.winfo_id()
                        u32.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
                except Exception:
                    pass
                # Schedule the Tk-side focus work back on the main thread.
                try:
                    self.root.after(0, _tk_focus)
                except Exception:
                    pass

            def _tk_focus():
                try:
                    if not win.winfo_exists():
                        return
                    win.deiconify()
                    win.lift()
                    win.attributes("-topmost", True)
                    win.after(300, _safe_topmost_off)
                    win.focus_force()
                    txt.focus_set()
                except Exception:
                    pass

            # deiconify immediately so the window is visible,
            # then grab foreground in a thread.
            try:
                win.deiconify()
                win.lift()
                win.attributes("-topmost", True)
                win.after(300, _safe_topmost_off)
            except Exception:
                pass
            threading.Thread(target=_win32_grab, daemon=True).start()

        win.after(50, _focus_input)

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
                # Press Escape first to close chat if the user already had it
                # open — otherwise our Enter would toggle it closed instead of
                # opening it, and the message would never send.
                VK_ESCAPE = 0x1B
                _kd(VK_ESCAPE); time.sleep(0.05); _ku(VK_ESCAPE)
                time.sleep(0.15)
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
