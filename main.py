"""Dota 2 chat OCR + translation overlay.

Usage:
    1. pip install -r requirements.txt
    2. Install the Russian OCR language pack in Windows (admin PowerShell):
       Add-WindowsCapability -Online -Name "Language.OCR~~~ru-RU~0.0.1.0"
    3. python calibrate.py      (once, to pick the chat region)
    4. python main.py

Press the "Translate" button or F7 to capture + translate the chat.
Only Russian text after the player name ':' is translated.
"""

from __future__ import annotations

# --- Suppress flashing console windows from subprocess calls (tesseract). ---
# pytesseract spawns tesseract.exe without CREATE_NO_WINDOW, which pops up
# a black cmd window every OCR call when the app is built with --noconsole.
# We patch subprocess.Popen here (before pytesseract imports it) so every
# child process inherits the hidden-window flags.
import subprocess as _sp
import sys as _sys
if _sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _STARTF_USESHOWWINDOW = 0x00000001
    _SW_HIDE = 0
    _orig_popen_init = _sp.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        if kwargs.get("creationflags", 0) == 0:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        if kwargs.get("startupinfo") is None:
            si = _sp.STARTUPINFO()
            si.dwFlags |= _STARTF_USESHOWWINDOW
            si.wShowWindow = _SW_HIDE
            kwargs["startupinfo"] = si
        return _orig_popen_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _silent_popen_init

    # Tell Windows this is a distinct app so the taskbar uses gg.ico
    # instead of the generic python.exe icon.
    try:
        import ctypes as _ct
        _ct.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Ninjito.Translate.1"
        )
    except Exception:
        pass

    # --- Single-instance guard ---
    # Create a named mutex; if it already exists, another copy of the app
    # is running.  In that case we try to bring its window to the front
    # and exit immediately — clicking the .exe again just "focuses" it.
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        _k32 = _ct.windll.kernel32
        _u32 = _ct.windll.user32
        _ERROR_ALREADY_EXISTS = 183
        _MUTEX_NAME = "Global\\NinjitoTranslate_SingleInstance_Mutex"
        _k32.CreateMutexW.argtypes = [_wt.LPVOID, _wt.BOOL, _wt.LPCWSTR]
        _k32.CreateMutexW.restype = _wt.HANDLE
        _mutex = _k32.CreateMutexW(None, True, _MUTEX_NAME)
        if _k32.GetLastError() == _ERROR_ALREADY_EXISTS:
            # Try to raise the existing window.
            try:
                _u32.FindWindowW.argtypes = [_wt.LPCWSTR, _wt.LPCWSTR]
                _u32.FindWindowW.restype = _wt.HWND
                hwnd = _u32.FindWindowW(None, "Ninjito Translate")
                if hwnd:
                    _u32.ShowWindow(hwnd, 9)   # SW_RESTORE
                    _u32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            _sys.exit(0)
        # Keep a reference so the mutex lives for the process lifetime.
        globals()["_SINGLE_INSTANCE_MUTEX"] = _mutex
    except Exception:
        pass

import hashlib
import json
import sys
import threading
import time
import traceback
from pathlib import Path

# Russian transcriptions and chat lines are Cyrillic, but the Windows
# console defaults to cp1252 and raises UnicodeEncodeError on print.
# Degrade to '?' characters instead of losing the log line.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _install_file_log() -> None:
    """Mirror stdout/stderr into logs/app.log.

    The packaged EXE is built with --noconsole, so sys.stdout is None and
    every diagnostic the app prints is lost. That makes a failure in the
    voice pipeline (which surfaces at runtime, not at build time)
    invisible and unreportable. Writing to a file keeps them.
    """
    try:
        base = (Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent)
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "app.log"
        # Truncate per launch; a stale 100 MB log helps nobody.
        handle = open(log_path, "w", encoding="utf-8", errors="replace",
                      buffering=1)

        class _Tee:
            """Write to the real stream (if any) and the log file."""

            def __init__(self, stream, sink):
                self._stream = stream
                self._sink = sink

            def write(self, data):
                if self._stream is not None:
                    try:
                        self._stream.write(data)
                    except Exception:
                        pass
                try:
                    self._sink.write(data)
                except Exception:
                    pass
                return len(data)

            def flush(self):
                for target in (self._stream, self._sink):
                    if target is not None:
                        try:
                            target.flush()
                        except Exception:
                            pass

            def isatty(self):
                return False

        sys.stdout = _Tee(sys.stdout, handle)
        sys.stderr = _Tee(sys.stderr, handle)
        globals()["_LOG_HANDLE"] = handle  # keep alive for process lifetime
        print(f"[log] writing to {log_path}", flush=True)
    except Exception:
        pass


_install_file_log()

import cv2

from dota_ocr.capture import RegionCapture
from dota_ocr.dedup import MessageDeduplicator
from dota_ocr.dpi import enable_dpi_awareness
from dota_ocr.ocr import OCRReader
from dota_ocr.overlay import Overlay
from dota_ocr.postprocess import (
    has_cyrillic, is_chat_line, normalize_colons, normalize_cyrillic,
    split_chat_line,
)
from dota_ocr.translator import Translator
from dota_ocr import history, glossary

def _app_dir() -> Path:
    """Return the folder where config.json lives.

    - When frozen by PyInstaller: next to the EXE.
    - When running as a script: project root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _app_dir() / "config.json"
DEBUG_DIR = _app_dir() / "debug"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"Missing config at {CONFIG_PATH}.", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# split_chat_line lives in postprocess so the junk filter and the
# translator agree on where the message body starts. They used to
# disagree, and OCR-mangled clan tags landed in the body of both.


def worker(overlay: Overlay, cfg: dict, stop_event: threading.Event,
           shared: dict | None = None) -> None:
    rel_region = cfg.get("chat_region_relative")
    abs_region = cfg.get("chat_region")
    if rel_region and rel_region.get("width", 0) >= 10:
        region_descr = f"dota-relative {rel_region}"
    elif abs_region and abs_region.get("width", 0) >= 10:
        region_descr = f"absolute-screen {abs_region}"
    else:
        print("Chat region not calibrated. Run: python calibrate.py", file=sys.stderr)
        return

    debug = bool(cfg.get("debug", False))
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    capture = RegionCapture(
        bbox=abs_region if not rel_region else None,
        relative_bbox=rel_region,
        require_foreground=bool(cfg.get("require_dota_foreground", False)),
        capture_mode=cfg.get("capture_mode", "printwindow"),
    )
    if shared is not None:
        shared["capture_ref"] = capture

    print(f"[init] loading Windows OCR for lang='{cfg.get('ocr_language', 'ru')}' ...",
          flush=True)
    ocr = OCRReader(
        lang=cfg.get("ocr_language", "ru"),
        preprocess=bool(cfg.get("preprocess", True)),
        debug=debug,
    )
    print("[init] OCR ready.", flush=True)

    translator = Translator(target=cfg.get("target_language", "en"))
    dedup = MessageDeduplicator()
    gloss = glossary.load()
    if gloss:
        print(f"[init] Loaded {len(gloss)} glossary entries.", flush=True)

    print(f"[run] watching {region_descr}. Press Translate or F7.", flush=True)

    # Auto-retry state: if a trigger produces 0 translations, we immediately
    # re-run capture+OCR (without waiting for another F7 press) up to
    # MAX_ATTEMPTS-1 more times.  OCR can miss text on a single frame.
    MAX_ATTEMPTS = 3
    attempt_num = 0  # 0 = idle; 1..MAX_ATTEMPTS = in a retry burst
    while not stop_event.is_set() and not overlay.is_closing():
      try:
        # Wait for button click or F7 hotkey — unless we're mid-retry.
        if attempt_num == 0:
            if not overlay.wait_for_trigger(timeout=0.3):
                continue
            attempt_num = 1
        else:
            attempt_num += 1
            if attempt_num > MAX_ATTEMPTS:
                attempt_num = 0
                continue
            time.sleep(0.15)
            if debug:
                print(f"[retry] attempt {attempt_num}/{MAX_ATTEMPTS}", flush=True)

        # --- Capture ---
        try:
            img = capture.grab()
        except Exception as e:
            print(f"[capture] ERROR: {e}", flush=True)
            overlay.set_status("Capture failed", "#ff4444")
            continue

        if img is None:
            overlay.set_status("Dota 2 not found", "#ff4444")
            continue

        if debug:
            try:
                cv2.imwrite(str(DEBUG_DIR / "frame_raw.png"), img)
                if ocr.preprocess_enabled:
                    pre = OCRReader._preprocess(img)
                    cv2.imwrite(str(DEBUG_DIR / "frame_pre.png"), pre)
                    # Also save grayscale for analysis.
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    cv2.imwrite(str(DEBUG_DIR / "frame_gray.png"), gray)
                    bright = (gray > 170).sum() / gray.size * 100
                    print(f"[debug] Bright pixel ratio: {bright:.1f}%", flush=True)
            except Exception:
                pass

        # --- OCR ---
        overlay.set_status("Reading...", "#ffa500")
        try:
            lines = ocr.read(img)
        except Exception as e:
            print(f"[ocr] ERROR: {e}", flush=True)
            overlay.set_status("OCR failed", "#ff4444")
            continue

        # Always log what was found on button press.
        if not lines:
            print("[ocr] 0 lines detected (empty frame?). "
                  "Check debug/frame_raw.png — does it show the chat?", flush=True)
        else:
            print(f"[ocr] {len(lines)} lines detected:", flush=True)
            for text, _ in lines:
                norm = normalize_colons(text)
                cyr = has_cyrillic(norm)
                tag = "ru→en" if cyr else "en→ru"
                print(f"  [{tag}] {text!r}", flush=True)

        # --- Merge OCR fragments back into full chat lines ---
        # Tesseract sometimes splits "[Allies] Name [Clan] : msg" across
        # multiple output lines because the player name is in a different
        # color (blue/colored) that the brightness threshold drops.
        # Rebuild chat lines by gluing [Tag] + (optional middle) + : body.
        merged: list[tuple[str, float]] = []
        i = 0
        # Pre-normalize each fragment's colon-lookalikes (';', '{', '}', '&')
        # so merge-logic's ':' checks work on OCR variants like
        # '[Allies] Name ; body'. Otherwise we'd wrongly treat that
        # fragment as "no colon" and glue it to the NEXT line.
        raw = [
            (normalize_colons(t), c)
            for t, c in lines
            if t.strip()
        ]
        while i < len(raw):
            text, conf = raw[i]
            t = text.strip()
            # If this fragment starts with [Tag] but has no ':', look ahead
            # for a line that starts with ':' and merge them.
            if t.startswith('[') and ':' not in t:
                buf = [t]
                j = i + 1
                while j < len(raw):
                    nxt = raw[j][0].strip()
                    if nxt.startswith(':'):
                        buf.append(nxt)
                        j += 1
                        break
                    # Intermediate junk lines (stray characters) — skip.
                    if len(nxt) <= 3 and ':' not in nxt:
                        j += 1
                        continue
                    break
                if len(buf) > 1:
                    merged.append((' '.join(buf), conf))
                    i = j
                    continue
            # A fragment that starts with ':' alone — try to glue the
            # previous unmerged tag (if present) from the raw list.
            if t.startswith(':') and not merged and i > 0:
                prev = raw[i - 1][0].strip()
                if prev.startswith('['):
                    merged.append((f"{prev} {t}", conf))
                    i += 1
                    continue
            merged.append((text, conf))
            i += 1

        if debug and merged != lines:
            print(f"[merge] {len(lines)} fragments -> {len(merged)} chat lines", flush=True)
            for m, _ in merged:
                print(f"  merged: {m!r}", flush=True)

        # --- Translate Russian lines; skip English ---
        translated_count = 0
        overlay.set_status("Translating...", "#ffa500")
        # Clear previous batch so only current F7 press results show.
        overlay.clear()

        # Keep only the LAST 5 chat lines OCR produced. Older ones are
        # either already shown from a prior F7 press or off-screen in
        # Dota. This makes the overlay consistent (always the newest
        # chat) and saves translation API calls on stale lines.
        merged = merged[-5:]

        for text, conf in merged:
            # Normalize colon lookalikes first.
            text = normalize_colons(text)

            # Must look like a real chat line: "[Tag] Name : message".
            # This rejects HUD garbage (hero stats, ability tooltips,
            # scoreboard columns) that happens to contain Cyrillic.
            if not is_chat_line(text):
                print(f"[skip-notchat] {text!r}", flush=True)
                continue

            # RU → EN only: skip any line that isn't predominantly Russian.
            # Detection (is_chat_line) still accepts team AND all chat —
            # we just don't translate English-only messages.
            if not has_cyrillic(text):
                print(f"[skip-en] {text!r}", flush=True)
                continue

            # Extra junk guard: a "word" is a run of letters.  Real chat
            # messages have most of their words at length >=2 and mostly
            # lowercase.  OCR garbage tends to be 1-char tokens with
            # random caps like "CABP moa> LEI ise".
            import re as _re
            # Judge the message only. Splitting on the first ':' used to
            # include the OCR-mangled clan tag ('[:Я В Аи]'), whose
            # one-character fragments pushed real messages over the junk
            # threshold and silently dropped them.
            body_only = split_chat_line(text)[1] or text
            words = _re.findall(r"[^\s]+", body_only)
            if len(words) >= 3:
                short = sum(1 for w in words if len(w) == 1)
                mixed_case = sum(
                    1 for w in words
                    if len(w) >= 2 and any(c.isupper() for c in w[1:])
                )
                # >40% single-char tokens OR >40% mid-word-caps ⇒ junk.
                if short / len(words) > 0.4 or mixed_case / len(words) > 0.4:
                    print(f"[skip-junk] {text!r} words={words}", flush=True)
                    continue

            # Skip obvious system messages (no player typed these).
            lower = text.lower()
            system_keywords = (
                "has reconnected", "resumed the game", "paused the game",
                "unpausing in", "has abandoned", "has left",
                "glyph of fortification",
            )
            if any(k in lower for k in system_keywords):
                print(f"[skip-sys] {text!r}", flush=True)
                continue

            # Extract the message body: everything after the first ':'.
            # Fix Latin-for-Cyrillic homoglyph OCR errors in the body
            # (m3 -> из, Еrипtа -> Египта, etc). Only touches the body
            # after the first ':', so usernames stay intact.
            text = normalize_cyrillic(text)

            # If no ':', translate the whole line (plain body fragment).
            prefix, body = split_chat_line(text)
            to_translate = body if body.strip() else text

            try:
                # Apply custom glossary replacements.
                to_translate_with_gloss = glossary.apply(to_translate, gloss)

                # RU → EN only.
                src = "ru"
                tgt = "en"
                translated = translator.translate(to_translate_with_gloss, src=src, target_language=tgt)
                if not translated:
                    continue

                # Hard guard: the result must be meaningfully different
                # from the input AND must contain no Cyrillic.  Google
                # sometimes echoes the input back when it can't handle
                # OCR-garbled text — we skip those instead of showing
                # English-to-English or Russian-to-Russian noise.
                src_norm = to_translate.strip().lower()
                dst_norm = translated.strip().lower()
                if dst_norm == src_norm:
                    print(f"[skip-echo] src={src} {to_translate!r} -> {translated!r}", flush=True)
                    continue
                # RU→EN: output must be Latin — drop any Cyrillic echoes
                # from Google when it can't handle OCR-garbled input.
                if any("\u0400" <= c <= "\u04FF" for c in translated):
                    print(f"[skip-ru-out] {translated!r}", flush=True)
                    continue

                display_src = f"{prefix} {body}" if (prefix and body) else text
                display_dst = f"{prefix} {translated}" if (prefix and body) else translated
                print(f"{to_translate!r}  ->  {translated!r}", flush=True)
                overlay.push(display_src, display_dst)
                history.append(display_src, display_dst)
                translated_count += 1
            except Exception as e:
                print(f"[trans] error on {text!r}: {e}", flush=True)

        if translated_count > 0:
            overlay.set_status(f"Translated {translated_count} line(s)", "#7bd88f")
            attempt_num = 0  # end retry burst on success
        else:
            if attempt_num < MAX_ATTEMPTS:
                # Leave attempt_num > 0 so next iteration retries immediately.
                overlay.set_status(f"Reading... ({attempt_num}/{MAX_ATTEMPTS})", "#ffa500")
            else:
                overlay.set_status("No translatable chat found", "#888")
                attempt_num = 0
      except Exception:
        # Any unhandled error in this iteration: log it, show status,
        # sleep briefly, then keep the loop alive. This way the app
        # NEVER stops on its own.
        print("[worker] unhandled error — recovering:", flush=True)
        traceback.print_exc()
        try:
            overlay.set_status("Recovered from error", "#ff8844")
        except Exception:
            pass
        time.sleep(0.5)


def _worker_respawn(overlay, cfg, stop_event, shared=None):
    """Outer wrapper: if the worker itself crashes, restart it forever."""
    while not stop_event.is_set() and not overlay.is_closing():
        try:
            worker(overlay, cfg, stop_event, shared)
            # worker returned cleanly (loop exited because stop_event set).
            return
        except Exception:
            print("[worker] thread crashed — respawning in 2s:", flush=True)
            traceback.print_exc()
            try:
                overlay.set_status("Worker crashed, restarting...", "#ff4444")
            except Exception:
                pass
            time.sleep(2.0)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _make_voice_toggle(overlay: Overlay, cfg: dict, holder: dict):
    """Build the callback the overlay uses to switch voice translation.

    Returns callback(enabled: bool) -> bool, where the return value is the
    state actually reached — starting can fail (no loopback device, model
    download blocked) and the UI must reflect reality, not intent.

    The listener is created lazily on first enable so that users who never
    turn voice on never pay the model-loading cost.
    """
    def toggle(enabled: bool) -> bool:
        listener = holder.get("listener")
        if not enabled:
            if listener is not None:
                try:
                    listener.stop()
                except Exception as e:
                    print(f"[voice] stop failed: {e}", flush=True)
            return False

        # Rebuild each time we enable so device/model config changes apply.
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

        try:
            from dota_ocr.voice import VoiceListener
        except Exception as e:
            print(f"[voice] unavailable: {e}", flush=True)
            overlay.set_status("Voice: not installed", "#ff4444")
            return False

        def on_result(russian: str, english: str) -> None:
            overlay.push_voice(russian, english)
            # Tagged in the log too, so the history window distinguishes
            # what was spoken from what was typed.
            history.append(f"🔊 {russian}", f"🔊 {english}")

        try:
            listener = VoiceListener(
                cfg=cfg,
                translator=Translator(target=cfg.get("target_language", "en")),
                on_result=on_result,
                on_status=overlay.set_status,
                glossary_map=glossary.load(),
            )
            holder["listener"] = listener
            return bool(listener.start())
        except Exception as e:
            print(f"[voice] start failed: {e}", flush=True)
            traceback.print_exc()
            overlay.set_status("Voice: failed to start", "#ff4444")
            return False

    return toggle


def main() -> None:
    enable_dpi_awareness()
    cfg = load_config()

    # Shared mutable state so the recalibrate callback can update the
    # running capture without restarting the worker thread.
    shared = {"cfg": cfg, "capture_ref": None}
    voice_holder: dict = {"listener": None}

    def on_recalibrate(new_rel: dict) -> None:
        cfg["chat_region_relative"] = new_rel
        cfg.pop("chat_region", None)
        try:
            save_config(cfg)
            print(f"[calibrate] saved new region: {new_rel}", flush=True)
        except Exception as e:
            print(f"[calibrate] save failed: {e}", flush=True)
        cap = shared.get("capture_ref")
        if cap is not None:
            try:
                cap._relative_bbox = new_rel
                cap._absolute_bbox = None
                print("[calibrate] capture region updated live", flush=True)
            except Exception as e:
                print(f"[calibrate] live update failed: {e}", flush=True)

    def on_hotkey_changed(new_vk: int, name: str) -> None:
        cfg.setdefault("overlay", {})["hotkey_vk"] = int(new_vk)
        try:
            save_config(cfg)
            print(f"[hotkey] saved new hotkey: {name} (VK 0x{new_vk:02X})", flush=True)
        except Exception as e:
            print(f"[hotkey] save failed: {e}", flush=True)

    overlay_kwargs = dict(cfg.get("overlay", {}))
    overlay_kwargs["on_recalibrate"] = on_recalibrate
    overlay_kwargs["on_hotkey_changed"] = on_hotkey_changed
    overlay_kwargs["cfg"] = cfg
    # Late-bound so the callback can capture `overlay` itself.
    overlay_kwargs["on_voice_toggle"] = lambda en: voice_holder["toggle"](en)
    overlay = Overlay(**overlay_kwargs)
    voice_holder["toggle"] = _make_voice_toggle(overlay, cfg, voice_holder)

    # Restore the previous session's voice state. Deferred onto the Tk
    # loop so a slow model load can't stall the overlay from appearing.
    if bool((cfg.get("voice") or {}).get("enabled", False)):
        def _resume_voice() -> None:
            ok = voice_holder["toggle"](True)
            if not ok:
                try:
                    overlay._set_voice_cfg("enabled", False)
                except Exception:
                    pass
            try:
                overlay._update_voice_button()
            except Exception:
                pass
        overlay.root.after(600, _resume_voice)

    # --- Live typing suggestions in Dota's chat box ---
    # Started after the overlay exists because the popup is a Toplevel
    # of its root, and deferred so loading the 82k-word dictionary can't
    # delay the window appearing.
    suggest_holder: dict = {"ctrl": None}
    if bool((cfg.get("suggest") or {}).get("enabled", True)):
        def _start_suggest() -> None:
            try:
                from dota_ocr.suggest_controller import SuggestController
                ctrl = SuggestController(overlay.root, cfg)
                suggest_holder["ctrl"] = ctrl
                overlay._suggest_controller = ctrl
                ok = ctrl.start()
                print(f"[suggest] started={ok} {ctrl.last_error}", flush=True)
            except Exception as e:
                print(f"[suggest] start failed: {e}", flush=True)
                return

            # tick() refreshes the cached Dota window handle and runs
            # both recovery paths — idle timeout and lost focus. The
            # handle lookup is too slow for the hook callback, so it
            # happens here instead.
            def _beat() -> None:
                try:
                    ctrl.tick()
                except Exception:
                    pass
                try:
                    overlay.root.after(500, _beat)
                except Exception:
                    pass
            overlay.root.after(500, _beat)

        overlay.root.after(900, _start_suggest)

    stop_event = threading.Event()
    t = threading.Thread(
        target=_worker_respawn, args=(overlay, cfg, stop_event, shared),
        daemon=True,
    )
    t.start()

    # Tk mainloop — if an unhandled exception ever leaks out, restart it.
    try:
        while not stop_event.is_set():
            try:
                overlay.mainloop()
                break  # clean exit
            except Exception:
                print("[ui] mainloop crashed — restarting:", flush=True)
                traceback.print_exc()
                time.sleep(0.5)
    finally:
        stop_event.set()
        listener = voice_holder.get("listener")
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        ctrl = suggest_holder.get("ctrl")
        if ctrl is not None:
            try:
                ctrl.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
