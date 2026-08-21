"""Tests for the suggestion popup's selection behaviour.

The navigation logic is what Tab acts on, so an off-by-one here inserts
a word the user didn't pick. These drive a real Tk window because the
selection state and the widget state have to stay in step.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

tk = pytest.importorskip("tkinter")

from dota_ocr.suggest import Suggestion
from dota_ocr.suggest_popup import SuggestPopup


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
        assert popup.selected() is None

    def test_show_makes_it_visible(self, popup):
        popup.show(ITEMS, 100, 100)
        assert popup.visible is True

    def test_showing_nothing_hides_it(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.show([], 100, 100)
        assert popup.visible is False

    def test_hide_clears_selection(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.hide()
        assert popup.visible is False
        assert popup.selected() is None

    def test_destroy_is_safe_twice(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.destroy()
        popup.destroy()
        assert popup.visible is False


class TestSelection:
    def test_first_item_is_selected_by_default(self, popup):
        popup.show(ITEMS, 100, 100)
        assert popup.selected().text == "mid"

    def test_next_walks_forward(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.move_next()
        assert popup.selected().text == "middle"

    def test_next_wraps_around(self, popup):
        popup.show(ITEMS, 100, 100)
        for _ in range(3):
            popup.move_next()
        assert popup.selected().text == "mid"

    def test_up_wraps_to_the_end(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.move_up()
        assert popup.selected().text == "might"

    def test_prev_matches_up(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.move_prev()
        assert popup.selected().text == "might"

    def test_navigation_while_hidden_is_safe(self, popup):
        popup.move_next()
        popup.move_up()
        assert popup.selected() is None


class TestRefresh:
    def test_refresh_keeps_the_highlight(self, popup):
        """A new keystroke must not slide a different word under Tab."""
        popup.show(ITEMS, 100, 100)
        popup.move_next()
        popup.show(ITEMS, 100, 100)
        assert popup.selected().text == "middle"

    def test_shorter_list_resets_an_out_of_range_highlight(self, popup):
        popup.show(ITEMS, 100, 100)
        popup.move_next()
        popup.move_next()
        popup.show(ITEMS[:1], 100, 100)
        assert popup.selected().text == "mid"

    def test_more_items_than_rows_are_capped(self, popup):
        many = [Suggestion(f"w{i}", "complete", "word") for i in range(20)]
        popup.show(many, 100, 100)
        assert popup.selected() is not None
        for _ in range(30):
            popup.move_next()
        assert popup.selected() is not None
