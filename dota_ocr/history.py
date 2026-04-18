"""Persistent translation history.

Every successful translation is appended to `logs/history.jsonl` next to
the app (or next to the .exe when frozen), one JSON record per line:

    {"t": "2026-04-18T15:22:09", "src": "...", "dst": "..."}

JSONL means the file can be streamed and truncated safely; the viewer
reads the whole file into memory (it's small — chat lines are tiny).
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


LOG_DIR = _app_dir() / "logs"
HISTORY_FILE = LOG_DIR / "history.jsonl"

_lock = threading.Lock()


def append(src: str, dst: str) -> None:
    """Append one translation. Never raises — logging failures must not
    kill the main translate path."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "src": src,
            "dst": dst,
        }
        with _lock, open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_all() -> List[dict]:
    """Return every record in the history file, oldest first.  Missing
    / malformed lines are skipped silently."""
    out: List[dict] = []
    if not HISTORY_FILE.is_file():
        return out
    try:
        with _lock, open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def clear() -> None:
    try:
        with _lock:
            if HISTORY_FILE.is_file():
                HISTORY_FILE.unlink()
    except Exception:
        pass


def file_path() -> Path:
    return HISTORY_FILE
