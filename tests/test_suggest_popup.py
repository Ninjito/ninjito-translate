"""Tests for the suggestion popup's rendering contract.

The popup is a pure renderer — the controller owns which row is
highlighted — so what matters here is that it shows and hides on the
right inputs, survives out-of-range indices, and never leaves a stale
window on screen. These drive a real Tk window.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

tk = pytest.importorskip("tkinter")

from dota_ocr.suggest import Suggestion
from dota_ocr.suggest_popup import MAX_ROWS, SuggestPopup


@pytest.fixture
def root():
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
def popup(root):
    p = SuggestPopup(root)
    yield p
    p.destroy()


ITEMS = [
    Suggestion("mid", "complete", "word"),
    Suggestion("middle", "complete", "word"),
    Suggestion("might", "fix", "word"),
]


class TestVisibility:
    def test_starts_hidden(self, popup):
        assert popup.visible is False

    def test_show_makes_it_visible(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        assert popup.visible is True

    def test_showing_nothing_hides_it(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        popup.show([], 0, 100, 100)
        assert popup.visible is False

    def test_hide_makes_it_invisible(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        popup.hide()
        assert popup.visible is False

    def test_hide_before_show_is_safe(self, popup):
        popup.hide()
        assert popup.visible is False

    def test_reshow_after_hide_works(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        popup.hide()
        popup.show(ITEMS, 1, 100, 100)
        assert popup.visible is True

    def test_destroy_is_safe_twice(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        popup.destroy()
        popup.destroy()
        assert popup.visible is False


class TestRendering:
    def test_one_row_per_item(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        packed = [r for r in popup._rows if r.winfo_manager()]
        assert len(packed) == len(ITEMS)

    def test_highlighted_row_gets_the_marker(self, popup):
        popup.show(ITEMS, 1, 100, 100)
        assert popup._rows[1].cget("text").startswith("▸")
        assert not popup._rows[0].cget("text").startswith("▸")

    def test_row_text_includes_the_kind_label(self, popup):
        popup.show([Suggestion("I need help", "sentence", "line")],
                   0, 100, 100)
        assert "[grammar]" in popup._rows[0].cget("text")

    def test_out_of_range_index_still_renders(self, popup):
        popup.show(ITEMS, 99, 100, 100)
        assert popup.visible is True

    def test_negative_index_still_renders(self, popup):
        popup.show(ITEMS, -1, 100, 100)
        assert popup.visible is True

    def test_more_items_than_rows_are_capped(self, popup):
        many = [Suggestion(f"w{i}", "complete", "word") for i in range(20)]
        popup.show(many, 0, 100, 100)
        packed = [r for r in popup._rows if r.winfo_manager()]
        assert len(packed) == MAX_ROWS

    def test_shrinking_the_list_drops_stale_rows(self, popup):
        popup.show(ITEMS, 0, 100, 100)
        popup.show(ITEMS[:1], 0, 100, 100)
        packed = [r for r in popup._rows if r.winfo_manager()]
        assert len(packed) == 1
