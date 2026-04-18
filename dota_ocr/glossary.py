"""Custom glossary for consistent term mapping.

Load glossary.json next to config.json. Format:

    {
      "спс": "thanks",
      "гг": "gg",
      "аускейв": "skill issue",
      ...
    }

These are applied as simple string replacements *before* translation.
Useful for slang, abbreviations, hero names, item names that the
translator might mangle or mistranslate.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


GLOSSARY_FILE = _app_dir() / "glossary.json"


def load() -> Dict[str, str]:
    """Load glossary.json, return empty dict if missing or malformed."""
    if not GLOSSARY_FILE.is_file():
        return {}
    try:
        with open(GLOSSARY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def apply(text: str, glossary: Dict[str, str]) -> str:
    """Apply glossary replacements (case-insensitive on keys)."""
    if not glossary:
        return text
    for key, val in glossary.items():
        # Case-insensitive replacement: find word boundaries.
        pattern = r"\b" + re.escape(key) + r"\b"
        text = re.sub(pattern, val, text, flags=re.IGNORECASE)
    return text
