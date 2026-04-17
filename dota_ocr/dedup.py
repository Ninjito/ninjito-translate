"""Message deduplicator.

Dota 2 chat lingers on screen for several seconds, so the same message
will appear in many consecutive frames. We also get slightly different
OCR transcriptions of the same text between frames (one extra space, a
character flipped, etc.). Exact-match deduping is therefore not enough:
we use a fuzzy ratio via difflib.
"""

from __future__ import annotations

from collections import deque
from difflib import SequenceMatcher


class MessageDeduplicator:
    def __init__(self, maxlen: int = 60, similarity_threshold: float = 0.85):
        self._recent: "deque[str]" = deque(maxlen=maxlen)
        self._threshold = similarity_threshold

    def is_new(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        for prev in self._recent:
            if SequenceMatcher(None, prev, text).ratio() >= self._threshold:
                return False
        self._recent.append(text)
        return True
