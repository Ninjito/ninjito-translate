"""Tests for the controller that turns keystrokes into suggestions.

The swallow predicate is the risky part: it runs inside the hook
callback and decides whether Dota sees a key at all. Swallowing when
nothing is showing would break the scoreboard and the text caret in the
middle of a game, so every case is pinned here.

The fake root deliberately does NOT run queued work on its own — tests
call `pump()` where the Tk thread would, which keeps the queue hand-off
under test instead of papering over it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dota_ocr.suggest import Suggester, Suggestion
from dota_ocr.suggest_controller import SuggestController
from dota_ocr.typing_buffer import (
    KeyEvent, VK_RETURN, VK_ESCAPE, VK_TAB, VK_UP, VK_LEFT, VK_RIGHT,
)

WORDS = {"mid": 600, "middle": 100, "roshan": 250, "push": 500,
         "receive": 400, "gank": 300, "ganking": 100}


class FakePopup:
    def __init__(self):
        self.visible = False
        self.items = []
        self.index = 0

    def show(self, items, index, x, y):
        self.items = list(items)
        self.index = index
        self.visible = bool(items)

    def hide(self):
        self.visible = False
        self.items = []
        self.index = 0

    def destroy(self):
        self.hide()


class FakeTyper:
    def __init__(self):
        self.calls = []

    def replace_word(self, backspaces, replacement):
        self.calls.append(("word", backspaces, replacement))

    def send_backspaces(self, n):
        self.calls.append(("back", n))

    def send_text(self, text):
        self.calls.append(("text", text))


class FakeRoot:
    """Records after() calls without running them."""

    def __init__(self):
        self.scheduled = 0

    def after(self, _ms, fn=None, *args):
        self.scheduled += 1
        return "id"

    def after_cancel(self, _id):
        pass


class FakeHook:
    def __init__(self, on_event=None, should_swallow=None, ok=True):
        self.on_event = on_event
        self.should_swallow = should_swallow
        self._ok = ok
        self._running = False
        self.last_error = "" if ok else "boom"

    def start(self):
        self._running = self._ok
        return self._ok

    def stop(self):
        self._running = False

    def is_running(self):
        return self._running


class FakeGrammar:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def suggest_line(self, text):
        self.calls += 1
        if not self.result:
            return []
        return [Suggestion(self.result, "sentence", "line")]


def _cfg(**over):
    base = {
        "enabled": True, "fix_word": True, "complete_word": True,
        "fix_sentence": False, "translate_live": False,
        "max_results": 3, "grammar_debounce_ms": 600, "min_prefix": 2,
    }
    base.update(over)
    return {"suggest": base}


def _make(cfg=None, grammar=None):
    popup = FakePopup()
    typer = FakeTyper()
    ctrl = SuggestController(
        root=FakeRoot(),
        cfg=cfg or _cfg(),
        popup=popup,
        hook_factory=FakeHook,
        suggester=Suggester(words=WORDS, max_results=3, min_prefix=2),
        grammar=grammar or FakeGrammar(),
        typer_mod=typer,
    )
    return ctrl, popup, typer


def _ch(ctrl, text):
    for c in text:
        ctrl.handle_event(KeyEvent(vk=ord(c.upper()), down=True, char=c,
                                   shift=False, ctrl=False, alt=False))
    ctrl.pump()


def _vk(ctrl, vk, shift=False):
    ctrl.handle_event(KeyEvent(vk=vk, down=True, char="", shift=shift,
                               ctrl=False, alt=False))
    ctrl.pump()


def _open_chat(ctrl):
    _vk(ctrl, VK_RETURN)


class TestLifecycle:
    def test_start_installs_the_hook(self):
        ctrl, _, _ = _make()
        assert ctrl.start() is True
        assert ctrl.is_running() is True

    def test_start_is_refused_when_disabled(self):
        ctrl, _, _ = _make(cfg=_cfg(enabled=False))
        assert ctrl.start() is False
        assert ctrl.last_error == "disabled in settings"

    def test_failed_hook_reports_and_stays_stopped(self):
        popup = FakePopup()
        ctrl = SuggestController(
            root=FakeRoot(), cfg=_cfg(), popup=popup,
            hook_factory=lambda **kw: FakeHook(ok=False, **kw),
            suggester=Suggester(words=WORDS), grammar=FakeGrammar(),
            typer_mod=FakeTyper())
        assert ctrl.start() is False
        assert ctrl.last_error == "boom"
        assert ctrl.is_running() is False

    def test_stop_is_safe_without_start(self):
        ctrl, _, _ = _make()
        ctrl.stop()
        assert ctrl.is_running() is False


class TestCapture:
    def test_no_suggestions_before_chat_opens(self):
        ctrl, popup, _ = _make()
        _ch(ctrl, "mi")
        assert popup.visible is False

    def test_suggestions_appear_while_chat_is_open(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        assert popup.visible is True
        assert "middle" in [s.text for s in popup.items]

    def test_shift_enter_also_opens_capture(self):
        ctrl, popup, _ = _make()
        _vk(ctrl, VK_RETURN, shift=True)
        _ch(ctrl, "mi")
        assert popup.visible is True

    def test_the_opening_enter_is_not_part_of_the_message(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "gg")
        assert ctrl.buffer.text == "gg"

    def test_closing_chat_hides_the_popup_and_wipes_the_buffer(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_RETURN)          # send
        assert popup.visible is False
        assert ctrl.buffer.text == ""

    def test_space_ends_the_word_and_hides_the_popup(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi ")
        assert popup.visible is False

    def test_key_up_events_are_ignored(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        ctrl.handle_event(KeyEvent(vk=ord("M"), down=False, char="m",
                                   shift=False, ctrl=False, alt=False))
        ctrl.pump()
        assert ctrl.buffer.text == ""


class TestSwallow:
    def test_nav_keys_pass_through_when_nothing_is_showing(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        for vk in (VK_TAB, VK_UP, VK_LEFT, VK_RIGHT, VK_ESCAPE):
            ev = KeyEvent(vk=vk, down=True, char="", shift=False,
                          ctrl=False, alt=False)
            assert ctrl.should_swallow(ev) is False

    def test_nav_keys_are_swallowed_when_the_popup_is_up(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        assert popup.visible is True
        for vk in (VK_TAB, VK_UP, VK_LEFT, VK_RIGHT, VK_ESCAPE):
            ev = KeyEvent(vk=vk, down=True, char="", shift=False,
                          ctrl=False, alt=False)
            assert ctrl.should_swallow(ev) is True

    def test_swallowing_does_not_wait_for_the_tk_thread(self):
        """The decision must hold the instant the key arrives, not one
        pump later, or Tab leaks into the game."""
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        for c in "mi":
            ctrl.handle_event(KeyEvent(vk=ord(c.upper()), down=True, char=c,
                                       shift=False, ctrl=False, alt=False))
        ev = KeyEvent(vk=VK_TAB, down=True, char="", shift=False,
                      ctrl=False, alt=False)
        assert ctrl.should_swallow(ev) is True

    def test_letters_are_never_swallowed(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        ev = KeyEvent(vk=ord("D"), down=True, char="d", shift=False,
                      ctrl=False, alt=False)
        assert ctrl.should_swallow(ev) is False

    def test_enter_is_never_swallowed(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        ev = KeyEvent(vk=VK_RETURN, down=True, char="", shift=False,
                      ctrl=False, alt=False)
        assert ctrl.should_swallow(ev) is False

    def test_nothing_is_swallowed_when_chat_is_closed(self):
        ctrl, _, _ = _make()
        ev = KeyEvent(vk=VK_TAB, down=True, char="", shift=False,
                      ctrl=False, alt=False)
        assert ctrl.should_swallow(ev) is False

    def test_swallowing_stops_once_the_popup_is_dismissed(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_ESCAPE)
        ev = KeyEvent(vk=VK_TAB, down=True, char="", shift=False,
                      ctrl=False, alt=False)
        assert ctrl.should_swallow(ev) is False


class TestSelection:
    def test_right_moves_forward(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_RIGHT)
        assert popup.index == 1

    def test_left_moves_back_and_wraps(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_LEFT)
        assert popup.index == len(popup.items) - 1

    def test_up_moves_back_like_left(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_UP)
        assert popup.index == len(popup.items) - 1

    def test_nav_keys_never_reach_the_buffer(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_RIGHT)
        assert ctrl.buffer.text == "mi"

    def test_escape_hides_popup_but_keeps_chat_open(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_ESCAPE)
        assert popup.visible is False
        assert ctrl.session.is_open is True

    def test_typing_after_escape_brings_suggestions_back(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_ESCAPE)
        _ch(ctrl, "d")
        assert popup.visible is True


class TestAccept:
    def test_tab_replaces_the_word(self):
        ctrl, popup, typer = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        chosen = popup.items[popup.index].text
        _vk(ctrl, VK_TAB)
        assert typer.calls[0] == ("word", 2, chosen)

    def test_tab_updates_the_buffer_to_match(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        chosen = popup.items[popup.index].text
        _vk(ctrl, VK_TAB)
        assert ctrl.buffer.text == chosen

    def test_tab_takes_the_highlighted_one_not_the_first(self):
        ctrl, popup, typer = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_RIGHT)
        chosen = popup.items[popup.index].text
        _vk(ctrl, VK_TAB)
        assert typer.calls[0] == ("word", 2, chosen)

    def test_tab_hides_the_popup(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        _vk(ctrl, VK_TAB)
        assert popup.visible is False

    def test_tab_with_no_popup_does_nothing(self):
        ctrl, _, typer = _make()
        _open_chat(ctrl)
        _vk(ctrl, VK_TAB)
        assert typer.calls == []

    def test_accepting_mid_line_keeps_the_rest(self):
        ctrl, popup, typer = _make()
        _open_chat(ctrl)
        _ch(ctrl, "push mi")
        chosen = popup.items[popup.index].text
        _vk(ctrl, VK_TAB)
        assert typer.calls[0] == ("word", 2, chosen)
        assert ctrl.buffer.text == "push " + chosen

    def test_line_suggestion_replaces_the_whole_line(self):
        g = FakeGrammar(result="I need help")
        ctrl, popup, typer = _make(cfg=_cfg(fix_word=False,
                                            complete_word=False,
                                            fix_sentence=True),
                                   grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        ctrl.run_pending_grammar()
        ctrl.pump()
        assert popup.visible is True
        _vk(ctrl, VK_TAB)
        assert typer.calls[0] == ("back", len("i need halp"))
        assert typer.calls[1] == ("text", "I need help")
        assert ctrl.buffer.text == "I need help"


def _await_grammar(ctrl, fake, timeout=2.0):
    """Wait for the worker thread pump() spawned to finish.

    Waiting on `fake.calls` alone would race: the count goes up when the
    check starts, but the popup is only queued once it returns.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fake.calls > 0 and not ctrl._grammar_busy:
            break
        time.sleep(0.005)
    return fake.calls


class TestGrammarTiming:
    def test_not_checked_before_the_debounce_elapses(self):
        g = FakeGrammar(result="I need help")
        ctrl, _, _ = _make(cfg=_cfg(fix_sentence=True), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        assert ctrl._grammar_due(time.monotonic()) is False
        assert g.calls == 0

    def test_pump_checks_and_shows_once_the_debounce_elapses(self):
        """The whole path: debounce -> worker thread -> queue -> popup."""
        g = FakeGrammar(result="I need help")
        ctrl, popup, _ = _make(cfg=_cfg(fix_word=False, complete_word=False,
                                        fix_sentence=True,
                                        grammar_debounce_ms=0), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        assert _await_grammar(ctrl, g) == 1
        ctrl.pump()
        assert popup.visible is True
        assert popup.items[0].text == "I need help"
        assert popup.items[0].scope == "line"

    def test_the_same_line_is_not_checked_twice(self):
        g = FakeGrammar(result="I need help")
        ctrl, _, _ = _make(cfg=_cfg(fix_sentence=True,
                                    grammar_debounce_ms=0), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        _await_grammar(ctrl, g)
        ctrl.pump()
        ctrl.pump()
        assert g.calls == 1
        assert ctrl._grammar_due(time.monotonic()) is False

    def test_not_due_when_chat_is_closed(self):
        g = FakeGrammar(result="I need help")
        ctrl, _, _ = _make(cfg=_cfg(fix_sentence=True,
                                    grammar_debounce_ms=600), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        _vk(ctrl, VK_RETURN)
        assert ctrl._grammar_due(time.monotonic()) is False

    def test_a_stale_result_is_discarded(self):
        """The user kept typing while we were out on the network."""

        class MovingTarget(FakeGrammar):
            def suggest_line(self, text):
                out = super().suggest_line(text)
                # Simulate the hook thread advancing the line mid-request.
                ctrl._pending_line = "something else entirely"
                return out

        g = MovingTarget(result="I need help")
        ctrl, popup, _ = _make(cfg=_cfg(fix_word=False, complete_word=False,
                                        fix_sentence=True,
                                        grammar_debounce_ms=0), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        assert _await_grammar(ctrl, g) == 1
        ctrl.pump()
        assert popup.visible is False


class TestToggles:
    def test_disabling_fix_word_drops_corrections(self):
        ctrl, popup, _ = _make(cfg=_cfg(fix_word=False))
        _open_chat(ctrl)
        _ch(ctrl, "recieve")
        assert "receive" not in [s.text for s in popup.items]

    def test_disabling_both_word_toggles_shows_nothing(self):
        ctrl, popup, _ = _make(cfg=_cfg(fix_word=False, complete_word=False))
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        assert popup.visible is False

    def test_grammar_not_called_when_disabled(self):
        g = FakeGrammar(result="I need help")
        ctrl, _, _ = _make(cfg=_cfg(fix_sentence=False), grammar=g)
        _open_chat(ctrl)
        _ch(ctrl, "i need halp")
        ctrl.run_pending_grammar()
        assert g.calls == 0

    def test_max_results_is_honoured(self):
        popup = FakePopup()
        ctrl = SuggestController(
            root=FakeRoot(), cfg=_cfg(), popup=popup, hook_factory=FakeHook,
            suggester=Suggester(words=WORDS, max_results=2, min_prefix=2),
            grammar=FakeGrammar(), typer_mod=FakeTyper())
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        assert len(popup.items) <= 2


class TestRecovery:
    def test_foreground_loss_closes_and_wipes(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        ctrl.on_foreground_lost()
        ctrl.pump()
        assert popup.visible is False
        assert ctrl.buffer.text == ""
        assert ctrl.session.is_open is False

    def test_idle_timeout_closes_and_wipes(self):
        ctrl, popup, _ = _make()
        ctrl.session._idle_timeout = 0.0
        _open_chat(ctrl)
        _ch(ctrl, "mi")
        ctrl.tick()
        ctrl.pump()
        assert popup.visible is False
        assert ctrl.buffer.text == ""

    def test_nothing_is_captured_after_recovery(self):
        ctrl, popup, _ = _make()
        _open_chat(ctrl)
        ctrl.on_foreground_lost()
        _ch(ctrl, "mi")
        assert popup.visible is False
        assert ctrl.buffer.text == ""


class TestPrivacy:
    def test_reset_leaves_no_typed_text_behind(self):
        ctrl, _, _ = _make()
        _open_chat(ctrl)
        _ch(ctrl, "something private")
        _vk(ctrl, VK_RETURN)
        assert ctrl.buffer.text == ""
        assert ctrl._pending_line == ""
        assert ctrl._items == []
