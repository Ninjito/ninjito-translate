"""Russian voice-chat capture, transcription and translation.

Dota 2 voice chat comes out of your *speakers*, not your microphone, so
this module captures the system render device via WASAPI **loopback**
(pyaudiowpatch) — i.e. "what you hear" — and pushes any Russian speech it
finds through faster-whisper, then through the same Translator/glossary
the chat OCR path uses.

Pipeline (two threads so a slow transcribe never drops audio):

    [capture thread]  loopback -> mono float32 @16 kHz -> energy VAD
                          |
                          v  utterances (0.6s .. 12s)
                    bounded queue (newest wins)
                          |
    [process thread]  faster-whisper -> reject filters -> translate
                          |
                          v
                    on_result(russian_text, english_text)

The loopback stream hears everything: spells, music, the announcer, hero
lines.  Four stacked filters keep that noise off the overlay — see
`_reject_reason` and `REJECT_*` constants.  The hallucination blocklist
matters most in practice: Whisper reliably invents YouTube-subtitle
credits when fed music or silence, and without the blocklist you get
phantom chat lines during every teamfight.

Nothing here raises into the caller: the listener is a background feature
and must never be able to kill the overlay.
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Audio constants
# ---------------------------------------------------------------------------

TARGET_RATE = 16000        # what Whisper wants
FRAME_MS = 30              # VAD frame size
FRAME_LEN = TARGET_RATE * FRAME_MS // 1000   # 480 samples

# Utterance shaping.
# Give up on a device whose warm-up transcription stalls. A CUDA model
# competing with Dota for the GPU can block inside detect_language()
# indefinitely rather than raising, which used to wedge load() forever.
PROBE_TIMEOUT_SEC = 20.0

# CTranslate2 needs these at CUDA runtime. It resolves them lazily, so
# their absence shows up as a hang or a late error rather than at model
# construction — we check up front instead.
CUDA_RUNTIME_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")

MIN_UTTERANCE_SEC = 0.6    # shorter than this is a click/blip, not speech
MAX_UTTERANCE_SEC = 12.0   # force a cut so long rants still get translated
SILENCE_HANGOVER_SEC = 0.7 # trailing silence that ends an utterance
PREROLL_SEC = 0.3          # audio kept from *before* speech was detected

# Energy gate. These are deliberately permissive — this VAD only decides
# *when to bother running Whisper*, and Whisper's own Silero VAD makes the
# real speech/non-speech call.
NOISE_FLOOR_ALPHA = 0.995  # slow adaptation of the background level
SPEECH_FACTOR = 3.0        # frame must be this much louder than the floor
ABS_SILENCE_RMS = 0.004    # ...and above this absolute level, always


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


MODEL_DIR = _app_dir() / "models"


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def list_loopback_devices() -> list[dict]:
    """Return every WASAPI loopback device as
    {index, name, rate, channels, is_default}.

    Returns [] if pyaudiowpatch is missing or WASAPI is unavailable, so
    callers can degrade to "voice unsupported" instead of crashing.
    """
    try:
        import pyaudiowpatch as pyaudio
    except Exception as e:
        print(f"[voice] pyaudiowpatch unavailable: {e}", flush=True)
        return []

    out: list[dict] = []
    p = None
    try:
        p = pyaudio.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_name = p.get_device_info_by_index(
                wasapi["defaultOutputDevice"]
            )["name"]
        except Exception:
            default_name = ""
        for lb in p.get_loopback_device_info_generator():
            out.append({
                "index": int(lb["index"]),
                "name": str(lb["name"]),
                "rate": int(lb["defaultSampleRate"]),
                "channels": int(lb["maxInputChannels"]),
                # The loopback device name is the render device name plus
                # a " [Loopback]" suffix, so substring-match it.
                "is_default": bool(default_name and default_name in str(lb["name"])),
            })
    except Exception as e:
        print(f"[voice] device enumeration failed: {e}", flush=True)
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
    return out


def cuda_libraries_available() -> bool:
    """True when CTranslate2's CUDA dependencies can actually be loaded.

    Having an NVIDIA GPU is not enough: the CUDA runtime is a separate
    install most players never do. CTranslate2 resolves these lazily, so
    without this check a GPU model builds happily and then either errors
    on the first utterance or — with the GPU busy rendering Dota — blocks
    inside its C extension and never returns.
    """
    if sys.platform != "win32":
        return True          # let CTranslate2 decide on other platforms
    try:
        import ctypes
    except Exception:
        return False
    for name in CUDA_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(name)
        except OSError:
            print(f"[voice] CUDA unavailable ({name} not loadable) — "
                  f"using CPU", flush=True)
            return False
    return True


def pick_device(devices: list[dict], want_name: str = "",
                want_index: int | None = None) -> dict | None:
    """Resolve a configured device to a real one.

    Name wins over index because device indices are reassigned whenever
    Windows enumerates audio hardware (headset sleeping, USB replug), and
    a stale index silently records the wrong device.  Falls back to the
    default output's loopback, then to the first device available.
    """
    if not devices:
        return None
    if want_name:
        for d in devices:
            if d["name"] == want_name:
                return d
    if want_index is not None:
        for d in devices:
            if d["index"] == want_index:
                return d
    for d in devices:
        if d["is_default"]:
            return d
    return devices[0]


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_to_16k(x: np.ndarray, src_rate: int) -> np.ndarray:
    """Downsample mono float32 audio to 16 kHz.

    Integer ratios (48k/32k -> 16k, the common cases) use a boxcar average
    over each group of `ratio` samples, which both anti-aliases and
    decimates in one pass — plenty for speech.  Non-integer rates fall
    back to linear interpolation.
    """
    if src_rate == TARGET_RATE or x.size == 0:
        return x.astype(np.float32, copy=False)

    ratio = src_rate / TARGET_RATE
    int_ratio = int(round(ratio))
    if int_ratio >= 2 and abs(ratio - int_ratio) < 1e-6:
        usable = (x.size // int_ratio) * int_ratio
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        return x[:usable].reshape(-1, int_ratio).mean(axis=1).astype(np.float32)

    n_out = int(round(x.size * TARGET_RATE / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0, x.size - 1, num=n_out, dtype=np.float64)
    return np.interp(src_idx, np.arange(x.size), x).astype(np.float32)


# ---------------------------------------------------------------------------
# Voice activity segmentation
# ---------------------------------------------------------------------------

class VadSegmenter:
    """Slice a continuous 16 kHz stream into speech utterances.

    Energy-based with an adaptive noise floor, so it self-tunes to game
    volume instead of needing a fixed threshold.  Feed it audio with
    `feed()`; it returns a list of finished utterances (float32 arrays).
    """

    def __init__(self,
                 min_sec: float = MIN_UTTERANCE_SEC,
                 max_sec: float = MAX_UTTERANCE_SEC,
                 hangover_sec: float = SILENCE_HANGOVER_SEC,
                 preroll_sec: float = PREROLL_SEC):
        self.min_len = int(min_sec * TARGET_RATE)
        self.max_len = int(max_sec * TARGET_RATE)
        self.hangover_frames = max(1, int(hangover_sec * 1000 / FRAME_MS))
        self.preroll_frames = max(1, int(preroll_sec * 1000 / FRAME_MS))

        self._tail = np.zeros(0, dtype=np.float32)   # leftover partial frame
        self._preroll: list[np.ndarray] = []
        self._active: list[np.ndarray] = []
        self._silence_run = 0
        self._noise_floor = 0.01
        self._in_speech = False
        # Counted separately from len(_active): the buffer also holds
        # pre-roll and hangover padding, which together always exceed
        # min_len.  Measuring the buffer would let every 0.2s click
        # through as a 1.2s "utterance" — and short noise is precisely
        # what makes Whisper hallucinate subtitle credits.
        self._speech_samples = 0

    def reset(self) -> None:
        self._tail = np.zeros(0, dtype=np.float32)
        self._preroll.clear()
        self._active.clear()
        self._silence_run = 0
        self._in_speech = False
        self._speech_samples = 0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Consume mono 16 kHz float32 audio, return finished utterances."""
        done: list[np.ndarray] = []
        if samples.size:
            self._tail = np.concatenate([self._tail, samples])

        n_frames = self._tail.size // FRAME_LEN
        if n_frames == 0:
            return done

        usable = n_frames * FRAME_LEN
        frames = self._tail[:usable].reshape(n_frames, FRAME_LEN)
        self._tail = self._tail[usable:]

        for frame in frames:
            rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
            is_speech = (rms > ABS_SILENCE_RMS
                         and rms > self._noise_floor * SPEECH_FACTOR)

            # Only quiet frames update the floor, otherwise loud continuous
            # speech would drag the threshold up above itself.
            if not is_speech:
                self._noise_floor = (NOISE_FLOOR_ALPHA * self._noise_floor
                                     + (1.0 - NOISE_FLOOR_ALPHA) * rms)

            if is_speech:
                if not self._in_speech:
                    self._in_speech = True
                    # Pull in the pre-roll so we don't clip the first
                    # syllable, which is what the energy gate always misses.
                    self._active = list(self._preroll)
                    self._preroll.clear()
                self._active.append(frame)
                self._speech_samples += frame.size
                self._silence_run = 0
                if sum(f.size for f in self._active) >= self.max_len:
                    done.append(self._finish())
            else:
                if self._in_speech:
                    self._active.append(frame)   # keep trailing silence
                    self._silence_run += 1
                    if self._silence_run >= self.hangover_frames:
                        utt = self._finish()
                        if utt is not None:
                            done.append(utt)
                else:
                    self._preroll.append(frame)
                    if len(self._preroll) > self.preroll_frames:
                        self._preroll.pop(0)

        return [u for u in done if u is not None]

    def _finish(self) -> np.ndarray | None:
        """Close the current utterance; None if it held too little speech."""
        self._in_speech = False
        self._silence_run = 0
        speech = self._speech_samples
        self._speech_samples = 0
        if not self._active:
            return None
        utt = np.concatenate(self._active)
        self._active = []
        # Judge on actual speech, not the padded buffer length.
        if speech < self.min_len:
            return None
        return utt


# ---------------------------------------------------------------------------
# Reject filters
# ---------------------------------------------------------------------------

# Whisper's signature failure mode on music, silence and game SFX is to
# emit fragments of YouTube subtitle boilerplate it saw in training. These
# are matched as normalized lowercase substrings.
HALLUCINATION_PHRASES = (
    "субтитры",
    "субтитр",
    "dimatorzok",
    "редактор субтитров",
    "корректор",
    "продолжение следует",
    "спасибо за просмотр",
    "спасибо за внимание",
    "подписывайтесь на канал",
    "подпишись на канал",
    "поставьте лайк",
    "ставьте лайк",
    "не забудьте подписаться",
    "всем пока",
    "до новых встреч",
    "перевод и озвучка",
    "озвучка",
    "аудиовизуальный перевод",
    "игорь негода",
    "фонд кино",
    "продолжение в следующей",
    "смотрите в следующей серии",
    "конец первой части",
    "музыка играет",
    "звучит музыка",
    "аплодисменты",
    "смех",
)

# A URL in "speech" is always a hallucination.
_URL_RE = re.compile(r"(https?://|www\.|\.ru\b|\.com\b|\.org\b)", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)

REJECT_NONE = ""
REJECT_EMPTY = "empty"
REJECT_HALLUCINATION = "hallucination"
REJECT_URL = "url"
REJECT_REPEAT = "repeat-loop"
REJECT_NO_CYRILLIC = "no-cyrillic"
REJECT_TOO_SHORT = "too-short"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_hallucination(text: str) -> bool:
    """True if `text` looks like Whisper subtitle-credit boilerplate."""
    norm = _normalize(text)
    if not norm:
        return False
    return any(p in norm for p in HALLUCINATION_PHRASES)


def is_repeat_loop(text: str, min_words: int = 4, ratio: float = 0.6) -> bool:
    """True if one token dominates the line.

    Whisper falls into "да да да да да" loops on sustained non-speech
    audio; real chatter almost never repeats one word past 60%.
    """
    words = _WORD_RE.findall(_normalize(text))
    if len(words) < min_words:
        return False
    top = max(words.count(w) for w in set(words))
    return top / len(words) >= ratio


def has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= c <= "ӿ" for c in text)


def _reject_reason(text: str, min_chars: int = 3) -> str:
    """Return a REJECT_* reason for `text`, or REJECT_NONE to keep it."""
    stripped = text.strip()
    if not stripped:
        return REJECT_EMPTY
    if len(stripped) < min_chars:
        return REJECT_TOO_SHORT
    if not has_cyrillic(stripped):
        return REJECT_NO_CYRILLIC
    if is_hallucination(stripped):
        return REJECT_HALLUCINATION
    if _URL_RE.search(stripped):
        return REJECT_URL
    if is_repeat_loop(stripped):
        return REJECT_REPEAT
    return REJECT_NONE


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

class Transcriber:
    """faster-whisper wrapper with lazy, thread-safe model loading.

    The model is several hundred MB and downloads on first use, so it is
    NOT loaded in __init__ — `load()` is called from the process thread
    and reports progress through `on_status`.
    """

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "int8", lang_prob_min: float = 0.6,
                 initial_prompt: str = "", on_status=None):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.lang_prob_min = lang_prob_min
        self.initial_prompt = initial_prompt or ""
        self._on_status = on_status
        self._model = None
        self._lock = threading.Lock()
        self.last_error = ""

    def _status(self, text: str, color: str = "#888") -> None:
        if self._on_status:
            try:
                self._on_status(text, color)
            except Exception:
                pass

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _probe(self, model) -> None:
        """Force the model's lazy device init, abandoning a stalled device.

        `transcribe()` returns a generator, so nothing touches the GPU
        until it is consumed.  Draining it here makes a broken device fail
        during load() rather than on the user's first real utterance.

        The probe runs on its own thread because a CUDA model contending
        with Dota for the GPU blocks inside CTranslate2's C extension
        instead of raising — and a blocked C call cannot be interrupted
        from Python.  On timeout we abandon the thread (daemon, so it dies
        with the process) and let the caller fall back to CPU.
        """
        audio = (np.random.RandomState(0).randn(TARGET_RATE)
                 * 0.05).astype(np.float32)
        failure: list[BaseException] = []

        def _run() -> None:
            try:
                segments, _ = model.transcribe(
                    audio, beam_size=1, vad_filter=False,
                    without_timestamps=True,
                )
                list(segments)   # force the lazy generator to compute
            except BaseException as e:     # noqa: BLE001 - reported to caller
                failure.append(e)

        thread = threading.Thread(target=_run, daemon=True,
                                  name="whisper-probe")
        thread.start()
        thread.join(PROBE_TIMEOUT_SEC)
        if thread.is_alive():
            raise TimeoutError(
                f"device did not respond within {PROBE_TIMEOUT_SEC:.0f}s"
            )
        if failure:
            raise failure[0]

    def _build(self, device: str, compute_type: str):
        """Construct a model on `device` and prove it can actually run."""
        from faster_whisper import WhisperModel

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        model = WhisperModel(
            self.model_size,
            device=device,
            compute_type=compute_type,
            download_root=str(MODEL_DIR),
        )
        self._probe(model)
        return model

    def load(self) -> bool:
        """Load (downloading on first run) the Whisper model. False on failure."""
        with self._lock:
            if self._model is not None:
                return True
            try:
                import faster_whisper  # noqa: F401
            except Exception as e:
                self.last_error = f"faster-whisper not installed: {e}"
                print(f"[voice] {self.last_error}", flush=True)
                self._status("Voice: faster-whisper missing", "#ff4444")
                return False

            self._status(f"Voice: loading {self.model_size} model...", "#ffa500")
            print(f"[voice] loading whisper '{self.model_size}' "
                  f"({self.compute_type}) — first run downloads the model",
                  flush=True)

            # "auto" means "use the GPU only if it genuinely works" — a
            # machine can have an NVIDIA card but no CUDA runtime, which is
            # the common case for users who never installed the toolkit.
            # The preflight matters more than it looks: without it we build
            # a CUDA model that can block forever on first use instead of
            # raising, and load() never returns.
            if self.device == "cpu":
                attempts = [("cpu", "int8")]
            else:
                attempts = [("cpu", "int8")]
                if cuda_libraries_available():
                    attempts.insert(0, ("cuda", self.compute_type))

            for device, compute_type in attempts:
                try:
                    self._model = self._build(device, compute_type)
                    self.device = device
                    self.compute_type = compute_type
                    print(f"[voice] whisper ready on {device} ({compute_type})",
                          flush=True)
                    self._status("Voice: listening", "#7bd88f")
                    return True
                except Exception as e:
                    self.last_error = str(e)
                    if device != attempts[-1][0]:
                        print(f"[voice] {device} unusable ({e}); "
                              f"falling back to CPU", flush=True)
                    else:
                        print(f"[voice] model load failed: {e}", flush=True)

            self._status("Voice: model load failed", "#ff4444")
            return False

    def transcribe(self, audio: np.ndarray) -> tuple[str, str, float, float]:
        """Return (text, language, language_probability, avg_logprob).

        Language is auto-detected rather than forced to Russian: forcing
        "ru" onto English announcer lines produces plausible-looking
        transliterated garbage, whereas detection cleanly labels it "en"
        and we drop it.
        """
        if self._model is None:
            return ("", "", 0.0, -99.0)

        segments, info = self._model.transcribe(
            audio,
            beam_size=1,                     # greedy — fast enough, less looping
            vad_filter=True,                 # Whisper's own Silero VAD
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,  # stops runaway hallucination loops
            temperature=0.0,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            initial_prompt=self.initial_prompt or None,
        )
        parts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            parts.append(seg.text)
            logprobs.append(float(getattr(seg, "avg_logprob", -1.0)))

        text = " ".join(p.strip() for p in parts).strip()
        avg_lp = float(np.mean(logprobs)) if logprobs else -99.0
        return (text,
                str(getattr(info, "language", "") or ""),
                float(getattr(info, "language_probability", 0.0) or 0.0),
                avg_lp)


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "Разговор игроков в Dota 2: мид, лес, руна, вард, рошан, "
    "гангк, пуш, отступаем, атакуем, бараки."
)


class VoiceListener:
    """Owns the capture + process threads. Safe to start/stop repeatedly."""

    def __init__(self, cfg: dict, translator, on_result,
                 on_status=None, glossary_map: dict | None = None):
        """
        cfg          — the app config dict (reads its "voice" block live)
        translator   — dota_ocr.translator.Translator instance
        on_result    — callback(russian_text, english_text)
        on_status    — callback(text, color) for the overlay status line
        glossary_map — optional glossary dict applied before translation
        """
        self._cfg = cfg
        self._translator = translator
        self._on_result = on_result
        self._on_status = on_status
        self._glossary = glossary_map or {}

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # Bounded: during a teamfight we would rather drop the oldest
        # utterance than build a queue the transcriber can never catch up on.
        self._utterances: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)
        self._transcriber: Transcriber | None = None
        self._recent: list[tuple[str, float]] = []   # (normalized text, time)
        self._running = False
        self.device_name = ""
        self.last_error = ""

    # -- config helpers -----------------------------------------------------
    def _vcfg(self) -> dict:
        return dict((self._cfg or {}).get("voice") or {})

    def _status(self, text: str, color: str = "#888") -> None:
        if self._on_status:
            try:
                self._on_status(text, color)
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._running

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        if self._running:
            return True
        v = self._vcfg()
        devices = list_loopback_devices()
        if not devices:
            self.last_error = "no WASAPI loopback device"
            print("[voice] no loopback devices — voice disabled", flush=True)
            self._status("Voice: no audio device", "#ff4444")
            return False

        dev = pick_device(devices,
                          want_name=str(v.get("device_name", "") or ""),
                          want_index=v.get("device_index"))
        if dev is None:
            self.last_error = "device resolution failed"
            self._status("Voice: no audio device", "#ff4444")
            return False
        self.device_name = dev["name"]

        self._transcriber = Transcriber(
            model_size=str(v.get("model_size", "small")),
            device=str(v.get("compute_device", "auto")),
            compute_type=str(v.get("compute_type", "int8")),
            lang_prob_min=float(v.get("lang_prob_min", 0.6)),
            initial_prompt=(DEFAULT_PROMPT if v.get("use_dota_prompt", True) else ""),
            on_status=self._on_status,
        )

        self._stop.clear()
        self._running = True
        self._threads = [
            threading.Thread(target=self._capture_loop, args=(dev,),
                             name="voice-capture", daemon=True),
            threading.Thread(target=self._process_loop,
                             name="voice-process", daemon=True),
        ]
        for t in self._threads:
            t.start()
        print(f"[voice] listening on {dev['name']!r}", flush=True)
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        self._running = False
        for t in self._threads:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._threads = []
        # Drop any queued audio so a restart doesn't replay stale speech.
        while True:
            try:
                self._utterances.get_nowait()
            except queue.Empty:
                break
        print("[voice] stopped", flush=True)
        self._status("Voice: off", "#888")

    # -- capture ------------------------------------------------------------
    def _capture_loop(self, dev: dict) -> None:
        """Read the loopback stream, segment it, enqueue utterances.

        Reopens the stream on error (headset sleeping, device switch)
        rather than dying, since this thread has to survive a whole match.
        """
        import pyaudiowpatch as pyaudio

        segmenter = VadSegmenter()
        chunk = 1024

        while not self._stop.is_set():
            p = None
            stream = None
            try:
                p = pyaudio.PyAudio()
                rate = int(dev["rate"])
                channels = int(dev["channels"])
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=chunk,
                    input=True,
                    input_device_index=int(dev["index"]),
                )
                segmenter.reset()

                while not self._stop.is_set():
                    raw = stream.read(chunk, exception_on_overflow=False)
                    if not raw:
                        continue
                    mono = self._to_mono_16k(raw, channels, rate)
                    for utt in segmenter.feed(mono):
                        self._enqueue(utt)

            except Exception as e:
                if self._stop.is_set():
                    break
                self.last_error = str(e)
                print(f"[voice] capture error: {e} — reopening in 2s", flush=True)
                self._status("Voice: audio device error", "#ff8844")
                time.sleep(2.0)
            finally:
                for closer in (
                    lambda: stream.stop_stream() if stream else None,
                    lambda: stream.close() if stream else None,
                    lambda: p.terminate() if p else None,
                ):
                    try:
                        closer()
                    except Exception:
                        pass

    @staticmethod
    def _to_mono_16k(raw: bytes, channels: int, rate: int) -> np.ndarray:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            usable = (audio.size // channels) * channels
            audio = audio[:usable].reshape(-1, channels).mean(axis=1)
        return resample_to_16k(audio, rate)

    def _enqueue(self, utt: np.ndarray) -> None:
        """Queue an utterance, dropping the oldest if the queue is full."""
        try:
            self._utterances.put_nowait(utt)
        except queue.Full:
            try:
                self._utterances.get_nowait()
                self._utterances.put_nowait(utt)
                print("[voice] queue full — dropped oldest utterance", flush=True)
            except Exception:
                pass

    # -- process ------------------------------------------------------------
    def _process_loop(self) -> None:
        tr = self._transcriber
        if tr is None or not tr.load():
            self._running = False
            return

        while not self._stop.is_set():
            try:
                utt = self._utterances.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._handle_utterance(utt)
            except Exception as e:
                print(f"[voice] process error: {e}", flush=True)

    def _handle_utterance(self, utt: np.ndarray) -> None:
        v = self._vcfg()
        tr = self._transcriber
        if tr is None:
            return

        # Cheap guard: a segment that is essentially silence is the single
        # most reliable way to make Whisper hallucinate. Never send one.
        peak = float(np.abs(utt).max()) if utt.size else 0.0
        if peak < ABS_SILENCE_RMS:
            return

        t0 = time.monotonic()
        text, lang, lang_prob, avg_lp = tr.transcribe(utt)
        elapsed = time.monotonic() - t0

        if not text.strip():
            return

        debug = bool((self._cfg or {}).get("debug"))
        if debug:
            print(f"[voice] {elapsed:.1f}s lang={lang}({lang_prob:.2f}) "
                  f"lp={avg_lp:.2f} {text!r}", flush=True)

        # Filter 1 — language. English announcer/hero lines land here.
        min_prob = float(v.get("lang_prob_min", 0.6))
        if lang != "ru" or lang_prob < min_prob:
            print(f"[voice-skip:lang] {lang}({lang_prob:.2f}) {text!r}", flush=True)
            return

        # Filter 2 — acoustic confidence. Music and SFX score badly.
        min_lp = float(v.get("min_avg_logprob", -1.0))
        if avg_lp < min_lp:
            print(f"[voice-skip:conf] lp={avg_lp:.2f} {text!r}", flush=True)
            return

        # Filter 3 — text shape (Cyrillic / hallucination / repeat loop).
        reason = _reject_reason(text)
        if reason:
            print(f"[voice-skip:{reason}] {text!r}", flush=True)
            return

        # Filter 4 — don't re-show the same line the model emits twice when
        # a long utterance gets split across two segments.
        if self._is_duplicate(text):
            print(f"[voice-skip:dup] {text!r}", flush=True)
            return

        # --- Translate through the same path the chat OCR uses ---
        try:
            from dota_ocr import glossary as _gloss
            src_text = _gloss.apply(text, self._glossary) if self._glossary else text
        except Exception:
            src_text = text

        try:
            english = self._translator.translate(src_text, src="ru",
                                                 target_language="en")
        except Exception as e:
            print(f"[voice] translate failed: {e}", flush=True)
            return

        if not english or not english.strip():
            return
        # Google echoes the input back when it can't parse garbled text.
        if _normalize(english) == _normalize(src_text) or has_cyrillic(english):
            print(f"[voice-skip:echo] {english!r}", flush=True)
            return

        print(f"[voice] {text!r} -> {english!r}", flush=True)
        try:
            self._on_result(text, english)
        except Exception as e:
            print(f"[voice] result callback failed: {e}", flush=True)

    def _is_duplicate(self, text: str, window_sec: float = 8.0) -> bool:
        now = time.monotonic()
        norm = _normalize(text)
        self._recent = [(t, ts) for t, ts in self._recent
                        if now - ts < window_sec]
        if any(t == norm for t, _ in self._recent):
            return True
        self._recent.append((norm, now))
        return False
