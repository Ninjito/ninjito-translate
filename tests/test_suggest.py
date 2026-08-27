"""Tests for word-level suggestions.

Ranking matters more than raw lookup here: the popup shows three slots,
and a Dota term losing its slot to a common English word is the failure
mode that makes the feature useless in a real game.

Tests inject a tiny dictionary so they stay fast and deterministic —
the real 82k-entry dictionary is loaded only when `words` is None.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dota_ocr.suggest import Suggester, Suggestion


WORDS = {
    # 'a' and 'i' carry huge frequency and sit one edit from most short
    # prefixes — exactly the trap the fix-length floor exists for.
    "a": 90000, "i": 80000,
    "the": 10000, "they": 900, "them": 800, "there": 700,
    "go": 5000, "good": 4000, "gold": 900, "gone": 500,
    "gank": 300, "gang": 200,
    "receive": 400, "recover": 350,
    "mid": 600, "middle": 100,
    "roshan": 250, "rosh": 50,
    "push": 500, "pull": 450,
}


@pytest.fixture
def sug():
    return Suggester(words=WORDS, max_results=3, min_prefix=2)


class TestFixes:
    def test_misspelling_is_corrected(self, sug):
        out = [s.text for s in sug.suggest_word("recieve")]
        assert "receive" in out

    def test_transposition_is_corrected(self, sug):
        out = [s.text for s in sug.suggest_word("teh")]
        assert "the" in out

    def test_fix_is_tagged_as_fix(self, sug):
        s = next(s for s in sug.suggest_word("recieve") if s.text == "receive")
        assert s.kind == "fix"
        assert s.scope == "word"

    def test_fixes_can_be_disabled(self, sug):
        out = [s.text for s in sug.suggest_word("recieve", fix=False)]
        assert "receive" not in out


class TestCompletions:
    def test_prefix_completes(self, sug):
        out = [s.text for s in sug.suggest_word("rosh")]
        assert "roshan" in out

    def test_completion_is_tagged(self, sug):
        s = next(s for s in sug.suggest_word("rosh") if s.text == "roshan")
        assert s.kind == "complete"

    def test_completions_ranked_by_frequency(self, sug):
        out = [s.text for s in sug.suggest_word("go", fix=False)]
        assert out.index("good") < out.index("gone")

    def test_completions_can_be_disabled(self, sug):
        out = [s.text for s in sug.suggest_word("rosh", complete=False)]
        assert "roshan" not in out

    def test_known_word_still_offers_longer_completions(self, sug):
        out = [s.text for s in sug.suggest_word("mid")]
        assert "middle" in out


class TestRanking:
    def test_one_and_two_letter_fixes_are_suppressed(self):
        """Nobody typing 'mi' wants to be offered 'i'."""
        s = Suggester(words=WORDS, max_results=3, min_prefix=2)
        out = [x.text for x in s.suggest_word("mi")]
        assert "i" not in out
        assert "a" not in out
        assert "mid" in out

    def test_completion_outranks_a_comparable_fix(self):
        s = Suggester(words=WORDS, max_results=3, min_prefix=2)
        out = [x.text for x in s.suggest_word("mid")]
        assert out[0] == "middle"

    def test_a_much_more_common_fix_still_wins(self):
        """'teh' means 'the', not some rare word starting with t-e-h."""
        words = dict(WORDS)
        words["tehran"] = 5
        s = Suggester(words=words, max_results=3, min_prefix=2)
        out = [x.text for x in s.suggest_word("teh")]
        assert out[0] == "the"


class TestGuards:
    def test_short_prefix_returns_nothing(self, sug):
        assert sug.suggest_word("g") == []

    def test_empty_prefix_returns_nothing(self, sug):
        assert sug.suggest_word("") == []

    def test_exact_word_is_never_suggested_back(self, sug):
        out = [s.text for s in sug.suggest_word("mid")]
        assert "mid" not in out

    def test_respects_max_results(self, sug):
        assert len(sug.suggest_word("the")) <= 3

    def test_no_duplicates(self, sug):
        out = [s.text for s in sug.suggest_word("gan")]
        assert len(out) == len(set(out))

    def test_non_ascii_prefix_returns_nothing(self, sug):
        """Cyrillic goes to the translator, not the English dictionary."""
        assert sug.suggest_word("прив") == []

    def test_digits_return_nothing(self, sug):
        assert sug.suggest_word("gg2") == []


class TestRealDictionary:
    def test_bundled_dictionary_loads_and_knows_dota_terms(self):
        s = Suggester()
        assert "roshan" in [x.text for x in s.suggest_word("rosha")]

    def test_bundled_dictionary_corrects_real_english(self):
        s = Suggester()
        assert "receive" in [x.text for x in s.suggest_word("recieve")]


class TestIndexSize:
    """prefix_length is a memory/CPU trade, never a quality trade.

    It shrank the deletion index from 127 MB to 38 MB. These pin the
    behaviour that made that safe, so a future bump back to 7 has to
    justify the memory rather than the results.
    """

    def test_prefix_length_is_the_tuned_value(self):
        from dota_ocr.suggest import Suggester
        assert Suggester()._sym._prefix_length == 5

    @pytest.mark.parametrize("typo,want", [
        ("teh", "the"),          # transposition
        ("smoek", "smoke"),      # transposition, Dota term
        ("rosahn", "roshan"),    # two edits, Dota term
        ("jungel", "jungle"),    # transposition
        ("recieve", "receive"),  # the classic
        ("defnd", "defend"),     # deletion
    ])
    def test_corrections_survive_the_smaller_index(self, typo, want):
        from dota_ocr.suggest import Suggester
        got = [s.text for s in Suggester().suggest_word(typo, fix=True,
                                                        complete=True)]
        assert want in got, f"{typo!r} -> {got}"
