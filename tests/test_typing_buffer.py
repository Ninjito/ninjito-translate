"""Tests for reconstructing Dota's chat input from raw keystrokes.

Dota exposes no way to read its chat box, so this buffer IS our only
model of what the user sees. Every drift between the two produces a
suggestion for a word the user isn't typing, or a Tab that eats the
wrong characters.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.typing_buffer import (
    KeyEvent, TypingBuffer,
    VK_BACK, VK_DELETE, VK_LEFT, VK_RIGHT, VK_HOME, VK_END,
)


def _type(buf: TypingBuffer, text: str) -> None:
    for ch in text:
        buf.apply(KeyEvent(vk=ord(ch.upper()), down=True, char=ch,
                           shift=False, ctrl=False, alt=False))


def _key(buf: TypingBuffer, vk: int, ctrl: bool = False) -> bool:
    return buf.apply(KeyEvent(vk=vk, down=True, char="",
                              shift=False, ctrl=ctrl, alt=False))


class TestTyping:
    def test_printable_chars_append(self):
        buf = TypingBuffer()
        _type(buf, "gank mid")
        assert buf.text == "gank mid"
        assert buf.cursor == 8

    def test_apply_reports_change(self):
        buf = TypingBuffer()
        assert buf.apply(KeyEvent(vk=ord("A"), down=True, char="a",
                                  shift=False, ctrl=False, alt=False)) is True
        assert _key(buf, VK_LEFT) is False   # caret move is not a text change
        assert _key(buf, VK_BACK) is False   # nothing before the caret

    def test_key_up_is_ignored(self):
        buf = TypingBuffer()
        _type(buf, "gg")
        buf.apply(KeyEvent(vk=ord("G"), down=False, char="g",
                           shift=False, ctrl=False, alt=False))
        assert buf.text == "gg"

    def test_ctrl_letter_is_a_command_not_text(self):
        buf = TypingBuffer()
        buf.apply(KeyEvent(vk=ord("A"), down=True, char="a",
                           shift=False, ctrl=True, alt=False))
        assert buf.text == ""

    def test_cyrillic_char_is_kept(self):
        buf = TypingBuffer()
        _type(buf, "привет")
        assert buf.text == "привет"


class TestEditing:
    def test_backspace_removes_before_cursor(self):
        buf = TypingBuffer()
        _type(buf, "gankk")
        _key(buf, VK_BACK)
        assert buf.text == "gank"
        assert buf.cursor == 4

    def test_backspace_at_start_is_noop(self):
        buf = TypingBuffer()
        assert _key(buf, VK_BACK) is False
        assert buf.text == ""

    def test_delete_removes_after_cursor(self):
        buf = TypingBuffer()
        _type(buf, "gank")
        _key(buf, VK_HOME)
        _key(buf, VK_DELETE)
        assert buf.text == "ank"
        assert buf.cursor == 0

    def test_delete_at_end_is_noop(self):
        buf = TypingBuffer()
        _type(buf, "gg")
        assert _key(buf, VK_DELETE) is False
        assert buf.text == "gg"

    def test_ctrl_backspace_deletes_word(self):
        buf = TypingBuffer()
        _type(buf, "push mid now")
        _key(buf, VK_BACK, ctrl=True)
        assert buf.text == "push mid "

    def test_ctrl_backspace_eats_trailing_space_too(self):
        buf = TypingBuffer()
        _type(buf, "push mid ")
        _key(buf, VK_BACK, ctrl=True)
        assert buf.text == "push "

    def test_insert_at_cursor(self):
        buf = TypingBuffer()
        _type(buf, "gank")
        _key(buf, VK_HOME)
        _type(buf, "b")
        assert buf.text == "bgank"
        assert buf.cursor == 1


class TestCursor:
    def test_left_right_clamp(self):
        buf = TypingBuffer()
        _type(buf, "gg")
        _key(buf, VK_RIGHT)
        assert buf.cursor == 2
        _key(buf, VK_LEFT); _key(buf, VK_LEFT); _key(buf, VK_LEFT)
        assert buf.cursor == 0

    def test_home_and_end(self):
        buf = TypingBuffer()
        _type(buf, "gank mid")
        _key(buf, VK_HOME)
        assert buf.cursor == 0
        _key(buf, VK_END)
        assert buf.cursor == 8


class TestCurrentWord:
    def test_word_before_cursor(self):
        buf = TypingBuffer()
        _type(buf, "push mi")
        assert buf.current_word() == ("mi", 5)

    def test_empty_after_space(self):
        buf = TypingBuffer()
        _type(buf, "push ")
        assert buf.current_word() == ("", 5)

    def test_word_respects_cursor_position(self):
        buf = TypingBuffer()
        _type(buf, "push mid")
        _key(buf, VK_HOME)
        assert buf.current_word() == ("", 0)

    def test_apostrophe_stays_inside_the_word(self):
        buf = TypingBuffer()
        _type(buf, "dont")
        assert buf.current_word() == ("dont", 0)

    def test_set_current_word_replaces_in_place(self):
        buf = TypingBuffer()
        _type(buf, "push mi")
        buf.set_current_word("mid")
        assert buf.text == "push mid"
        assert buf.cursor == 8

    def test_set_current_word_keeps_the_tail(self):
        buf = TypingBuffer()
        _type(buf, "push mi now")
        for _ in range(4):
            _key(buf, VK_LEFT)
        buf.set_current_word("mid")
        assert buf.text == "push mid now"


class TestReset:
    def test_reset_clears_everything(self):
        buf = TypingBuffer()
        _type(buf, "secret text")
        buf.reset()
        assert buf.text == ""
        assert buf.cursor == 0
