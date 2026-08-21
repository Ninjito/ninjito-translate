"""Tests for tracking whether Dota's chat box is open.

We infer this from keystrokes alone, so a missed transition leaves the
app capturing keys that are really going to the game as commands. The
foreground-loss and idle-timeout paths exist purely to recover from
that, and are the cases most worth pinning down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.chat_session import ChatSession, ChatState
from dota_ocr.typing_buffer import KeyEvent, VK_RETURN, VK_ESCAPE


def _k(vk, shift=False, char=""):
    return KeyEvent(vk=vk, down=True, char=char, shift=shift,
                    ctrl=False, alt=False)


class TestOpening:
    def test_enter_opens_team_chat(self):
        s = ChatSession()
        assert s.state is ChatState.CLOSED
        assert s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0) is True
        assert s.state is ChatState.TEAM
        assert s.is_open is True

    def test_shift_enter_opens_all_chat(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN, shift=True), popup_visible=False, now=0.0)
        assert s.state is ChatState.ALL

    def test_letters_do_not_open_chat(self):
        s = ChatSession()
        s.on_key(_k(ord("Q"), char="q"), popup_visible=False, now=0.0)
        assert s.state is ChatState.CLOSED

    def test_escape_while_closed_is_noop(self):
        s = ChatSession()
        assert s.on_key(_k(VK_ESCAPE), popup_visible=False, now=0.0) is False
        assert s.state is ChatState.CLOSED

    def test_key_up_is_ignored(self):
        s = ChatSession()
        up = KeyEvent(vk=VK_RETURN, down=False, char="", shift=False,
                      ctrl=False, alt=False)
        assert s.on_key(up, popup_visible=False, now=0.0) is False
        assert s.state is ChatState.CLOSED


class TestClosing:
    def test_enter_sends_and_closes(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0)
        assert s.on_key(_k(VK_RETURN), popup_visible=False, now=1.0) is True
        assert s.state is ChatState.CLOSED

    def test_escape_closes_when_popup_hidden(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0)
        s.on_key(_k(VK_ESCAPE), popup_visible=False, now=1.0)
        assert s.state is ChatState.CLOSED

    def test_escape_does_not_close_when_popup_visible(self):
        """The popup swallows that Esc, so Dota never sees it."""
        s = ChatSession()
        s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0)
        assert s.on_key(_k(VK_ESCAPE), popup_visible=True, now=1.0) is False
        assert s.state is ChatState.TEAM

    def test_second_escape_closes_once_popup_is_gone(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0)
        s.on_key(_k(VK_ESCAPE), popup_visible=True, now=1.0)
        s.on_key(_k(VK_ESCAPE), popup_visible=False, now=2.0)
        assert s.state is ChatState.CLOSED

    def test_all_chat_closes_the_same_way(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN, shift=True), popup_visible=False, now=0.0)
        s.on_key(_k(VK_RETURN), popup_visible=False, now=1.0)
        assert s.state is ChatState.CLOSED


class TestRecovery:
    def test_foreground_loss_closes(self):
        s = ChatSession()
        s.on_key(_k(VK_RETURN), popup_visible=False, now=0.0)
        assert s.on_foreground_lost() is True
        assert s.state is ChatState.CLOSED

    def test_foreground_loss_while_closed_is_noop(self):
        s = ChatSession()
        assert s.on_foreground_lost() is False

    def test_idle_timeout_closes(self):
        s = ChatSession(idle_timeout=20.0)
        s.on_key(_k(VK_RETURN), popup_visible=False, now=100.0)
        assert s.tick(now=115.0) is False
        assert s.state is ChatState.TEAM
        assert s.tick(now=121.0) is True
        assert s.state is ChatState.CLOSED

    def test_typing_refreshes_the_idle_timer(self):
        s = ChatSession(idle_timeout=20.0)
        s.on_key(_k(VK_RETURN), popup_visible=False, now=100.0)
        s.on_key(_k(ord("G"), char="g"), popup_visible=False, now=115.0)
        assert s.tick(now=130.0) is False
        assert s.state is ChatState.TEAM

    def test_tick_while_closed_is_noop(self):
        s = ChatSession(idle_timeout=20.0)
        assert s.tick(now=99999.0) is False
