"""Word-level suggestions for the Dota chat box.

Two things happen per keystroke, both offline and both fast, because
this runs on every character the user types:

  * a fix    — edit-distance lookup against an English frequency
               dictionary, for the word as typed
  * a completion — prefix lookup, so a long word can be taken after
               three letters instead of typed out mid-fight

Dota's vocabulary is not in any English dictionary, so DOTA_WORDS is
merged in with weights high enough that 'rosh' completes to 'roshan'
rather than to something from the newswire corpus symspellpy ships.

Pure module — no ctypes, no Tk, no network.
"""

from __future__ import annotations

import bisect
import os
import sys
from dataclasses import dataclass

from symspellpy import SymSpell, Verbosity

# Weights are on the same scale as symspellpy's bundled corpus counts.
# 'roshan' at 6_000_000 beats generic words that share its prefix.
DOTA_WORDS: dict[str, int] = {
    "roshan": 6_000_000, "rosh": 4_000_000, "aegis": 5_000_000,
    "gank": 6_000_000, "ganking": 3_000_000, "ganked": 3_000_000,
    "creep": 5_000_000, "creeps": 5_000_000, "lane": 6_000_000,
    "lanes": 4_000_000, "mid": 8_000_000, "top": 7_000_000,
    "bot": 7_000_000, "jungle": 5_000_000, "offlane": 5_000_000,
    "safelane": 4_000_000, "carry": 6_000_000, "support": 6_000_000,
    "ward": 6_000_000, "wards": 6_000_000, "warding": 3_000_000,
    "smoke": 5_000_000, "dust": 4_000_000, "sentry": 4_000_000,
    "courier": 5_000_000, "buyback": 5_000_000, "bkb": 6_000_000,
    "blink": 5_000_000, "ulti": 5_000_000, "ultimate": 5_000_000,
    "cooldown": 5_000_000, "stun": 6_000_000, "silence": 4_000_000,
    "push": 7_000_000, "pushing": 4_000_000, "defend": 6_000_000,
    "throne": 5_000_000, "barracks": 5_000_000, "rax": 5_000_000,
    "tower": 7_000_000, "towers": 5_000_000, "farm": 6_000_000,
    "farming": 4_000_000, "feed": 5_000_000, "feeding": 4_000_000,
    "report": 6_000_000, "mute": 5_000_000, "pause": 5_000_000,
    "unpause": 4_000_000, "regroup": 5_000_000, "retreat": 6_000_000,
    "missing": 6_000_000, "care": 6_000_000, "back": 8_000_000,
    "come": 8_000_000, "help": 8_000_000, "wait": 7_000_000,
    "team": 8_000_000, "enemy": 7_000_000, "invis": 4_000_000,
    "illusion": 4_000_000, "illusions": 4_000_000, "runes": 5_000_000,
    "rune": 5_000_000, "bounty": 4_000_000, "shard": 4_000_000,
    "scepter": 4_000_000, "sorry": 7_000_000, "thanks": 7_000_000,
    "please": 7_000_000, "nice": 7_000_000, "gg": 8_000_000,
    "glhf": 6_000_000, "noob": 5_000_000, "smurf": 4_000_000,
}

_SCOPE_WORD = "word"

# An exact prefix match is far stronger evidence of intent than being
# two edits away, so completions outrank fixes of comparable frequency.
_COMPLETION_BOOST = 20

# 'a' and 'i' sit one edit from half the two-letter prefixes in the
# language and have enormous frequency, so without a floor they win
# every short lookup. Nobody needs help spelling them anyway.
_MIN_FIX_LENGTH = 3

# DOTA_WORDS weights are hand-ranked on a small scale; the bundled
# corpus counts run to billions. This lifts them onto the same scale so
# a Dota term wins its own prefix ('mid' over 'might').
_DOTA_BOOST = 250


@dataclass(frozen=True)
class Suggestion:
    """One row in the popup.

    `scope` decides what Tab replaces: the word before the caret, or
    the whole line.
    """

    text: str
    kind: str      # "fix" | "complete" | "sentence" | "translate"
    scope: str     # "word" | "line"


class Suggester:
    def __init__(
        self,
        words: dict[str, int] | None = None,
        max_results: int = 3,
        min_prefix: int = 2,
        max_edit_distance: int = 2,
    ) -> None:
        self.max_results = max_results
        self.min_prefix = min_prefix
        self._max_edit = max_edit_distance
        self._sym = SymSpell(max_dictionary_edit_distance=max_edit_distance,
                             prefix_length=7)

        if words is None:
            self._load_bundled()
            self._merge_dota_words()
        else:
            for word, count in words.items():
                self._sym.create_dictionary_entry(word, count)

        # Prefix completion runs per keystroke, so scanning all 82k
        # entries each time is wasteful. A sorted list plus bisect turns
        # it into a slice.
        self._sorted: list[str] = sorted(self._sym.words.keys())
        self._freq: dict[str, int] = dict(self._sym.words)

    def _merge_dota_words(self) -> None:
        """Fold the Dota vocabulary into the loaded English dictionary.

        Two traps here. `create_dictionary_entry` *sets* a count rather
        than adding to it, so writing a raw weight over a word the
        corpus already knows would demote it — 'mid' really is in the
        82k list, at 47M. And the corpus counts run to billions, so a
        weight has to be scaled onto that scale or the Dota term loses
        its own prefix to whatever generic word outranks it.
        """
        for word, weight in DOTA_WORDS.items():
            scaled = weight * _DOTA_BOOST
            if scaled > self._sym.words.get(word, 0):
                self._sym.create_dictionary_entry(word, scaled)

    def _load_bundled(self) -> None:
        """Load symspellpy's own 82k-word English frequency dictionary.

        The frozen build unpacks package data next to the exe rather
        than inside an importable package, so the resource lookup is
        tried first and the bundle layout second. Losing this file is
        not fatal — the Dota vocabulary still loads — but it degrades
        the feature to about 75 words, which looks broken rather than
        absent, so it is worth saying loudly.
        """
        for path in self._dictionary_candidates():
            try:
                if path and os.path.isfile(path):
                    self._sym.load_dictionary(path, term_index=0,
                                              count_index=1, encoding="utf-8")
                    return
            except Exception as e:
                print(f"[suggest] dictionary at {path} failed: {e}", flush=True)
        print("[suggest] English dictionary not found — only Dota terms "
              "will be suggested", flush=True)

    @staticmethod
    def _dictionary_candidates() -> list[str]:
        name = "frequency_dictionary_en_82_765.txt"
        out: list[str] = []
        try:
            import importlib.resources as res
            out.append(str(res.files("symspellpy") / name))
        except Exception:
            pass
        base = getattr(sys, "_MEIPASS", None)
        if base:
            out.append(os.path.join(base, "symspellpy", name))
        if getattr(sys, "frozen", False):
            out.append(os.path.join(os.path.dirname(sys.executable),
                                    "symspellpy", name))
        return out

    def suggest_word(
        self,
        prefix: str,
        *,
        fix: bool = True,
        complete: bool = True,
    ) -> list[Suggestion]:
        """Suggestions for the word currently being typed."""
        word = prefix.strip().lower()
        if len(word) < self.min_prefix:
            return []
        if not word.isascii() or not word.isalpha():
            # Cyrillic and mixed input belong to the translator path.
            return []

        known = word in self._freq
        fixes = self._fixes(word) if (fix and not known) else []
        completions = self._completions(word) if complete else []

        # Fixes and completions compete in one ranking rather than one
        # group leading the other. Grouping got both common cases wrong:
        # fixes-first buried 'retreat' under 'retro' for 'retre', and
        # completions-first offered 'tehran' ahead of 'the' for 'teh'.
        # Frequency settles both, with a boost for completions because
        # an exact prefix match is much stronger evidence than being two
        # edits away.
        ranked = sorted(completions + fixes, key=self._score, reverse=True)

        out: list[Suggestion] = []
        seen = {word}
        for s in ranked:
            if s.text in seen:
                continue
            seen.add(s.text)
            out.append(s)
            if len(out) >= self.max_results:
                break
        return out

    def _score(self, s: Suggestion) -> float:
        freq = float(self._freq.get(s.text, 0))
        return freq * _COMPLETION_BOOST if s.kind == "complete" else freq

    def _fixes(self, word: str) -> list[Suggestion]:
        try:
            hits = self._sym.lookup(word, Verbosity.CLOSEST,
                                    max_edit_distance=self._max_edit)
        except Exception:
            return []
        return [Suggestion(h.term, "fix", _SCOPE_WORD) for h in hits
                if h.term != word and len(h.term) >= _MIN_FIX_LENGTH]

    def _completions(self, word: str) -> list[Suggestion]:
        i = bisect.bisect_left(self._sorted, word)
        matches: list[str] = []
        while i < len(self._sorted) and self._sorted[i].startswith(word):
            if self._sorted[i] != word:
                matches.append(self._sorted[i])
            i += 1
            # A two-letter prefix can match thousands of words; we only
            # need enough to rank the top few.
            if len(matches) >= 400:
                break
        matches.sort(key=lambda w: self._freq.get(w, 0), reverse=True)
        return [Suggestion(w, "complete", _SCOPE_WORD)
                for w in matches[: self.max_results]]
