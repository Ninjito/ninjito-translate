"""Tests for the overlay's voice-line handling.

The overlay is a Tk window with hotkey threads, so instead of building a
real one these tests bind the actual Overlay methods onto a lightweight
stub.  That exercises the real logic — which messages survive clear(),
which colour tag each line gets — without opening a window.
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.overlay import VOICE_PREFIX, Overlay


class FakeText:
    """Records what would have been drawn into the Tk Text widget."""

    def __init__(self):
        self.inserted: list[tuple[str, str]] = []   # (text, tag)
        self.state = "disabled"

    def configure(self, **kw):
        self.state = kw.get("state", self.state)

    def delete(self, *_a):
        self.inserted.clear()

    def insert(self, _index, text, tag=None):
        self.inserted.append((text, tag))

    def see(self, *_a):
        pass


class FakeOverlay:
    """Stub carrying the real methods under test."""

    _is_voice = Overlay.__dict__["_is_voice"]
    clear = Overlay.__dict__["clear"]
    _render = Overlay.__dict__["_render"]
    _drain = Overlay.__dict__["_drain"]

    def __init__(self, messages=None, cfg=None):
        self._messages = list(messages or [])
        self.text = FakeText()
        self.autosize_calls = 0
        self._cfg = cfg or {}
        self._msg_queue = queue.Queue()
        self.max_messages = 50
        self._closing = True        # stop _drain rescheduling itself
        self.root = None

    def _autosize_to_messages(self, visible=5):
        self.autosize_calls += 1


def voice_msg(ru: str, en: str, ts: float = 0.0) -> tuple[str, str, float]:
    return (f"{VOICE_PREFIX} {ru}", f"{VOICE_PREFIX} {en}", ts)


def chat_msg(ru: str, en: str, ts: float = 0.0) -> tuple[str, str, float]:
    return (ru, en, ts)


# --------------------------------------------------------------------------
# clear() must not wipe voice lines
# --------------------------------------------------------------------------

class TestClearPreservesVoice:
    def test_chat_lines_cleared(self):
        o = FakeOverlay([chat_msg("[Allies] Ninja: привет", "[Allies] Ninja: hello")])
        o.clear()
        assert o._messages == []

    def test_voice_lines_survive(self):
        """Voice arrives on its own schedule; an F7 chat capture must not
        make spoken translations vanish."""
        v = voice_msg("они на рошане", "they are at roshan")
        o = FakeOverlay([v])
        o.clear()
        assert o._messages == [v]

    def test_mixed_keeps_only_voice(self):
        v = voice_msg("отступаем", "retreat")
        chat = chat_msg("[Allies] Ninja: гг", "[Allies] Ninja: gg")
        o = FakeOverlay([chat, v, chat])
        o.clear()
        assert o._messages == [v]

    def test_surviving_voice_lines_are_repainted(self):
        """After clearing, kept lines must be drawn again — otherwise the
        widget is blank while _messages says otherwise."""
        v = voice_msg("они на рошане", "they are at roshan")
        o = FakeOverlay([v])
        o.clear()
        assert o.text.inserted == [(f"{VOICE_PREFIX} they are at roshan\n", "voice")]

    def test_empty_after_clear_still_autosizes(self):
        """With nothing left, the overlay must shrink back down."""
        o = FakeOverlay([chat_msg("[Allies] Ninja: гг", "[Allies] Ninja: gg")])
        o.clear()
        assert o.autosize_calls == 1


# --------------------------------------------------------------------------
# message expiry
# --------------------------------------------------------------------------

class TestMessageExpiry:
    """Voice lines are never part of an OCR batch, so clear() deliberately
    keeps them — which meant they stayed on screen for the rest of the
    match. They now age out on their own."""

    def test_old_lines_are_dropped(self):
        now = time.monotonic()
        o = FakeOverlay([voice_msg("старое", "old", now - 10)],
                        cfg={"message_ttl_sec": 7})
        o._drain()
        assert o._messages == []

    def test_fresh_lines_are_kept(self):
        now = time.monotonic()
        fresh = voice_msg("новое", "new", now - 2)
        o = FakeOverlay([fresh], cfg={"message_ttl_sec": 7})
        o._drain()
        assert o._messages == [fresh]

    def test_only_expired_lines_go(self):
        now = time.monotonic()
        old = voice_msg("старое", "old", now - 30)
        fresh = voice_msg("новое", "new", now - 1)
        o = FakeOverlay([old, fresh], cfg={"message_ttl_sec": 7})
        o._drain()
        assert o._messages == [fresh]

    def test_zero_ttl_disables_expiry(self):
        old = voice_msg("старое", "old", time.monotonic() - 999)
        o = FakeOverlay([old], cfg={"message_ttl_sec": 0})
        o._drain()
        assert o._messages == [old]

    def test_expiry_repaints_the_widget(self):
        """Dropping a line must redraw, or the text stays on screen while
        _messages says it's gone."""
        now = time.monotonic()
        o = FakeOverlay([voice_msg("старое", "old", now - 10),
                         voice_msg("новое", "new", now - 1)],
                        cfg={"message_ttl_sec": 7})
        o._drain()
        assert o.text.inserted == [(f"{VOICE_PREFIX} new\n", "voice")]

    def test_queued_messages_get_a_timestamp(self):
        o = FakeOverlay(cfg={"message_ttl_sec": 7})
        o._msg_queue.put(("привет", "hello"))
        o._drain()
        assert len(o._messages) == 1
        assert o._messages[0][:2] == ("привет", "hello")
        assert o._messages[0][2] > 0


# --------------------------------------------------------------------------
# _is_voice
# --------------------------------------------------------------------------

class TestIsVoice:
    def test_detects_prefix(self):
        assert Overlay._is_voice(f"{VOICE_PREFIX} привет") is True

    def test_tolerates_leading_space(self):
        assert Overlay._is_voice(f"  {VOICE_PREFIX} привет") is True

    def test_plain_chat_is_not_voice(self):
        assert Overlay._is_voice("[Allies] Ninja: привет") is False

    def test_empty_is_not_voice(self):
        assert Overlay._is_voice("") is False


# --------------------------------------------------------------------------
# render tagging
# --------------------------------------------------------------------------

class TestRenderTags:
    def test_voice_line_gets_voice_tag(self):
        o = FakeOverlay([voice_msg("отступаем", "retreat")])
        o._render()
        assert o.text.inserted[0][1] == "voice"

    def test_allies_line_gets_allies_tag(self):
        o = FakeOverlay([chat_msg("[Allies] Ninja: гг", "[Allies] Ninja: gg")])
        o._render()
        assert o.text.inserted[0][1] == "allies"

    def test_all_chat_gets_all_tag(self):
        o = FakeOverlay([chat_msg("Ninja: гг", "Ninja: gg")])
        o._render()
        assert o.text.inserted[0][1] == "all"

    def test_voice_wins_over_channel_keywords(self):
        """A spoken line quoting '[Allies]' must still read as voice."""
        o = FakeOverlay([voice_msg("[allies] что", "[allies] what")])
        o._render()
        assert o.text.inserted[0][1] == "voice"

    def test_renders_only_last_five(self):
        msgs = [voice_msg(f"строка {i}", f"line {i}") for i in range(8)]
        o = FakeOverlay(msgs)
        o._render()
        assert len(o.text.inserted) == 5
        assert o.text.inserted[0][0] == f"{VOICE_PREFIX} line 3\n"
