"""Whole-sentence grammar correction for the line being typed.

The word-level suggester is fast but has no idea about articles, tense,
or agreement — it only ever sees one word. This module handles the rest
of the sentence using LanguageTool's public API, which knows real
grammar rules.

Two things keep it usable in a game:

  * it is only called after the user pauses typing (the controller
    debounces), never per keystroke, which also keeps us inside the free
    tier's ~20 requests/minute
  * results are cached, so repeating a callout you make every match is
    instant the second time

When the API is unreachable or rate-limits us, we fall back to the
EN->RU->EN round trip the Paste window's "Fix grammar" button already
uses. It rewrites rather than corrects, but it beats showing nothing.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable

from dota_ocr.suggest import Suggestion

_API_URL = "https://api.languagetool.org/v2/check"
_MIN_LENGTH = 6


def _round_trip(text: str) -> str | None:
    """EN -> RU -> EN through the translator already in the project."""
    from dota_ocr.translator import Translator

    t = Translator()
    pivot = t.translate(text, src="en", target_language="ru")
    if not pivot:
        return None
    return t.translate(pivot, src="ru", target_language="en")


class GrammarFixer:
    def __init__(
        self,
        session=None,
        fallback: Callable[[str], str | None] | None = None,
        api_url: str = _API_URL,
        timeout: float = 2.5,
        cache_size: int = 256,
    ) -> None:
        self._api_url = api_url
        self._timeout = timeout
        self._cache_size = cache_size
        self._cache: "OrderedDict[str, str | None]" = OrderedDict()
        self._fallback = fallback if fallback is not None else _round_trip
        if session is not None:
            self._session = session
        else:
            try:
                import requests
                self._session = requests.Session()
            except Exception:
                self._session = None

    def fix(self, text: str) -> str | None:
        """Return a corrected version, or None if unchanged/unavailable."""
        cleaned = text.strip()
        if len(cleaned) < _MIN_LENGTH:
            return None

        if cleaned in self._cache:
            self._cache.move_to_end(cleaned)
            return self._cache[cleaned]

        result = self._check(cleaned)
        if result is None:
            result = self._try_fallback(cleaned)

        if result is not None:
            result = result.strip()
        if result == cleaned or not result:
            result = None

        self._cache[cleaned] = result
        self._cache.move_to_end(cleaned)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    def suggest_line(self, text: str) -> list[Suggestion]:
        """The corrected sentence as a popup row, or nothing."""
        fixed = self.fix(text)
        if not fixed:
            return []
        return [Suggestion(fixed, "sentence", "line")]

    def _check(self, text: str) -> str | None:
        if self._session is None:
            return None
        try:
            resp = self._session.post(
                self._api_url,
                data={"text": text, "language": "en-US"},
                timeout=self._timeout,
            )
            if getattr(resp, "status_code", 200) != 200:
                # 429 is the free tier's rate limit; anything else is
                # equally unusable. Both go to the fallback.
                return None
            matches = (resp.json() or {}).get("matches") or []
        except Exception:
            return None

        # Apply right to left: rewriting an early span would invalidate
        # every offset that comes after it.
        out = text
        for m in sorted(matches, key=lambda m: m.get("offset", 0), reverse=True):
            reps = m.get("replacements") or []
            if not reps:
                continue
            value = reps[0].get("value")
            if value is None:
                continue
            start = int(m.get("offset", 0))
            end = start + int(m.get("length", 0))
            if start < 0 or end > len(out):
                continue
            out = out[:start] + value + out[end:]
        return out

    def _try_fallback(self, text: str) -> str | None:
        try:
            return self._fallback(text)
        except Exception:
            return None
