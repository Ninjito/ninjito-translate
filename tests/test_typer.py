"""Tests for injecting the accepted suggestion into Dota's chat box.

SendInput can't be observed from pytest, so these cover the guards and
the two ctypes details that make it fail silently. A zero or negative
backspace count must not fire anything — getting that wrong eats
characters out of the user's half-typed line during a fight.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dota_ocr.typer as typer
from dota_ocr.keyhook import SYNTHETIC_TAG


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, inputs):
        self.calls.append(inputs)


class TestStructLayout:
    def test_input_struct_is_the_size_windows_expects(self):
        """A keyboard-only union is 32 bytes and SendInput rejects every
        call with ERROR_INVALID_PARAMETER. The union must carry
        MOUSEINPUT to reach the real 40."""
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        assert ctypes.sizeof(typer._INPUT) == expected

    def test_every_event_carries_the_synthetic_tag(self):
        """Without the tag our own keystrokes feed back into the buffer."""
        ev = typer._key(typer.VK_BACK, 0, 0)
        assert ev.ki.dwExtraInfo == SYNTHETIC_TAG


class TestGuards:
    def test_zero_backspaces_sends_nothing(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_backspaces(0)
        assert rec.calls == []

    def test_negative_backspaces_sends_nothing(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_backspaces(-3)
        assert rec.calls == []

    def test_empty_text_sends_nothing(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_text("")
        assert rec.calls == []

    def test_replace_word_with_nothing_to_do_sends_nothing(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.replace_word(0, "")
        assert rec.calls == []


class TestBuilds:
    def test_backspaces_produce_down_up_pairs(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_backspaces(3)
        assert len(rec.calls) == 1
        assert len(rec.calls[0]) == 6      # 3 keys x (down + up)

    def test_text_produces_down_up_per_character(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_text("mid")
        assert len(rec.calls[0]) == 6

    def test_text_is_sent_as_unicode_not_virtual_keys(self, monkeypatch):
        """A VK path would produce Cyrillic on a Russian layout."""
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_text("m")
        first = rec.calls[0][0]
        assert first.ki.wVk == 0
        assert first.ki.wScan == ord("m")
        assert first.ki.dwFlags & typer.KEYEVENTF_UNICODE

    def test_non_ascii_text_survives(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.send_text("привет")
        assert len(rec.calls[0]) == 12
        assert rec.calls[0][0].ki.wScan == ord("п")

    def test_replace_word_sends_backspaces_then_text(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(typer, "_send", rec)
        typer.replace_word(2, "mid")
        assert len(rec.calls) == 2
        assert len(rec.calls[0]) == 4      # 2 backspaces
        assert len(rec.calls[1]) == 6      # 3 characters
