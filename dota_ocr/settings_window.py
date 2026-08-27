"""The Settings window.

Lifted out of overlay.py, which was 3,300 lines with this accounting for
about 370 of them. The behaviour and the config wiring are unchanged —
every control still calls the same method on the Overlay it was built
from. What changed is that it's now built from `ui_theme` components
instead of inline colours, so the spacing lines up and the controls
aren't stock Tk.

The panel gets opened mid-match, used for a few seconds and closed, so
it's laid out for scanning: label on the left, control right-aligned on
a shared axis, one card per group.
"""

from __future__ import annotations

import tkinter as tk

from dota_ocr import sizes as _sz
from dota_ocr import ui_theme as t


class SettingsWindow:
    """Builds and owns the Settings Toplevel for one Overlay."""

    def __init__(self, overlay) -> None:
        self.overlay = overlay
        self.win: tk.Toplevel | None = None
        self._keycaps: dict[int, t.Keycap] = {}
        self._key_binding = None
        self._closing = False

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def build(self) -> tk.Toplevel:
        ov = self.overlay
        win = tk.Toplevel(ov.root)
        self.win = win
        win.title("Settings")
        win.configure(bg=t.BG)
        win.geometry(f"{_sz.SETTINGS_WIDTH}x{_sz.SETTINGS_HEIGHT}")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        t.style_widgets(win)
        t.style_dropdown(win)

        self.tabview = t.TabView(win)
        self.tabview.pack(fill="both", expand=True, pady=(0, t.LG))

        tabs = {}
        for key, label in (("hotkeys", "Hotkeys"), ("capture", "Capture"),
                           ("voice", "Voice"), ("suggest", "Suggest"),
                           ("theme", "Theme")):
            tabs[key] = self.tabview.add(key, label)

        self._build_hotkeys(tabs["hotkeys"])
        self._build_capture(tabs["capture"])
        self._build_voice(tabs["voice"])
        self._build_suggest(tabs["suggest"])
        self._build_theme(tabs["theme"])
        return win

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------

    def _build_hotkeys(self, parent) -> None:
        ov = self.overlay
        from dota_ocr.overlay import _combo_name

        t.section_header(parent, "Keybinds")
        box = t.card(parent)
        for index, (aid, info) in enumerate(ov._action_defs.items()):
            if index:
                t.divider(box)
            slot = t.row(box, info["label"], compact=True)
            cap = t.Keycap(slot, _combo_name(info["vk"], info["mods"]),
                           command=lambda a=aid: self._start_capture(a))
            cap.pack()
            self._keycaps[aid] = cap

        t.hint(parent,
               "Click a key to rebind it. Combos like Ctrl+Shift+L work "
               "globally;\nF-keys on their own are less likely to collide "
               "with other apps.",
               fg=t.MUTED)

    def _refresh_keycap(self, aid: int) -> None:
        from dota_ocr.overlay import _combo_name
        info = self.overlay._action_defs[aid]
        self._keycaps[aid].set_listening(False,
                                         _combo_name(info["vk"], info["mods"]))

    def _start_capture(self, aid: int) -> None:
        """Listen for the next combo and bind it to this action."""
        ov = self.overlay
        win = self.win
        from dota_ocr.overlay import _combo_name, _KEYSYM_TO_VK

        info = ov._action_defs[aid]
        self._keycaps[aid].set_listening(True)
        # Every global hotkey goes quiet while capturing, so typing e.g.
        # Ctrl+Shift+P here doesn't also open the Paste window.
        ov._request_hotkey_unregister_all()

        def finish() -> None:
            if self._key_binding is not None:
                try:
                    win.unbind("<Key>", self._key_binding)
                except Exception:
                    pass
                self._key_binding = None
            ov._request_hotkey_reregister()

        def capture(event: tk.Event):
            keysym = event.keysym
            if keysym == "Escape":
                self._refresh_keycap(aid)
                finish()
                return "break"
            if keysym in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                          "Alt_L", "Alt_R"):
                return "break"          # a modifier alone isn't a binding
            vk = _KEYSYM_TO_VK.get(keysym)
            if vk is None and len(keysym) == 1 and keysym.isalnum():
                vk = ord(keysym.upper())
            if vk is None:
                self._keycaps[aid].set_listening(True, f"{keysym} won't bind")
                return "break"

            mods = 0
            if event.state & 0x0004:
                mods |= 0x0002          # Ctrl
            if event.state & 0x0001:
                mods |= 0x0004          # Shift
            if event.state & 0x20000:
                mods |= 0x0001          # Alt

            info["vk"] = vk
            info["mods"] = mods
            self._refresh_keycap(aid)
            finish()
            ov._persist_hotkeys()

            # Keep the Translate button and the status hint in step.
            if aid == 1:
                ov._hotkey_vk = vk
                ov._hotkey_name = _combo_name(vk, mods)
                try:
                    ov._btn.configure(
                        text=f"📷 Translate ({ov._hotkey_name})")
                except Exception:
                    pass
                try:
                    ov._status.configure(
                        text=f"Press button or {ov._hotkey_name} to translate",
                        fg=t.MUTED)
                except Exception:
                    pass
            return "break"

        self._key_binding = win.bind("<Key>", capture)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _build_capture(self, parent) -> None:
        ov = self.overlay
        cfg = ov._cfg or {}

        t.section_header(parent, "Automatic capture")
        box = t.card(parent)

        slot = t.row(box, "Read chat on a timer",
                     "Runs OCR on the chat region on its own, no key press.")
        t.Toggle(slot, value=bool(cfg.get("auto_ocr_enabled", False)),
                 command=lambda v: (ov._set_cfg("auto_ocr_enabled", v),
                                    ov._apply_auto_ocr())).pack()

        t.divider(box)
        interval = tk.IntVar(value=int(cfg.get("auto_ocr_interval_sec", 5)))
        slot = t.row(box, "How often", "Seconds between reads.")
        tk.Spinbox(
            slot, from_=2, to=120, width=4, textvariable=interval,
            bg=t.RIDGE, fg=t.TEXT, insertbackground=t.TEXT,
            buttonbackground=t.RIDGE, font=t.mono(9), relief="flat",
            highlightthickness=1, highlightbackground=t.LINE,
            highlightcolor=t.GOLD_DIM,
            command=lambda: (ov._set_cfg("auto_ocr_interval_sec",
                                         interval.get()),
                             ov._apply_auto_ocr()),
        ).pack()

        t.section_header(parent, "Chat key")
        box = t.card(parent)
        slot = t.row(box, "Read chat when I open it",
                     "Fires the moment you press Enter in Dota.")
        t.Toggle(slot, value=bool(cfg.get("auto_translate_on_chat", False)),
                 command=lambda v: (ov._set_cfg("auto_translate_on_chat", v),
                                    ov._apply_auto_chat_watch())).pack()

    # ------------------------------------------------------------------
    # Voice
    # ------------------------------------------------------------------

    def _build_voice(self, parent) -> None:
        ov = self.overlay
        vblock = dict((ov._cfg or {}).get("voice") or {})

        t.section_header(parent, "Russian voice chat")
        box = t.card(parent)
        slot = t.row(box, "Translate what I hear",
                     "Listens to your speakers, not your microphone.")
        self._voice_toggle = t.Toggle(
            slot, value=bool(vblock.get("enabled", False)),
            command=self._on_voice_toggled)
        self._voice_toggle.pack()

        # -- device --
        try:
            from dota_ocr import voice as _voice
            devices = _voice.list_loopback_devices()
        except Exception as e:
            print(f"[voice] settings device list failed: {e}", flush=True)
            devices = []

        t.section_header(parent, "Listen to")
        box = t.card(parent)
        # " [Loopback]" is an artifact of how WASAPI exposes the device,
        # not something the user picked, so it isn't shown.
        labels = ["Default output"] + [
            d["name"].replace(" [Loopback]", "") for d in devices]
        current = str(vblock.get("device_name", "") or "")
        picked = labels[0]
        for dev, label in zip(devices, labels[1:]):
            if dev["name"] == current:
                picked = label
                break

        def on_device(choice):
            if choice == labels[0]:
                ov._set_voice_cfg("device_name", "")
                ov._set_voice_cfg("device_index", None)
            else:
                for dev, label in zip(devices, labels[1:]):
                    if label == choice:
                        ov._set_voice_cfg("device_name", dev["name"])
                        ov._set_voice_cfg("device_index", dev["index"])
                        break
            ov._settings_voice_restart()

        wrap = tk.Frame(box, bg=t.PANEL)
        wrap.pack(fill="x", padx=t.MD, pady=t.MD)
        t.Dropdown(wrap, labels, value=picked,
                   command=on_device).pack(fill="x")
        if not devices:
            tk.Label(wrap, text="No loopback device found — voice can't run.",
                     bg=t.PANEL, fg=t.WARN, font=t.ui(8),
                     anchor="w").pack(fill="x", pady=(t.SM, 0))

        # -- model --
        t.section_header(parent, "Recognition model")
        box = t.card(parent)
        wrap = tk.Frame(box, bg=t.PANEL)
        wrap.pack(fill="x", padx=t.MD, pady=t.MD)
        t.Segmented(
            wrap,
            [("base", "Base"), ("small", "Small"), ("medium", "Medium")],
            value=str(vblock.get("model_size", "small")),
            command=lambda v: (ov._set_voice_cfg("model_size", v),
                               ov._settings_voice_restart()),
        ).pack(anchor="w")
        tk.Label(wrap,
                 text="Bigger is more accurate and slower. Base is quickest, "
                      "Medium\nhandles noisy voice chat best. Each one "
                      "downloads once.",
                 bg=t.PANEL, fg=t.TEXT_DIM, font=t.ui(8), anchor="w",
                 justify="left").pack(fill="x", pady=(t.SM, 0))

    def _on_voice_toggled(self, value: bool) -> None:
        """Mirror the toggle into config, then reflect what actually happened.

        Starting can fail — no loopback device, a model that won't load —
        so the switch is snapped back to the state the app really reached
        rather than the one the click asked for.
        """
        ov = self.overlay
        var = tk.BooleanVar(value=value)
        try:
            ov._settings_voice_enable(var)
        except Exception as e:
            print(f"[voice] toggle failed: {e}", flush=True)
        try:
            actual = bool((ov._cfg or {}).get("voice", {}).get("enabled",
                                                               False))
            self._voice_toggle.set(actual)
        except Exception:
            pass
        ov.notify_tray()

    # ------------------------------------------------------------------
    # Suggest
    # ------------------------------------------------------------------

    def _build_suggest(self, parent) -> None:
        ov = self.overlay
        current = dict((ov._cfg or {}).get("suggest") or {})

        t.section_header(parent, "Typing suggestions")
        box = t.card(parent)
        rows = [
            ("enabled", "Suggest while I type", ""),
            ("fix_word", "Fix the word I'm on", ""),
            ("complete_word", "Finish the word", ""),
            ("fix_sentence", "Fix the sentence's grammar", "Needs internet."),
            ("translate_live", "Translate my line to English",
             "As you type, before you send."),
        ]
        for index, (key, label, note) in enumerate(rows):
            if index:
                t.divider(box)
            default = False if key == "translate_live" else True
            slot = t.row(box, label, note)
            t.Toggle(slot, value=bool(current.get(key, default)),
                     command=lambda v, k=key: ov._set_suggest_cfg(k, v)).pack()

        t.hint(parent, "↑ ↓ choose     ← → insert     Esc dismiss")
        ov._suggest_status = tk.Label(parent, text="", bg=t.BG, fg=t.OK,
                                      font=t.ui(8), anchor="w")
        ov._suggest_status.pack(fill="x", padx=t.LG, pady=(t.SM, 0))
        ov._refresh_suggest_status()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _build_theme(self, parent) -> None:
        ov = self.overlay
        cfg = ov._cfg or {}

        descriptions = {
            "dark": "Solid dark panel behind the text.",
            "light": "Light panel with dark text.",
            "transparent": "Text floats straight over the game.",
        }

        t.section_header(parent, "Overlay appearance")
        box = t.card(parent)
        wrap = tk.Frame(box, bg=t.PANEL)
        wrap.pack(fill="x", padx=t.MD, pady=t.MD)

        note = tk.Label(wrap, text="", bg=t.PANEL, fg=t.TEXT_DIM,
                        font=t.ui(8), anchor="w")

        def on_theme(value: str) -> None:
            ov._set_cfg("theme", value)
            ov._apply_theme(value)
            note.configure(text=descriptions.get(value, ""))

        current = str(cfg.get("theme", "dark"))
        t.Segmented(
            wrap,
            [("dark", "Dark"), ("light", "Light"),
             ("transparent", "Transparent")],
            value=current, command=on_theme,
        ).pack(anchor="w")
        note.configure(text=descriptions.get(current, ""))
        note.pack(fill="x", pady=(t.SM, 0))
