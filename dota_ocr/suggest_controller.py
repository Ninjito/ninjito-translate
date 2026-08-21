"""Wire the keyboard hook to the suggestion popup.

The flow, in one line: hook -> session -> buffer -> suggester -> popup,
with the grammar check hanging off a debounce so it fires on a pause
instead of on every keystroke.

Three threads meet here, and the split between them is the whole design:

  * the hook thread runs `should_swallow` and `handle_event`. It may not
    touch Tk and may not block, so it only ever updates plain Python
    state and drops a closure on a queue. `should_swallow` in particular
    decides whether Dota sees a key at all, so it is two lookups and
    nothing else.
  * the Tk thread runs `pump`, which drains that queue and repaints the
    popup. This is the same queue-and-drain shape Overlay already uses
    for chat messages, and it is why commit d20bd34's "no Tk from worker
    threads" rule survives here.
  * a short-lived worker thread runs the grammar check, because it is an
    HTTP call and running it on the Tk thread would freeze the overlay
    for as long as LanguageTool takes to answer.

The controller owns the item list and the highlighted index. The popup
is a renderer. That matters because the arrow keys arrive on the hook
thread while the widgets belong to Tk — reading the highlight back off
the widget would race on the one value Tab acts on.

Every dependency is injectable so the decision logic can be tested
without Windows, a display, or the network.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from dota_ocr.chat_session import ChatSession
from dota_ocr.suggest import Suggester, Suggestion
from dota_ocr.typing_buffer import (
    KeyEvent, TypingBuffer,
    VK_ESCAPE, VK_LEFT, VK_RETURN, VK_RIGHT, VK_TAB, VK_UP,
)

# Keys the popup owns while it is on screen. Enter is deliberately
# absent: it must always reach Dota so the message actually sends.
NAV_KEYS = frozenset({VK_TAB, VK_UP, VK_LEFT, VK_RIGHT, VK_ESCAPE})

# How often the Tk thread drains the UI queue. Fast enough that the
# popup tracks typing, slow enough to cost nothing.
POLL_MS = 30


class DotaForeground:
    """Is Dota the window the user is actually typing into?

    The hook sees every keystroke on the machine, so without this gate
    pressing Enter in a browser would open a phantom chat session and
    start swallowing Tab in whatever app the user is really in.

    Split in two on purpose: `refresh` enumerates windows and is called
    from the Tk heartbeat a couple of times a second, while `__call__`
    runs inside the hook callback and does nothing but compare two
    handles.
    """

    def __init__(self) -> None:
        self._hwnd = None

    def refresh(self) -> None:
        try:
            from dota_ocr.window import find_dota_hwnd
            self._hwnd = find_dota_hwnd()
        except Exception:
            self._hwnd = None

    def __call__(self) -> bool:
        hwnd = self._hwnd
        if not hwnd:
            return False
        try:
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow() == hwnd
        except Exception:
            return False


class SuggestController:
    def __init__(
        self,
        root,
        cfg: dict,
        popup=None,
        hook_factory: Callable | None = None,
        suggester=None,
        grammar=None,
        typer_mod=None,
        is_dota_foreground=None,
    ) -> None:
        self.root = root
        self._cfg = cfg or {}
        self._foreground = (is_dota_foreground if is_dota_foreground
                            is not None else DotaForeground())
        self.buffer = TypingBuffer()
        self.session = ChatSession(idle_timeout=20.0)
        self.last_error = ""

        self._popup = popup
        self._hook_factory = hook_factory
        self._hook = None
        self._suggester = suggester
        self._grammar = grammar
        self._typer = typer_mod

        # Owned by the hook thread, read by everyone.
        self._items: list[Suggestion] = []
        self._index = 0
        self._word_items: list[Suggestion] = []
        # The swallow decision can't wait for the Tk thread to repaint,
        # so it reads this rather than the widget's real visibility.
        self._popup_wanted = False

        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._pump_after = None
        self._pumping = False

        self._pending_line = ""
        self._pending_at = 0.0
        self._checked_line = ""
        self._grammar_busy = False

    # ---- config ----

    def _opt(self, key: str, default):
        return (self._cfg.get("suggest") or {}).get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._opt("enabled", True))

    # ---- lifecycle ----

    def is_running(self) -> bool:
        hook = self._hook
        return hook is not None and bool(hook.is_running())

    def start(self) -> bool:
        if not self.enabled:
            self.last_error = "disabled in settings"
            return False
        if self._suggester is None:
            self._suggester = Suggester(
                max_results=int(self._opt("max_results", 3)),
                min_prefix=int(self._opt("min_prefix", 2)),
            )
        if self._grammar is None:
            from dota_ocr.grammar import GrammarFixer
            self._grammar = GrammarFixer()
        if self._typer is None:
            from dota_ocr import typer as _typer
            self._typer = _typer
        if self._popup is None:
            from dota_ocr.suggest_popup import SuggestPopup
            self._popup = SuggestPopup(self.root)
        if self._hook_factory is None:
            from dota_ocr.keyhook import KeyboardHook
            self._hook_factory = KeyboardHook

        self._hook = self._hook_factory(
            on_event=self.handle_event,
            should_swallow=self.should_swallow,
        )
        if self._hook is None:
            self.last_error = "no hook"
            return False
        ok = bool(self._hook.start())
        if not ok:
            self.last_error = getattr(self._hook, "last_error", "hook failed")
            self._hook = None
            return False
        self.last_error = ""
        self._schedule_pump()
        return True

    def stop(self) -> None:
        hook, self._hook = self._hook, None
        if hook is not None:
            try:
                hook.stop()
            except Exception:
                pass
        self._pumping = False
        if self._pump_after is not None:
            try:
                self.root.after_cancel(self._pump_after)
            except Exception:
                pass
            self._pump_after = None
        self._reset()
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass

    # ---- hook thread: keep this cheap ----

    def should_swallow(self, ev: KeyEvent) -> bool:
        """Decide, inside the hook callback, whether Dota sees this key.

        Only nav keys, only while a popup is on screen, only while Dota
        has focus. When none of that holds the user is just playing — or
        is in another app entirely — and stealing Tab would break the
        scoreboard or their alt-tabbed browser.

        The three tests are ordered cheapest-first, so the foreground
        check costs a syscall on arrow keys alone, never on typing.
        """
        return (ev.vk in NAV_KEYS and self._popup_wanted
                and self._foreground())

    def handle_event(self, ev: KeyEvent) -> None:
        if not ev.down:
            return
        try:
            self._handle(ev)
        except Exception as e:
            print(f"[suggest] event error: {e}", flush=True)

    def _handle(self, ev: KeyEvent) -> None:
        now = time.monotonic()

        # The hook is global. Anything typed outside Dota is none of our
        # business, and must not open a session or reach the buffer.
        if not self._foreground():
            if self.session.is_open:
                self.session.on_foreground_lost()
                self._reset()
            return

        # Popup navigation comes first: these keys never reach the buffer.
        if self._popup_wanted and ev.vk in NAV_KEYS:
            if ev.vk == VK_ESCAPE:
                self._clear_suggestions()
            elif ev.vk == VK_UP or ev.vk == VK_LEFT:
                self._move(-1)
            elif ev.vk == VK_RIGHT:
                self._move(1)
            elif ev.vk == VK_TAB:
                self._accept()
            return

        was_open = self.session.is_open
        self.session.on_key(ev, popup_visible=self._popup_wanted, now=now)

        if not self.session.is_open:
            if was_open:
                self._reset()
            return

        if not was_open:
            # The Enter that opened chat is not part of the message.
            self.buffer.reset()
            return

        if ev.vk == VK_RETURN:
            return

        if self.buffer.apply(ev):
            self._refresh()

    def tick(self) -> None:
        """Heartbeat from the owner, a couple of times a second.

        Refreshes the cached Dota window handle (too slow to look up in
        the hook callback) and runs the two recovery paths.
        """
        refresh = getattr(self._foreground, "refresh", None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                pass
        if not self._foreground():
            self.on_foreground_lost()
            return
        if self.session.tick(now=time.monotonic()):
            self._reset()

    def on_foreground_lost(self) -> None:
        if self.session.on_foreground_lost():
            self._reset()

    # ---- suggestions ----

    def _refresh(self) -> None:
        word, _start = self.buffer.current_word()
        fix = bool(self._opt("fix_word", True))
        complete = bool(self._opt("complete_word", True))

        items: list[Suggestion] = []
        if (fix or complete) and word and self._suggester is not None:
            items = self._suggester.suggest_word(word, fix=fix,
                                                 complete=complete)
        self._word_items = items
        self._set_items(items)
        self._mark_line_pending()

    def _set_items(self, items: list[Suggestion]) -> None:
        # Keep the highlight where it was so a new keystroke doesn't
        # slide a different word under Tab.
        if self._index >= len(items):
            self._index = 0
        self._items = items
        self._popup_wanted = bool(items)
        self._push_render()

    def _move(self, delta: int) -> None:
        if not self._items:
            return
        self._index = (self._index + delta) % len(self._items)
        self._push_render()

    def _clear_suggestions(self) -> None:
        self._items = []
        self._word_items = []
        self._index = 0
        self._popup_wanted = False
        self._enqueue(lambda: self._popup and self._popup.hide())

    def _push_render(self) -> None:
        items = list(self._items)
        index = self._index
        if not items:
            self._enqueue(lambda: self._popup and self._popup.hide())
            return
        x, y = self._anchor(len(items))
        self._enqueue(
            lambda: self._popup and self._popup.show(items, index, x, y))

    def _anchor(self, rows: int) -> tuple[int, int]:
        """Where the popup sits: just above Dota's chat area.

        A saved position wins; otherwise it is derived from the region
        the user already calibrated for chat OCR, so it lands near the
        chat box without a second calibration step.
        """
        popup_cfg = self._opt("popup", {}) or {}
        if popup_cfg.get("x") is not None and popup_cfg.get("y") is not None:
            return int(popup_cfg["x"]), int(popup_cfg["y"])
        try:
            from dota_ocr import sizes as _sz
            from dota_ocr.window import find_dota_hwnd, get_client_screen_rect
            hwnd = find_dota_hwnd()
            rect = get_client_screen_rect(hwnd) if hwnd else None
            rel = self._cfg.get("chat_region_relative") or {}
            if rect and rel:
                left, top, _w, _h = rect
                height = rows * _sz.SUGGEST_ROW_HEIGHT + _sz.SUGGEST_PAD * 2
                return (left + int(rel.get("left", 0)),
                        max(0, top + int(rel.get("top", 0)) - height - 8))
        except Exception:
            pass
        return 200, 200

    # ---- grammar, on its own thread ----

    def _mark_line_pending(self) -> None:
        if not self._wants_line_suggestions():
            return
        self._pending_line = self.buffer.text
        self._pending_at = time.monotonic()

    def _wants_line_suggestions(self) -> bool:
        return bool(self._opt("fix_sentence", True)
                    or self._opt("translate_live", False))

    def _grammar_due(self, now: float) -> bool:
        if self._grammar_busy or not self._pending_line:
            return False
        if self._pending_line == self._checked_line:
            return False
        if not self.session.is_open:
            return False
        delay = int(self._opt("grammar_debounce_ms", 600)) / 1000.0
        return (now - self._pending_at) >= delay

    def run_pending_grammar(self) -> None:
        """Check the whole line. Fires on a typing pause, not per key.

        Runs on a worker thread — it makes an HTTP request.
        """
        text = self._pending_line
        self._checked_line = text
        if not text or not self.session.is_open:
            return

        extra: list[Suggestion] = []
        if self._opt("fix_sentence", True) and self._grammar is not None:
            try:
                extra.extend(self._grammar.suggest_line(text))
            except Exception as e:
                print(f"[suggest] grammar failed: {e}", flush=True)

        if self._opt("translate_live", False):
            try:
                from dota_ocr.translator import Translator
                out = Translator().translate(text, src="auto",
                                             target_language="en")
                if out and out.strip() and out.strip() != text.strip():
                    extra.append(Suggestion(out.strip(), "translate", "line"))
            except Exception as e:
                print(f"[suggest] translate failed: {e}", flush=True)

        if not extra:
            return
        # The line has probably moved on while we were on the network.
        if self._pending_line != text or not self.session.is_open:
            return
        # Line suggestions sit under the word ones; mid-word, the word
        # list is what the user is actually looking at.
        self._set_items(self._word_items + extra)

    # ---- Tk thread ----

    def _schedule_pump(self) -> None:
        self._pumping = True
        try:
            self._pump_after = self.root.after(POLL_MS, self.pump)
        except Exception:
            self._pump_after = None

    def pump(self) -> None:
        """Drain queued UI work and start a due grammar check.

        Must run on the Tk thread.
        """
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                print(f"[suggest] ui error: {e}", flush=True)

        if self._grammar_due(time.monotonic()):
            self._grammar_busy = True
            threading.Thread(target=self._grammar_worker, daemon=True,
                             name="ninjito-grammar").start()

        if self._pumping:
            self._schedule_pump()

    def _grammar_worker(self) -> None:
        try:
            self.run_pending_grammar()
        except Exception as e:
            print(f"[suggest] grammar worker: {e}", flush=True)
        finally:
            self._grammar_busy = False

    # ---- accepting ----

    def _accept(self) -> None:
        if not self._items or self._typer is None:
            return
        item = self._items[min(self._index, len(self._items) - 1)]

        if item.scope == "line":
            self._typer.send_backspaces(len(self.buffer.text))
            self._typer.send_text(item.text)
            self.buffer.text = item.text
            self.buffer.cursor = len(item.text)
        else:
            _word, start = self.buffer.current_word()
            self._typer.replace_word(self.buffer.cursor - start, item.text)
            self.buffer.set_current_word(item.text)

        # The accepted text is now the pending line, and it has already
        # been "checked" — re-sending it would just echo it back.
        self._pending_line = self.buffer.text
        self._checked_line = self.buffer.text
        self._clear_suggestions()

    # ---- helpers ----

    def _reset(self) -> None:
        self.buffer.reset()
        self._pending_line = ""
        self._checked_line = ""
        self._clear_suggestions()

    def _enqueue(self, fn: Callable[[], None]) -> None:
        try:
            self._ui_queue.put_nowait(fn)
        except Exception:
            pass
