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

import hashlib
import json
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2

from dota_ocr.capture import RegionCapture
from dota_ocr.dedup import MessageDeduplicator
from dota_ocr.dpi import enable_dpi_awareness
from dota_ocr.ocr import OCRReader
from dota_ocr.overlay import Overlay
from dota_ocr.postprocess import has_cyrillic, is_chat_line, normalize_colons
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


def split_chat_line(text: str) -> tuple[str, str]:
    """Split '[Tag] Name [Sub]: message body' into (prefix, body).

    Returns (prefix_with_colon, body). If no colon found, returns
    ('', full_text).
    """
    text = normalize_colons(text)
    idx = text.find(":")
    if idx < 0:
        return ("", text.strip())
    prefix = text[: idx + 1].strip()
    body = text[idx + 1 :].strip()
    return (prefix, body)


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
                tag = "TRANSLATE" if cyr else "skip-en"
                print(f"  [{tag}] {text!r}", flush=True)

        # --- Merge OCR fragments back into full chat lines ---
        # Tesseract sometimes splits "[Allies] Name [Clan] : msg" across
        # multiple output lines because the player name is in a different
        # color (blue/colored) that the brightness threshold drops.
        # Rebuild chat lines by gluing [Tag] + (optional middle) + : body.
        merged: list[tuple[str, float]] = []
        i = 0
        raw = [(t, c) for t, c in lines if t.strip()]
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

        for text, conf in merged:
            # Normalize colon lookalikes first.
            text = normalize_colons(text)

            # Must look like a real chat line: "[Tag] Name : message".
            # This rejects HUD garbage (hero stats, ability tooltips,
            # scoreboard columns) that happens to contain Cyrillic.
            if not is_chat_line(text):
                if debug:
                    print(f"[skip-notchat] {text!r}", flush=True)
                continue

            # Only translate if it has Russian (Cyrillic) characters.
            if not has_cyrillic(text):
                if debug:
                    print(f"[skip-en] {text!r}", flush=True)
                continue

            # Extra junk guard: a "word" is a run of letters.  Real chat
            # messages have most of their words at length >=2 and mostly
            # lowercase.  OCR garbage tends to be 1-char tokens with
            # random caps like "CABP moa> LEI ise".
            import re as _re
            body_only = text.split(":", 1)[1] if ":" in text else text
            words = _re.findall(r"[^\s]+", body_only)
            if len(words) >= 3:
                short = sum(1 for w in words if len(w) == 1)
                mixed_case = sum(
                    1 for w in words
                    if len(w) >= 2 and any(c.isupper() for c in w[1:])
                )
                # >40% single-char tokens OR >40% mid-word-caps ⇒ junk.
                if short / len(words) > 0.4 or mixed_case / len(words) > 0.4:
                    if debug:
                        print(f"[skip-junk] {text!r}", flush=True)
                    continue

            # Skip obvious system messages (no player typed these).
            lower = text.lower()
            system_keywords = (
                "has reconnected", "resumed the game", "paused the game",
                "unpausing in", "has abandoned", "has left",
                "glyph of fortification",
            )
            if any(k in lower for k in system_keywords):
                if debug:
                    print(f"[skip-sys] {text!r}", flush=True)
                continue

            # Extract the message body: everything after the first ':'.
            # If no ':', translate the whole line (plain body fragment).
            prefix, body = split_chat_line(text)
            to_translate = body if body.strip() else text

            try:
                # Apply custom glossary replacements.
                to_translate_with_gloss = glossary.apply(to_translate, gloss)

                # Detect language: Russian or English?
                # Heuristic: count Cyrillic chars in the original (before glossary).
                cyr_count = sum(1 for c in to_translate if "\u0400" <= c <= "\u04FF")
                lat_count = sum(1 for c in to_translate if c.isalpha() and not ("\u0400" <= c <= "\u04FF"))
                is_russian = cyr_count >= 3 and lat_count + cyr_count > 0 and (cyr_count / (lat_count + cyr_count)) >= 0.4
                src = "ru" if is_russian else "en"
                tgt = "en" if is_russian else "ru"

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
                    if debug:
                        print(f"[skip-echo] {to_translate!r}", flush=True)
                    continue
                if any("\u0400" <= c <= "\u04FF" for c in translated):
                    if debug:
                        print(f"[skip-ru-out] {translated!r}", flush=True)
                    continue

                display_src = f"{prefix} {body}" if (prefix and body) else text
                display_dst = f"{prefix} {translated}" if (prefix and body) else translated
                print(f"{to_translate!r}  ->  {translated!r}", flush=True)
                overlay.push(display_src, display_dst)
                history.append(display_src, display_dst)
                translated_count += 1
            except Exception as e:
                if debug:
                    print(f"[trans] error on {text!r}: {e}", flush=True)

        if translated_count > 0:
            overlay.set_status(f"Translated {translated_count} line(s)", "#7bd88f")
            attempt_num = 0  # end retry burst on success
        else:
            if attempt_num < MAX_ATTEMPTS:
                # Leave attempt_num > 0 so next iteration retries immediately.
                overlay.set_status(f"Reading... ({attempt_num}/{MAX_ATTEMPTS})", "#ffa500")
            else:
                overlay.set_status("No Russian text found", "#888")
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


def main() -> None:
    enable_dpi_awareness()
    cfg = load_config()

    # Shared mutable state so the recalibrate callback can update the
    # running capture without restarting the worker thread.
    shared = {"cfg": cfg, "capture_ref": None}

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
    overlay = Overlay(**overlay_kwargs)
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


if __name__ == "__main__":
    main()
