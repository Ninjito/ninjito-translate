"""Translation wrapper around deep-translator's GoogleTranslator.

- Auto source detection (`source="auto"`) so users don't need to declare
  the chat language up front.
- In-memory cache so the same line never hits the network twice.
- Silent fallback: on network errors the original text is returned so the
  overlay still shows *something* rather than crashing the worker.
"""

from __future__ import annotations

from typing import Dict, Tuple

from deep_translator import GoogleTranslator


class Translator:
    def __init__(self, target: str = "en"):
        self.target = target
        self._cache: Dict[Tuple[str, str], str] = {}

    def translate(self, text: str, src: str = "auto") -> str:
        if not text or len(text.strip()) < 2:
            return text
        key = (text, src)
        if key in self._cache:
            return self._cache[key]
        try:
            out = GoogleTranslator(source=src, target=self.target).translate(text)
        except Exception:
            out = None
        result = out if out else text
        self._cache[key] = result
        return result
