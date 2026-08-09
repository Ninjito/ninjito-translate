"""Live end-to-end check: real speakers -> real Whisper -> real filters.

Not part of the pytest suite — it needs audio hardware, plays sound out
loud, and loads the Whisper model, so it is run by hand:

    python tests/live_voice_check.py            # English rejection check
    python tests/live_voice_check.py --listen 45  # listen to whatever plays

The default mode speaks an English Dota-style phrase through the default
output device and asserts the pipeline (1) actually heard it and (2)
rejected it as non-Russian.  That is the behaviour that keeps the game's
own announcer and hero lines off the overlay.

--listen just runs the listener for N seconds and reports everything it
accepts, so you can play Russian audio and watch it translate.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.translator import Translator
from dota_ocr.voice import VoiceListener, list_loopback_devices

ENGLISH_PHRASE = ("Roshan is dead. They are pushing the middle lane "
                  "right now with five heroes.")


def speak(text: str) -> None:
    """Say `text` through the default output device via Windows SAPI."""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 0; "
        f"$s.Speak('{text}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=False, capture_output=True)


def build_listener(results: list, cfg: dict | None = None) -> VoiceListener:
    return VoiceListener(
        cfg=cfg or {"debug": True, "voice": {"model_size": "small"}},
        translator=Translator(target="en"),
        on_result=lambda ru, en: results.append((ru, en)),
        on_status=lambda t, c="": None,
    )


def wait_until_ready(listener: VoiceListener, timeout: float = 180.0) -> bool:
    """Block until the Whisper model has finished loading."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tr = listener._transcriber
        if tr is not None and tr.ready:
            return True
        if not listener.running:
            return False
        time.sleep(0.5)
    return False


def run_rejection_check() -> int:
    print("=" * 66)
    print("LIVE CHECK 1 — English speech must be heard, then rejected")
    print("=" * 66)

    devices = list_loopback_devices()
    if not devices:
        print("FAIL: no loopback devices")
        return 1
    for d in devices:
        print(f"  device: {d['name']}  default={d['is_default']}")

    results: list = []
    listener = build_listener(results)

    log = io.StringIO()
    # The listener logs its decisions to stdout from worker threads;
    # capturing them is how we assert on what it heard vs. what it kept.
    with redirect_stdout(log):
        if not listener.start():
            sys.stdout = sys.__stdout__
            print(f"FAIL: listener did not start ({listener.last_error})")
            return 1
        ready = wait_until_ready(listener)
        if ready:
            time.sleep(0.5)
            speak(ENGLISH_PHRASE)
            time.sleep(6.0)      # let the utterance close and transcribe
        listener.stop()

    output = log.getvalue()
    print(output)

    if not ready:
        print("FAIL: Whisper model never became ready")
        return 1

    # An empty result set is NOT evidence of correct rejection — a crashed
    # pipeline produces exactly the same empty set.  Require positive proof
    # that a transcription happened and that the language filter is what
    # discarded it.
    errored = "process error" in output or "capture error" in output
    rejected_lang = "[voice-skip:lang]" in output
    transcribed = rejected_lang or "] lang=" in output

    print("-" * 66)
    print(f"  spoke:            {ENGLISH_PHRASE!r}")
    print(f"  transcribed:      {transcribed}")
    print(f"  rejected as en:   {rejected_lang}")
    print(f"  pipeline errors:  {errored}")
    print(f"  overlay results:  {results}")
    print("-" * 66)

    if errored:
        print("FAIL: the pipeline raised — see the log above.")
        return 1
    if not transcribed:
        print("FAIL: nothing was transcribed — check that the speaking")
        print("      device matches the captured loopback device.")
        return 1
    if results:
        print("FAIL: English audio leaked through to the overlay")
        return 1
    if not rejected_lang:
        print("FAIL: transcribed but not dropped by the language filter.")
        return 1
    print("PASS: English speech was captured, transcribed and dropped as en.")
    return 0


def measure_device(device: dict, seconds: float = 2.0) -> float:
    """Return the peak level heard on `device` over `seconds`."""
    import numpy as np
    import pyaudiowpatch as pyaudio

    p = None
    stream = None
    try:
        p = pyaudio.PyAudio()
        rate = int(device["rate"])
        channels = int(device["channels"])
        chunk = 1024
        stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                        frames_per_buffer=chunk, input=True,
                        input_device_index=int(device["index"]))
        frames = [stream.read(chunk, exception_on_overflow=False)
                  for _ in range(int(rate / chunk * seconds))]
        audio = (np.frombuffer(b"".join(frames), dtype=np.int16)
                 .astype(np.float32) / 32768.0)
        return float(np.abs(audio).max()) if audio.size else 0.0
    except Exception as e:
        print(f"  (probe failed on {device['name']}: {e})")
        return 0.0
    finally:
        for closer in (lambda: stream.close() if stream else None,
                       lambda: p.terminate() if p else None):
            try:
                closer()
            except Exception:
                pass


def pick_loudest_device(devices: list[dict]) -> dict | None:
    """Choose the loopback device that is actually playing audio.

    The default output is often not where sound is going (headset vs.
    speakers), and listening to the silent one looks exactly like a broken
    pipeline — so measure instead of assuming.
    """
    print("  probing devices for activity...")
    best, best_level = None, 0.0
    for d in devices:
        level = measure_device(d)
        print(f"    {d['name']}: peak={level:.4f}")
        if level > best_level:
            best, best_level = d, level
    if best is None or best_level < 0.001:
        return None
    return best


def run_listen(seconds: float) -> int:
    print("=" * 66)
    print(f"LISTENING for {seconds:.0f}s — play Russian audio now")
    print("=" * 66)

    devices = list_loopback_devices()
    if not devices:
        print("FAIL: no loopback devices")
        return 1

    chosen = pick_loudest_device(devices)
    if chosen is None:
        print("\n  No audio is playing on ANY output device right now.")
        print("  Start playing the Russian audio FIRST, then re-run this.")
        return 3
    print(f"  -> listening on: {chosen['name']}\n")

    results: list = []
    listener = build_listener(results, cfg={
        "debug": True,
        "voice": {"model_size": "small", "device_name": chosen["name"]},
    })
    if not listener.start():
        print(f"FAIL: listener did not start ({listener.last_error})")
        return 1
    print(f"  capturing from: {listener.device_name}")
    print("  loading model...")
    if not wait_until_ready(listener):
        print("FAIL: model never became ready")
        listener.stop()
        return 1
    print("  READY — play Russian speech now\n")

    deadline = time.time() + seconds
    seen = 0
    while time.time() < deadline:
        time.sleep(0.5)
        if len(results) > seen:
            for ru, en in results[seen:]:
                print(f"  >>> RU: {ru}")
                print(f"      EN: {en}\n")
            seen = len(results)
    listener.stop()

    print("-" * 66)
    print(f"  translated {len(results)} line(s)")
    return 0 if results else 2


def main() -> int:
    # Transcriptions are Cyrillic and the Windows console defaults to
    # cp1252, which raises UnicodeEncodeError on print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=float, default=0,
                    help="listen for N seconds instead of the TTS check")
    args = ap.parse_args()
    if args.listen:
        return run_listen(args.listen)
    return run_rejection_check()


if __name__ == "__main__":
    sys.exit(main())
