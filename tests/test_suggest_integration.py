"""End-to-end checks with the real popup, suggester, and settings tab.

The unit tests each swap out their neighbours, so nothing else proves
the pieces fit: that a keystroke arriving on the hook thread ends up
rendered in an actual Tk window, and that the Settings tab writes the
keys the controller reads back.

The keyboard hook and SendInput are still stubbed — those need a real
game window and are covered by the manual checklist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

tk = pytest.importorskip("tkinter")

from dota_ocr.suggest import Suggester
from dota_ocr.suggest_controller import SuggestController
from dota_ocr.suggest_popup import SuggestPopup
from dota_ocr.typing_buffer import KeyEvent, VK_RETURN, VK_RIGHT


class _Typer:
    def __init__(self):
        self.calls = []

    def replace_word(self, n, text):
        self.calls.append(("word", n, text))

    def send_backspaces(self, n):
        self.calls.append(("back", n))

    def send_text(self, text):
        self.calls.append(("text", text))


class _Root:
    """Wraps a real Tk root but never reschedules the pump itself."""

    def __init__(self, tk_root):
        self.tk_root = tk_root

    def after(self, _ms, fn=None, *args):
        return "id"

    def after_cancel(self, _id):
        pass


@pytest.fixture
def tk_root():
    try:
        r = tk.Tk()
    except tk.TclError:  # pragma: no cover - no display available
        pytest.skip("no display")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


CFG = {
    "suggest": {
        "enabled": True, "fix_word": True, "complete_word": True,
        "fix_sentence": False, "translate_live": False,
        "max_results": 3, "min_prefix": 2, "grammar_debounce_ms": 600,
        "popup": {"x": 300, "y": 300},
    }
}


@pytest.fixture
def wired(tk_root):
    popup = SuggestPopup(tk_root)
    typer = _Typer()
    ctrl = SuggestController(
        root=_Root(tk_root),
        cfg=CFG,
        popup=popup,
        hook_factory=None,
        suggester=Suggester(words={"mid": 600, "middle": 100,
                                   "might": 90, "push": 500},
                            max_results=3, min_prefix=2),
        grammar=None,
        typer_mod=typer,
        is_dota_foreground=lambda: True,
    )
    yield ctrl, popup, typer
    popup.destroy()


def _type(ctrl, text):
    for c in text:
        ctrl.handle_event(KeyEvent(vk=ord(c.upper()), down=True, char=c,
                                   shift=False, ctrl=False, alt=False))
    ctrl.pump()


def _press(ctrl, vk):
    ctrl.handle_event(KeyEvent(vk=vk, down=True, char="", shift=False,
                               ctrl=False, alt=False))
    ctrl.pump()


class TestRealPopupPath:
    def test_typing_renders_in_a_real_window(self, wired):
        ctrl, popup, _ = wired
        _press(ctrl, VK_RETURN)
        _type(ctrl, "mi")
        assert popup.visible is True
        shown = [r.cget("text") for r in popup._rows if r.winfo_manager()]
        assert any("mid" in t for t in shown)

    def test_sending_the_message_clears_the_window(self, wired):
        ctrl, popup, _ = wired
        _press(ctrl, VK_RETURN)
        _type(ctrl, "mi")
        _press(ctrl, VK_RETURN)
        assert popup.visible is False

    def test_right_arrow_types_the_highlighted_word(self, wired):
        ctrl, popup, typer = wired
        _press(ctrl, VK_RETURN)
        _type(ctrl, "mi")
        _press(ctrl, VK_RIGHT)
        assert typer.calls[0][0] == "word"
        assert typer.calls[0][1] == 2
        assert ctrl.buffer.text == typer.calls[0][2]
        assert popup.visible is False

    def test_configured_popup_position_is_used(self, wired):
        ctrl, popup, _ = wired
        _press(ctrl, VK_RETURN)
        _type(ctrl, "mi")
        geom = popup._win.geometry()
        assert geom.endswith("+300+300")


def _toggles(widget):
    """Every Toggle switch under `widget`, at any depth.

    The panel nests them inside card -> row -> control frames, so a
    single level of winfo_children() no longer finds them.
    """
    from dota_ocr.ui_theme import Toggle
    found = []
    for child in widget.winfo_children():
        if isinstance(child, Toggle):
            found.append(child)
        found.extend(_toggles(child))
    return found


def _build_suggest_tab(overlay_shim, parent):
    """Build the Suggest tab against a stand-in Overlay."""
    from dota_ocr.settings_window import SettingsWindow
    panel = SettingsWindow.__new__(SettingsWindow)
    panel.overlay = overlay_shim
    panel._build_suggest(parent)
    return panel


class TestSettingsTab:
    """The tab must write exactly the keys the controller reads."""

    def test_tab_builds_and_toggles_persist(self, tk_root):
        from dota_ocr.overlay import Overlay

        written = {}

        class Shim:
            _cfg = {"suggest": dict(CFG["suggest"])}
            _suggest_controller = None

            def _set_cfg(self, key, value):
                written[key] = value
                self._cfg[key] = value

            _refresh_suggest_status = Overlay._refresh_suggest_status
            _set_suggest_cfg = Overlay._set_suggest_cfg

        shim = Shim()
        frame = tk.Frame(tk_root)
        _build_suggest_tab(shim, frame)

        assert len(_toggles(frame)) == 5

        shim._set_suggest_cfg("fix_sentence", False)
        assert written["suggest"]["fix_sentence"] is False
        # Everything else must survive the write.
        assert written["suggest"]["fix_word"] is True
        assert written["suggest"]["max_results"] == 3

    def test_controller_reads_what_the_tab_wrote(self, tk_root):
        """The two sides agree on the key names, not just the shape."""
        from dota_ocr.overlay import Overlay

        cfg = {"suggest": dict(CFG["suggest"])}

        class Shim:
            _suggest_controller = None

            def __init__(self):
                self._cfg = cfg

            def _set_cfg(self, key, value):
                self._cfg[key] = value

            _refresh_suggest_status = Overlay._refresh_suggest_status
            _set_suggest_cfg = Overlay._set_suggest_cfg

        shim = Shim()
        ctrl = SuggestController(root=_Root(tk_root), cfg=cfg,
                                 popup=SuggestPopup(tk_root),
                                 is_dota_foreground=lambda: True)
        assert ctrl._opt("fix_word", None) is True
        shim._set_suggest_cfg("fix_word", False)
        assert ctrl._opt("fix_word", None) is False
        ctrl.stop()
