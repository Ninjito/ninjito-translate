"""Track whether Dota's chat box is open, from keystrokes alone.

Enter opens team chat, Shift+Enter opens all chat, Enter or Escape
closes it again. Because we only ever infer this, a single missed
transition would leave us capturing keys the user is really sending to
the game as ability commands. The foreground-loss and idle-timeout
transitions exist to bound that damage: whatever happens, the session
returns to CLOSED on its own.

Pure module — no ctypes, no I/O.
"""

from __future__ import annotations

from enum import Enum

from dota_ocr.typing_buffer import KeyEvent, VK_ESCAPE, VK_RETURN


class ChatState(str, Enum):
    CLOSED = "closed"
    TEAM = "team"
    ALL = "all"


class ChatSession:
    def __init__(self, idle_timeout: float = 20.0) -> None:
        self.state = ChatState.CLOSED
        self._idle_timeout = idle_timeout
        self._last_key_at = 0.0

    @property
    def is_open(self) -> bool:
        return self.state is not ChatState.CLOSED

    def on_key(self, ev: KeyEvent, popup_visible: bool, now: float) -> bool:
        """Fold a key press into the session. True if the state changed."""
        if not ev.down:
            return False

        if self.is_open:
            self._last_key_at = now

        if ev.vk == VK_RETURN:
            if self.is_open:
                self.state = ChatState.CLOSED
                return True
            self.state = ChatState.ALL if ev.shift else ChatState.TEAM
            self._last_key_at = now
            return True

        if ev.vk == VK_ESCAPE and self.is_open:
            # A visible popup eats Escape before Dota can see it, so the
            # chat box stays open. See the spec, section 3.
            if popup_visible:
                return False
            self.state = ChatState.CLOSED
            return True

        return False

    def on_foreground_lost(self) -> bool:
        """Dota stopped being the foreground window."""
        if not self.is_open:
            return False
        self.state = ChatState.CLOSED
        return True

    def tick(self, now: float) -> bool:
        """Close the session if it has gone quiet for too long."""
        if not self.is_open:
            return False
        if now - self._last_key_at < self._idle_timeout:
            return False
        self.state = ChatState.CLOSED
        return True
