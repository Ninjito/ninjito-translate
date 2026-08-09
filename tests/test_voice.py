"""Tests for the voice capture/transcription pipeline.

These cover everything except Whisper itself: resampling, utterance
segmentation, the game-audio reject filters and device resolution.  No
audio hardware and no model download is required, so they run anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr import voice
from dota_ocr.voice import (
    FRAME_LEN,
    TARGET_RATE,
    Transcriber,
    VadSegmenter,
    VoiceListener,
    _reject_reason,
    REJECT_EMPTY,
    REJECT_HALLUCINATION,
    REJECT_NONE,
    REJECT_NO_CYRILLIC,
    REJECT_REPEAT,
    REJECT_TOO_SHORT,
    REJECT_URL,
    has_cyrillic,
    is_hallucination,
    is_repeat_loop,
    pick_device,
    resample_to_16k,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def tone(seconds: float, rate: int = TARGET_RATE, freq: float = 220.0,
         amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float, rate: int = TARGET_RATE,
            amp: float = 0.0001) -> np.ndarray:
    """Near-silence with a touch of noise, like a real idle audio device."""
    n = int(seconds * rate)
    return (np.random.RandomState(0).randn(n) * amp).astype(np.float32)


# --------------------------------------------------------------------------
# resampling
# --------------------------------------------------------------------------

class TestResample:
    def test_passthrough_when_already_16k(self):
        x = tone(0.1)
        out = resample_to_16k(x, TARGET_RATE)
        assert out is x or np.array_equal(out, x)

    def test_48k_to_16k_length(self):
        x = tone(1.0, rate=48000)
        out = resample_to_16k(x, 48000)
        assert out.size == TARGET_RATE
        assert out.dtype == np.float32

    def test_44100_to_16k_length(self):
        """Non-integer ratio takes the interpolation path."""
        x = tone(1.0, rate=44100)
        out = resample_to_16k(x, 44100)
        assert abs(out.size - TARGET_RATE) <= 1

    def test_preserves_tone_frequency(self):
        """A 440 Hz tone must still peak at 440 Hz after downsampling,
        proving we anti-alias rather than naively decimate."""
        x = tone(1.0, rate=48000, freq=440.0)
        out = resample_to_16k(x, 48000)
        spectrum = np.abs(np.fft.rfft(out))
        peak_hz = np.fft.rfftfreq(out.size, 1 / TARGET_RATE)[spectrum.argmax()]
        assert abs(peak_hz - 440.0) < 5.0

    def test_empty_input(self):
        assert resample_to_16k(np.zeros(0, dtype=np.float32), 48000).size == 0

    def test_shorter_than_ratio(self):
        """Fewer samples than the decimation factor must not crash."""
        out = resample_to_16k(np.ones(2, dtype=np.float32), 48000)
        assert out.size == 0


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------

class TestVadSegmenter:
    def test_silence_yields_nothing(self):
        seg = VadSegmenter()
        assert seg.feed(silence(3.0)) == []

    def test_single_burst_is_one_utterance(self):
        seg = VadSegmenter()
        out = []
        out += seg.feed(silence(1.0))
        out += seg.feed(tone(2.0))
        out += seg.feed(silence(1.5))
        assert len(out) == 1
        # 2s of speech plus pre-roll and hangover, minus nothing.
        assert 2.0 * TARGET_RATE <= out[0].size <= 3.5 * TARGET_RATE

    def test_two_bursts_separated_by_silence(self):
        seg = VadSegmenter()
        out = []
        out += seg.feed(silence(0.5))
        out += seg.feed(tone(1.0))
        out += seg.feed(silence(1.5))
        out += seg.feed(tone(1.0))
        out += seg.feed(silence(1.5))
        assert len(out) == 2

    def test_short_blip_is_dropped(self):
        """A 0.2s click is below MIN_UTTERANCE_SEC and must not be sent
        to Whisper — short noise is what makes it hallucinate."""
        seg = VadSegmenter()
        out = []
        out += seg.feed(silence(1.0))
        out += seg.feed(tone(0.2))
        out += seg.feed(silence(1.5))
        assert out == []

    def test_long_speech_is_force_cut(self):
        """Continuous speech past MAX_UTTERANCE_SEC still produces output
        instead of buffering forever."""
        seg = VadSegmenter()
        out = seg.feed(tone(30.0))
        assert len(out) >= 2
        for utt in out:
            assert utt.size <= int(voice.MAX_UTTERANCE_SEC * TARGET_RATE) + FRAME_LEN

    def test_preroll_keeps_speech_onset(self):
        """The utterance must start before the energy gate opened, so the
        first syllable isn't clipped."""
        seg = VadSegmenter()
        seg.feed(silence(1.0))
        out = seg.feed(tone(1.0)) + seg.feed(silence(1.5))
        assert len(out) == 1
        assert out[0].size > 1.0 * TARGET_RATE

    def test_reset_clears_partial_utterance(self):
        seg = VadSegmenter()
        seg.feed(silence(0.5))
        seg.feed(tone(1.0))       # mid-utterance
        seg.reset()
        assert seg.feed(silence(1.5)) == []

    def test_handles_partial_frames(self):
        """Chunk sizes that aren't a multiple of the frame length must be
        buffered, not dropped."""
        seg = VadSegmenter()
        out = []
        odd = 777
        speech = tone(2.0)
        for i in range(0, speech.size, odd):
            out += seg.feed(speech[i:i + odd])
        out += seg.feed(silence(1.5))
        assert len(out) == 1


# --------------------------------------------------------------------------
# reject filters
# --------------------------------------------------------------------------

class TestHallucinationFilter:
    @pytest.mark.parametrize("text", [
        "Субтитры сделал DimaTorzok",
        "субтитры создавал DimaTorzok",
        "Редактор субтитров А.Синецкая",
        "Продолжение следует...",
        "Спасибо за просмотр!",
        "Подписывайтесь на канал",
        "Всем пока!",
        "[Музыка играет]",
    ])
    def test_known_hallucinations_rejected(self, text):
        assert is_hallucination(text) is True

    @pytest.mark.parametrize("text", [
        "иди мид быстро",
        "они пошли на рошана",
        "у них нет вардов",
        "отступаем, их пятеро",
    ])
    def test_real_chat_survives(self, text):
        assert is_hallucination(text) is False


class TestRepeatLoop:
    def test_repeated_word_loop_rejected(self):
        assert is_repeat_loop("да да да да да да да") is True

    def test_normal_sentence_kept(self):
        assert is_repeat_loop("он идёт на нашу базу сейчас") is False

    def test_short_repetition_allowed(self):
        """'да да' is a real thing people say — too short to be a loop."""
        assert is_repeat_loop("да да") is False


class TestCyrillic:
    def test_russian_detected(self):
        assert has_cyrillic("привет") is True

    def test_english_not_detected(self):
        assert has_cyrillic("hello world") is False

    def test_mixed_detected(self):
        assert has_cyrillic("gg всем") is True


class TestRejectReason:
    def test_good_line_accepted(self):
        assert _reject_reason("они идут на рошана") == REJECT_NONE

    def test_empty_rejected(self):
        assert _reject_reason("   ") == REJECT_EMPTY

    def test_too_short_rejected(self):
        assert _reject_reason("ок") == REJECT_TOO_SHORT

    def test_english_rejected(self):
        assert _reject_reason("they are pushing mid") == REJECT_NO_CYRILLIC

    def test_hallucination_rejected(self):
        assert _reject_reason("Субтитры сделал DimaTorzok") == REJECT_HALLUCINATION

    def test_url_rejected(self):
        assert _reject_reason("заходи на сайт www.example.com") == REJECT_URL

    def test_repeat_loop_rejected(self):
        assert _reject_reason("да да да да да да") == REJECT_REPEAT


# --------------------------------------------------------------------------
# device resolution
# --------------------------------------------------------------------------

DEVICES = [
    {"index": 13, "name": "Speakers (HyperX Cloud III S Wireless) [Loopback]",
     "rate": 48000, "channels": 2, "is_default": False},
    {"index": 14, "name": "Speakers (Realtek High Definition Audio) [Loopback]",
     "rate": 48000, "channels": 2, "is_default": True},
]


class TestPickDevice:
    def test_empty_list_returns_none(self):
        assert pick_device([]) is None

    def test_name_match_wins(self):
        got = pick_device(DEVICES, want_name=DEVICES[0]["name"], want_index=14)
        assert got["index"] == 13, "name must take priority over a stale index"

    def test_index_used_when_name_missing(self):
        got = pick_device(DEVICES, want_name="", want_index=13)
        assert got["index"] == 13

    def test_falls_back_to_default_when_name_gone(self):
        """Headset unplugged: the configured name no longer exists, so we
        must fall back to the default output rather than return nothing."""
        got = pick_device(DEVICES, want_name="Gone (Unplugged) [Loopback]")
        assert got["index"] == 14

    def test_falls_back_to_first_when_no_default(self):
        devs = [dict(d, is_default=False) for d in DEVICES]
        assert pick_device(devs)["index"] == 13


# --------------------------------------------------------------------------
# channel downmix
# --------------------------------------------------------------------------

class TestToMono16k:
    def test_stereo_downmix_and_resample(self):
        """1s of 48 kHz stereo int16 must become 16000 mono samples."""
        n = 48000
        left = (np.ones(n) * 10000).astype(np.int16)
        right = (np.ones(n) * -10000).astype(np.int16)
        interleaved = np.empty(n * 2, dtype=np.int16)
        interleaved[0::2] = left
        interleaved[1::2] = right

        out = VoiceListener._to_mono_16k(interleaved.tobytes(), 2, 48000)
        assert out.size == TARGET_RATE
        # L and R cancel exactly, so the downmix must be ~0.
        assert np.abs(out).max() < 1e-6

    def test_mono_passthrough(self):
        n = 16000
        data = (np.ones(n) * 16384).astype(np.int16)
        out = VoiceListener._to_mono_16k(data.tobytes(), 1, 16000)
        assert out.size == n
        assert np.allclose(out, 0.5, atol=1e-3)


# --------------------------------------------------------------------------
# model device selection
# --------------------------------------------------------------------------

class TestTranscriberDeviceFallback:
    """CTranslate2 loads CUDA libraries lazily, so a machine with an
    NVIDIA card but no CUDA runtime builds a GPU model successfully and
    only fails on the first real transcription.  `load()` must discover
    that during startup and fall back, not hand back a broken model.
    """

    def _transcriber(self, device="auto"):
        return Transcriber(model_size="tiny", device=device)

    def test_falls_back_to_cpu_when_cuda_unusable(self, monkeypatch):
        tried: list[str] = []

        def fake_build(self, device, compute_type):
            tried.append(device)
            if device == "cuda":
                raise RuntimeError(
                    "Library cublas64_12.dll is not found or cannot be loaded")
            return object()

        monkeypatch.setattr(Transcriber, "_build", fake_build)
        tr = self._transcriber()
        assert tr.load() is True
        assert tried == ["cuda", "cpu"]
        assert tr.device == "cpu"
        assert tr.ready is True

    def test_uses_cuda_when_it_works(self, monkeypatch):
        tried: list[str] = []

        def fake_build(self, device, compute_type):
            tried.append(device)
            return object()

        monkeypatch.setattr(Transcriber, "_build", fake_build)
        tr = self._transcriber()
        assert tr.load() is True
        assert tried == ["cuda"], "must not probe CPU once CUDA succeeded"
        assert tr.device == "cuda"

    def test_cpu_setting_never_probes_cuda(self, monkeypatch):
        tried: list[str] = []

        def fake_build(self, device, compute_type):
            tried.append(device)
            return object()

        monkeypatch.setattr(Transcriber, "_build", fake_build)
        tr = self._transcriber(device="cpu")
        assert tr.load() is True
        assert tried == ["cpu"]

    def test_total_failure_reports_false(self, monkeypatch):
        def fake_build(self, device, compute_type):
            raise RuntimeError("no backend")

        monkeypatch.setattr(Transcriber, "_build", fake_build)
        tr = self._transcriber()
        assert tr.load() is False
        assert tr.ready is False
        assert "no backend" in tr.last_error

    def test_load_is_idempotent(self, monkeypatch):
        calls: list[str] = []

        def fake_build(self, device, compute_type):
            calls.append(device)
            return object()

        monkeypatch.setattr(Transcriber, "_build", fake_build)
        tr = self._transcriber()
        tr.load()
        tr.load()
        assert len(calls) == 1, "model must only be built once"

    def test_transcribe_without_model_is_safe(self):
        """A failed load must not turn every utterance into an exception."""
        tr = self._transcriber()
        text, lang, prob, lp = tr.transcribe(np.zeros(16000, dtype=np.float32))
        assert text == ""
        assert lang == ""


# --------------------------------------------------------------------------
# duplicate suppression
# --------------------------------------------------------------------------

class TestDuplicateSuppression:
    def _listener(self):
        return VoiceListener(cfg={}, translator=None, on_result=lambda a, b: None)

    def test_first_occurrence_allowed(self):
        vl = self._listener()
        assert vl._is_duplicate("они на рошане") is False

    def test_immediate_repeat_blocked(self):
        vl = self._listener()
        vl._is_duplicate("они на рошане")
        assert vl._is_duplicate("они на рошане") is True

    def test_case_and_space_insensitive(self):
        vl = self._listener()
        vl._is_duplicate("Они  на рошане")
        assert vl._is_duplicate("они на рошане") is True

    def test_different_text_allowed(self):
        vl = self._listener()
        vl._is_duplicate("они на рошане")
        assert vl._is_duplicate("отступаем") is False

    def test_expires_after_window(self):
        vl = self._listener()
        vl._is_duplicate("они на рошане")
        assert vl._is_duplicate("они на рошане", window_sec=0.0) is False
