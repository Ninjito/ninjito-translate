"""Centralized window sizes + a few layout constants.

Edit this file to tweak the dimensions of every window in the app
without hunting through overlay.py.  Everything is in pixels.

Each entry is (width, height).  Some windows (main overlay, logs)
also auto-resize at runtime — those values are the *initial* size.
"""

from __future__ import annotations

# --- Main overlay (the always-on-top translated-chat window) ------------
MAIN_WIDTH = 640
# Default height is just the button bar — the overlay auto-grows to fit
# messages (up to AUTOSIZE_MESSAGES) and shrinks back to this when empty.
MAIN_HEIGHT = 40
MAIN_X = 50
MAIN_Y = 50

# --- Paste / type-to-translate window -----------------------------------
PASTE_WIDTH = 800
PASTE_HEIGHT = 340

# --- Logs / history window ----------------------------------------------
LOGS_WIDTH = 760
LOGS_HEIGHT = 520

# --- Settings window (hotkeys / capture / voice / suggest / theme) ------
# Grew from 500x420 when the panel was rebuilt on cards and a real
# spacing scale — the old size only fit because everything was crammed.
# Height is set by Hotkeys, the tallest tab: nine keybind rows plus the
# note under them.
SETTINGS_WIDTH = 560
SETTINGS_HEIGHT = 540

# --- Live typing suggestion popup ---------------------------------------
# Width fits a corrected sentence; height is per row and the popup grows
# to the number of suggestions actually shown.
SUGGEST_WIDTH = 460
SUGGEST_ROW_HEIGHT = 22
SUGGEST_PAD = 6

# --- Auto-size behaviour for the main overlay ---------------------------
# When locked, the overlay can't be scrolled, so it auto-grows to fit
# this many messages on screen at once.
AUTOSIZE_MESSAGES = 5
AUTOSIZE_MIN_HEIGHT = 40
AUTOSIZE_MAX_HEIGHT = 900


def geom(w: int, h: int, x: int | None = None, y: int | None = None) -> str:
    """Build a Tk geometry string. Pass x/y to position, omit to center-ish."""
    if x is None or y is None:
        return f"{w}x{h}"
    return f"{w}x{h}+{x}+{y}"
