"""Tests for the low-level keyboard hook wrapper.

An installed OS hook can't be exercised from pytest, so what's pinned
down here is the contract around it: importing must never raise, a
failed install must report instead of throwing, and stopping a hook
that never started must be safe. The hook callback's real behaviour is
verified by the manual in-game checklist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.keyhook import KeyboardHook, SYNTHETIC_TAG, _decode


def _hook():
    return KeyboardHook(on_event=lambda ev: None,
                        should_swallow=lambda ev: False)


class TestModule:
    def test_synthetic_tag_is_a_nonzero_int(self):
        assert isinstance(SYNTHETIC_TAG, int)
        assert SYNTHETIC_TAG != 0

    def test_decode_never_raises_on_junk_input(self):
        assert isinstance(_decode(0, 0), str)
        assert isinstance(_decode(0xFFFF, 0xFFFF), str)


class TestLifecycle:
    def test_stop_without_start_is_safe(self):
        hook = _hook()
        hook.stop()
        assert hook.is_running() is False

    def test_double_stop_is_safe(self):
        hook = _hook()
        hook.stop()
        hook.stop()
        assert hook.is_running() is False

    def test_start_twice_is_idempotent(self):
        hook = _hook()
        try:
            first = hook.start()
            second = hook.start()
            assert second == first
        finally:
            hook.stop()

    def test_start_then_stop_leaves_it_not_running(self):
        hook = _hook()
        hook.start()
        hook.stop()
        assert hook.is_running() is False

    def test_restart_after_stop_works(self):
        hook = _hook()
        started = hook.start()
        hook.stop()
        try:
            assert hook.start() == started
        finally:
            hook.stop()

    def test_failed_start_reports_a_reason(self):
        hook = _hook()
        try:
            if not hook.start():
                assert hook.last_error != ""
        finally:
            hook.stop()
