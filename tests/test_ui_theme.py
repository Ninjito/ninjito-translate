"""The shared design tokens and widgets.

Two kinds of test here. The palette ones are about legibility — text has
to stay readable against whatever surface it sits on, and a token nudged
by hand shouldn't be able to break that quietly. The widget ones are
about state, because every control in Settings is now hand-drawn and
nothing else checks that clicking one actually reports the new value.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dota_ocr import ui_theme as t


@pytest.fixture(scope="module")
def _root():
    """One Tk root for the module.

    Creating and tearing down a root per test is unreliable — after
    enough of them Tk starts refusing to open a display connection, which
    turned into intermittent skips.
    """
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


@pytest.fixture
def tk_root(_root):
    """A throwaway parent, so widgets from one test can't reach another."""
    frame = tk.Frame(_root)
    yield frame
    try:
        frame.destroy()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


ALL_TOKENS = ["BG", "PANEL", "RIDGE", "RIDGE_HOVER", "LINE", "GOLD",
              "GOLD_HOVER", "GOLD_DIM", "TEXT", "TEXT_DIM", "MUTED",
              "OK", "WARN", "DANGER", "ON_GOLD"]


@pytest.mark.parametrize("name", ALL_TOKENS)
def test_every_token_is_a_hex_colour(name):
    value = getattr(t, name)
    assert isinstance(value, str) and value.startswith("#")
    assert len(value) == 7
    int(value[1:], 16)


@pytest.mark.parametrize("fg,bg,floor", [
    (t.TEXT, t.BG, 4.5),        # body text, normal size -> AA
    (t.TEXT, t.PANEL, 4.5),
    (t.TEXT, t.RIDGE, 4.5),
    (t.GOLD, t.BG, 4.5),        # the active tab label
    (t.GOLD, t.PANEL, 4.5),
    (t.ON_GOLD, t.GOLD, 4.5),   # text on a gold fill
    (t.OK, t.BG, 4.5),
    (t.WARN, t.PANEL, 4.5),
    (t.DANGER, t.PANEL, 4.5),
    (t.TEXT_DIM, t.PANEL, 3.0),  # hints, secondary -> AA large
    (t.MUTED, t.BG, 3.0),        # section eyebrows
])
def test_text_is_legible_on_its_surface(fg, bg, floor):
    assert contrast(fg, bg) >= floor, (
        f"{fg} on {bg} is {contrast(fg, bg):.2f}:1, below {floor}:1")


def test_surfaces_are_distinguishable_from_each_other():
    """The card has to read as a separate plane from the ground."""
    assert contrast(t.PANEL, t.BG) > 1.05
    assert contrast(t.RIDGE, t.PANEL) > 1.05


def test_spacing_scale_is_ordered():
    assert t.XS < t.SM < t.MD < t.LG < t.XL


def test_fonts_resolve_to_something_installed(tk_root):
    for spec in (t.ui(9), t.semi(8), t.mono(9, bold=True)):
        assert isinstance(spec[0], str) and spec[0]
        assert isinstance(spec[1], int)


# --------------------------------------------------------------------------
# Toggle
# --------------------------------------------------------------------------


class TestToggle:
    def test_reports_initial_value(self, tk_root):
        assert t.Toggle(tk_root, value=True).get() is True
        assert t.Toggle(tk_root, value=False).get() is False

    def test_click_flips_and_reports(self, tk_root):
        seen = []
        sw = t.Toggle(tk_root, value=False, command=seen.append)
        sw._clicked()
        assert sw.get() is True
        assert seen == [True]
        sw._clicked()
        assert sw.get() is False
        assert seen == [True, False]

    def test_set_does_not_fire_the_callback(self, tk_root):
        """Reflecting reality back into the switch must not re-trigger it.

        Voice can fail to start, so the panel snaps the switch back to
        what actually happened — which would loop if set() called back.
        """
        seen = []
        sw = t.Toggle(tk_root, value=False, command=seen.append)
        sw.set(True)
        assert sw.get() is True
        assert seen == []

    def test_hover_does_not_change_value(self, tk_root):
        sw = t.Toggle(tk_root, value=True)
        sw._enter()
        sw._leave()
        assert sw.get() is True


# --------------------------------------------------------------------------
# Segmented
# --------------------------------------------------------------------------


class TestSegmented:
    OPTIONS = [("base", "Base"), ("small", "Small"), ("medium", "Medium")]

    def test_starts_on_the_given_value(self, tk_root):
        assert t.Segmented(tk_root, self.OPTIONS, "small").get() == "small"

    def test_picking_reports_once(self, tk_root):
        seen = []
        seg = t.Segmented(tk_root, self.OPTIONS, "small", command=seen.append)
        seg._pick("medium")
        assert seg.get() == "medium"
        assert seen == ["medium"]

    def test_picking_the_current_value_is_a_no_op(self, tk_root):
        seen = []
        seg = t.Segmented(tk_root, self.OPTIONS, "small", command=seen.append)
        seg._pick("small")
        assert seen == []

    def test_only_the_selection_is_gold(self, tk_root):
        seg = t.Segmented(tk_root, self.OPTIONS, "small")
        seg._pick("base")
        assert seg._buttons["base"].cget("bg") == t.GOLD
        assert seg._buttons["small"].cget("bg") == t.RIDGE
        assert seg._buttons["medium"].cget("bg") == t.RIDGE

    def test_set_does_not_fire_the_callback(self, tk_root):
        seen = []
        seg = t.Segmented(tk_root, self.OPTIONS, "small", command=seen.append)
        seg.set("base")
        assert seg.get() == "base"
        assert seen == []


# --------------------------------------------------------------------------
# Dropdown
# --------------------------------------------------------------------------


class TestDropdown:
    VALUES = ["Default output", "Speakers", "Headset"]

    def test_shows_the_selected_value(self, tk_root):
        dd = t.Dropdown(tk_root, self.VALUES, value="Speakers")
        assert dd.get() == "Speakers"
        assert dd._label.cget("text") == "Speakers"

    def test_falls_back_to_the_first_value(self, tk_root):
        assert t.Dropdown(tk_root, self.VALUES).get() == "Default output"

    def test_empty_values_do_not_raise(self, tk_root):
        assert t.Dropdown(tk_root, []).get() == ""

    def test_picking_reports_and_relabels(self, tk_root):
        seen = []
        dd = t.Dropdown(tk_root, self.VALUES, command=seen.append)
        dd._pick("Headset")
        assert seen == ["Headset"]
        assert dd._label.cget("text") == "Headset"

    def test_set_does_not_fire_the_callback(self, tk_root):
        seen = []
        dd = t.Dropdown(tk_root, self.VALUES, command=seen.append)
        dd.set("Headset")
        assert dd.get() == "Headset"
        assert seen == []

    def test_menu_lists_every_value(self, tk_root):
        dd = t.Dropdown(tk_root, self.VALUES)
        assert dd._menu.index("end") == len(self.VALUES) - 1


# --------------------------------------------------------------------------
# Keycap
# --------------------------------------------------------------------------


class TestKeycap:
    def test_shows_the_combo(self, tk_root):
        assert t.Keycap(tk_root, "Ctrl+Shift+L")._label.cget("text") \
            == "Ctrl+Shift+L"

    def test_click_invokes_the_command(self, tk_root):
        seen = []
        cap = t.Keycap(tk_root, "F7", command=lambda: seen.append(1))
        cap._clicked()
        assert seen == [1]

    def test_listening_goes_gold(self, tk_root):
        cap = t.Keycap(tk_root, "F7")
        cap.set_listening(True)
        assert cap._label.cget("bg") == t.GOLD
        assert cap._label.cget("fg") == t.ON_GOLD

    def test_leaving_listening_restores_and_relabels(self, tk_root):
        cap = t.Keycap(tk_root, "F7")
        cap.set_listening(True)
        cap.set_listening(False, "F8")
        assert cap._label.cget("bg") == t.RIDGE
        assert cap._label.cget("text") == "F8"

    def test_hover_is_ignored_while_listening(self, tk_root):
        """Hover must not repaint over the 'press a key now' state."""
        cap = t.Keycap(tk_root, "F7")
        cap.set_listening(True)
        cap._enter()
        assert cap._label.cget("bg") == t.GOLD
        cap._leave()
        assert cap._label.cget("bg") == t.GOLD


# --------------------------------------------------------------------------
# TabView
# --------------------------------------------------------------------------


class TestTabView:
    def test_first_tab_is_selected_on_add(self, tk_root):
        tv = t.TabView(tk_root)
        tv.add("one", "One")
        tv.add("two", "Two")
        assert tv.active() == "one"

    def test_select_switches_the_visible_pane(self, tk_root):
        tv = t.TabView(tk_root)
        first = tv.add("one", "One")
        second = tv.add("two", "Two")
        tv.select("two")
        assert tv.active() == "two"
        assert second.winfo_manager() == "pack"
        assert first.winfo_manager() == ""

    def test_only_the_active_tab_is_marked(self, tk_root):
        tv = t.TabView(tk_root)
        tv.add("one", "One")
        tv.add("two", "Two")
        tv.select("two")
        assert tv._tabs["two"][0].cget("fg") == t.GOLD
        assert tv._tabs["two"][1].cget("bg") == t.GOLD
        assert tv._tabs["one"][0].cget("fg") == t.TEXT_DIM
        assert tv._tabs["one"][1].cget("bg") == t.BG

    def test_selecting_an_unknown_key_is_ignored(self, tk_root):
        tv = t.TabView(tk_root)
        tv.add("one", "One")
        tv.select("nope")
        assert tv.active() == "one"

    def test_hover_does_not_override_the_active_tab(self, tk_root):
        tv = t.TabView(tk_root)
        tv.add("one", "One")
        tv.add("two", "Two")
        tv._on_enter("one")          # "one" is active
        assert tv._tabs["one"][0].cget("fg") == t.GOLD
        tv._on_leave("one")
        assert tv._tabs["one"][0].cget("fg") == t.GOLD
