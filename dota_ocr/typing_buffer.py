"""Reconstruct what the user is typing into Dota's chat box.

Dota exposes no API for reading its chat input, so the only way to know
the text is to replay the user's own keystrokes into a buffer we own.
This module is deliberately pure — no ctypes, no Windows, no I/O — so
the reconstruction logic can be tested exhaustively. Every divergence
between this buffer and what's really on screen shows up as a
suggestion for a word the user isn't typing.

The buffer is memory-only and is wiped on `reset()`. Nothing here ever
touches disk.
"""

from __future__ import annotations

from dataclasses import dataclass

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E

# Characters that end a word. Apostrophes stay inside ("don't").
_WORD_BREAK = " \t\n.,!?;:()[]{}\"/\\<>|"


@dataclass(frozen=True)
class KeyEvent:
    """One key transition, already decoded to a character.

    `char` is the printable character the key produced under the active
    keyboard layout ('' when the key produces none). Decoding happens in
    `keyhook.py` via ToUnicodeEx so a Russian layout yields Cyrillic
    rather than mis-mapped Latin.
    """

    vk: int
    down: bool
    char: str
    shift: bool
    ctrl: bool
    alt: bool


class TypingBuffer:
    """The text and caret position we believe Dota's chat box holds."""

    def __init__(self) -> None:
        self.text = ""
        self.cursor = 0

    def reset(self) -> None:
        """Forget everything. Called whenever the chat box closes."""
        self.text = ""
        self.cursor = 0

    def apply(self, ev: KeyEvent) -> bool:
        """Fold one key event into the buffer.

        Returns True only when the *text* changed, so callers can skip
        recomputing suggestions on pure caret movement.
        """
        if not ev.down:
            return False

        if ev.vk == VK_BACK:
            if ev.ctrl:
                return self._delete_word_left()
            if self.cursor <= 0:
                return False
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
            return True

        if ev.vk == VK_DELETE:
            if self.cursor >= len(self.text):
                return False
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1:]
            return True

        if ev.vk == VK_LEFT:
            self.cursor = max(0, self.cursor - 1)
            return False
        if ev.vk == VK_RIGHT:
            self.cursor = min(len(self.text), self.cursor + 1)
            return False
        if ev.vk == VK_HOME:
            self.cursor = 0
            return False
        if ev.vk == VK_END:
            self.cursor = len(self.text)
            return False

        # Ctrl+letter is a command (Ctrl+A, Ctrl+V, ...), never text.
        # Ctrl+Alt is AltGr, which DOES produce text, so allow that.
        if ev.ctrl and not ev.alt:
            return False

        if ev.char:
            self.text = self.text[: self.cursor] + ev.char + self.text[self.cursor:]
            self.cursor += len(ev.char)
            return True

        return False

    def _delete_word_left(self) -> bool:
        if self.cursor <= 0:
            return False
        i = self.cursor
        while i > 0 and self.text[i - 1] in _WORD_BREAK:
            i -= 1
        while i > 0 and self.text[i - 1] not in _WORD_BREAK:
            i -= 1
        if i == self.cursor:
            return False
        self.text = self.text[:i] + self.text[self.cursor:]
        self.cursor = i
        return True

    def current_word(self) -> tuple[str, int]:
        """Return (word being typed before the caret, its start index)."""
        i = self.cursor
        while i > 0 and self.text[i - 1] not in _WORD_BREAK:
            i -= 1
        return self.text[i:self.cursor], i

    def set_current_word(self, new: str) -> None:
        """Replace the word before the caret, leaving the caret after it."""
        _word, start = self.current_word()
        self.text = self.text[:start] + new + self.text[self.cursor:]
        self.cursor = start + len(new)
