"""Tests for splitting a Dota chat line into (prefix, message body).

Every case here is a real line captured from `logs/app.log` during a
live match, where a mis-split silently lost a Russian message.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dota_ocr.postprocess import split_chat_line


class TestSplitChatLine:
    """The separator colon is the first one OUTSIDE any [...] group.

    OCR mangles the coloured clan tag ('[nc1x]' -> '[:Я В Аи]'), so the
    first colon in the raw text often sits inside the brackets. Splitting
    there drags the garbled name into the body, which then trips the junk
    filter, poisons the translation input, and leaves Cyrillic in the
    output that gets the line rejected.
    """

    @pytest.mark.parametrize("line,body", [
        # --- the failures from the live log -------------------------------
        ("[Allies] <5 westgoths. [:Я В Аи] : тут без шансов",
         "тут без шансов"),
        ("[Allies] -;     £ westgoths.  [:FЕВR А 51 : тут без шансов",
         "тут без шансов"),
        ("[Allies] << westgoths. [:# ВВ Я хт] : даже теоритических",
         "даже теоритических"),
        ("[Allies] -\\< westgoths. [: ЯЕВЕ Я иz] : даже теоритических",
         "даже теоритических"),
        # --- clean lines must keep working --------------------------------
        ("[Allies] kakuja [пс1х] : » Need 1312 gold for",
         "» Need 1312 gold for"),
        ("[Allies] Player: иди мид", "иди мид"),
        ("[All] Someone [CLAN]: gg wp", "gg wp"),
    ])
    def test_body_excludes_name_garbage(self, line, body):
        assert split_chat_line(line)[1] == body

    def test_prefix_keeps_the_whole_name(self):
        prefix, _ = split_chat_line("[Allies] kakuja [пс1х] : hello")
        assert prefix.startswith("[Allies]")
        assert prefix.endswith(":")

    def test_body_only_continuation(self):
        """OCR dropped the coloured name half and left just ': message'."""
        assert split_chat_line(": тут без шансов")[1] == "тут без шансов"

    def test_no_colon_returns_whole_text(self):
        assert split_chat_line("тут без шансов") == ("", "тут без шансов")

    def test_unclosed_bracket_still_splits(self):
        """A '[' with no ']' must not swallow the rest of the line."""
        assert split_chat_line("[Allies] name [clan : иди мид")[1] == "иди мид"

    def test_colon_inside_body_is_kept(self):
        """Only the *separator* colon is consumed; later ones belong to
        the message."""
        assert split_chat_line("[All] p [C]: 10:00 пуш")[1] == "10:00 пуш"

    def test_empty_input(self):
        assert split_chat_line("") == ("", "")
