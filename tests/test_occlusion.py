"""The gate that decides whether cheap screen capture may be used.

Getting this wrong in the permissive direction OCRs whatever window is
sitting over Dota's chat — the exact bug PrintWindow was introduced to
fix. So every case that cannot be positively ruled out must come back
False, and these tests pin that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Win32-only window logic")

import dota_ocr.window as W

DOTA = 1000
RECT = (100, 100, 200, 50)          # l, t, w, h  -> covers x 100-300, y 100-150


class FakeRect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class FakeUser32:
    """Just enough of user32 for the decision logic."""

    def __init__(self, foreground=DOTA, rects=None, visible=None, iconic=None):
        self._fg = foreground
        self._rects = rects or {}
        self._visible = visible or {}
        self._iconic = iconic or {}

    def GetForegroundWindow(self):
        return self._fg

    def IsIconic(self, hwnd):
        return self._iconic.get(hwnd, False)

    def IsWindowVisible(self, hwnd):
        return self._visible.get(hwnd, True)

    def GetWindowRect(self, hwnd, out):
        r = self._rects.get(hwnd)
        if r is None:
            return False
        out._obj.left, out._obj.top, out._obj.right, out._obj.bottom = r
        return True


@pytest.fixture
def env(monkeypatch):
    """Wire the module to fakes; returns a configure() helper."""
    def configure(above=(), rects=None, foreground=DOTA, visible=None,
                  iconic=None, cloaked=(), classes=None):
        import ctypes
        fake = FakeUser32(foreground, rects, visible, iconic)

        # byref() on our fake struct: hand back something with ._obj
        class Ref:
            def __init__(self, obj):
                self._obj = obj

        monkeypatch.setattr(W, "_user32", fake)
        monkeypatch.setattr(W.ctypes, "byref", Ref)
        monkeypatch.setattr(W.wintypes, "RECT",
                            lambda: type("R", (), {"left": 0, "top": 0,
                                                   "right": 0, "bottom": 0})())
        monkeypatch.setattr(W, "_windows_above", lambda h: list(above))
        monkeypatch.setattr(W, "_is_cloaked", lambda h: h in cloaked)
        monkeypatch.setattr(W, "_class_name",
                            lambda h: (classes or {}).get(h, "SomeApp"))
    return configure


def test_clear_region_is_usable(env):
    env(above=())
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_window_over_the_region_blocks_it(env):
    env(above=[7], rects={7: (150, 110, 250, 140)})
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_window_elsewhere_does_not_block(env):
    env(above=[7], rects={7: (900, 900, 1000, 950)})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_edge_touching_does_not_count_as_overlap(env):
    # Right edge exactly at the region's left edge — adjacent, not over.
    env(above=[7], rects={7: (0, 100, 100, 150)})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_one_pixel_of_overlap_blocks(env):
    env(above=[7], rects={7: (0, 100, 101, 150)})
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_background_dota_is_never_trusted(env):
    env(above=(), foreground=999)
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_minimized_dota_is_never_trusted(env):
    env(above=(), iconic={DOTA: True})
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_hidden_window_is_ignored(env):
    env(above=[7], rects={7: (150, 110, 250, 140)}, visible={7: False})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_minimized_window_is_ignored(env):
    env(above=[7], rects={7: (150, 110, 250, 140)}, iconic={7: True})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_cloaked_window_is_ignored(env):
    """A suspended UWP app is visible by flag but draws nothing."""
    env(above=[7], rects={7: (150, 110, 250, 140)}, cloaked=(7,))
    assert W.region_is_unoccluded(DOTA, RECT) is True


@pytest.mark.parametrize("cls", ["Progman", "WorkerW"])
def test_desktop_hosts_are_ignored(env, cls):
    """Desktop-sized and always visible, but painted behind everything."""
    env(above=[7], rects={7: (-1680, 0, 1920, 1080)}, classes={7: cls})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_zero_size_window_is_ignored(env):
    env(above=[7], rects={7: (150, 110, 150, 110)})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_unreadable_rect_is_ignored(env):
    """GetWindowRect failing on one window must not veto the whole check."""
    env(above=[7], rects={})
    assert W.region_is_unoccluded(DOTA, RECT) is True


def test_second_window_still_checked_after_a_skip(env):
    env(above=[7, 8], rects={8: (150, 110, 250, 140)}, visible={7: False})
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_unknown_failure_falls_back_to_printwindow(env, monkeypatch):
    env(above=(),)
    monkeypatch.setattr(W, "_windows_above",
                        lambda h: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        W._windows_above(DOTA)
    # region_is_unoccluded swallows it and answers conservatively.
    monkeypatch.setattr(W, "_windows_above", lambda h: [7])
    monkeypatch.setattr(W, "_is_cloaked",
                        lambda h: (_ for _ in ()).throw(OSError("boom")))
    assert W.region_is_unoccluded(DOTA, RECT) is False


def test_no_hwnd_is_never_trusted(env):
    env(above=())
    assert W.region_is_unoccluded(0, RECT) is False
