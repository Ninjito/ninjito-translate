"""End-to-end tests for VoiceListener._handle_utterance.

These drive the full decision path — confidence gating, language gating,
text filters, deduplication, glossary, translation and the result
callback — with the Whisper acoustic model stubbed out.  Whisper itself is
a well-tested third-party component; what matters here is that *our*
filters let the right lines through and drop the rest.

One test at the bottom hits the real translation API and is skipped when
the network is unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.voice import VoiceListener


class StubTranscriber:
    """Stands in for faster-whisper, returning a canned result."""

    def __init__(self, text: str, lang: str = "ru", prob: float = 0.95,
                 logprob: float = -0.35, no_speech: float = 0.05):
        self.result = (text, lang, prob, logprob)
        self.last_no_speech_prob = no_speech
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.result


class StubTranslator:
    """Uppercases as a stand-in for translation, so output is traceable."""

    def __init__(self, output: str = "they are going to roshan"):
        self.output = output
        self.seen: list[str] = []

    def translate(self, text, src="auto", target_language=""):
        self.seen.append(text)
        return self.output


def loud_audio(seconds: float = 1.5) -> np.ndarray:
    """Audio well above the silence gate."""
    n = int(seconds * 16000)
    t = np.arange(n) / 16000
    return (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


def make_listener(transcriber, translator=None, cfg=None, glossary_map=None):
    results: list[tuple[str, str]] = []
    vl = VoiceListener(
        cfg=cfg if cfg is not None else {"voice": {}},
        translator=translator or StubTranslator(),
        on_result=lambda ru, en: results.append((ru, en)),
        glossary_map=glossary_map,
    )
    vl._transcriber = transcriber
    return vl, results


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

class TestAcceptedSpeech:
    def test_russian_speech_reaches_callback(self):
        vl, results = make_listener(
            StubTranscriber("они идут на рошана"),
            StubTranslator("they are going to roshan"),
        )
        vl._handle_utterance(loud_audio())
        assert results == [("они идут на рошана", "they are going to roshan")]

    def test_translator_receives_russian_source(self):
        tr = StubTranslator("push mid")
        vl, _ = make_listener(StubTranscriber("пушим мид"), tr)
        vl._handle_utterance(loud_audio())
        assert tr.seen == ["пушим мид"]

    def test_glossary_applied_before_translation(self):
        """Custom glossary terms must be substituted on the way in, the
        same as the chat OCR path does."""
        tr = StubTranslator("Roshan is up")
        vl, _ = make_listener(
            StubTranscriber("рошан живой"), tr,
            glossary_map={"рошан": "Roshan"},
        )
        vl._handle_utterance(loud_audio())
        assert "Roshan" in tr.seen[0]


# --------------------------------------------------------------------------
# game-audio rejection
# --------------------------------------------------------------------------

class TestRejection:
    def test_english_announcer_dropped(self):
        """Dota's announcer and hero lines are English — the language
        gate is what keeps them off the overlay."""
        vl, results = make_listener(
            StubTranscriber("first blood", lang="en", prob=0.98))
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_long_noise_dropped_by_no_speech_prob(self):
        """no_speech_prob carries noise rejection at every length now.

        The language label used to do this job, but on game-mixed audio
        it mislabels real Russian (a genuine call came back as en(0.23)),
        so it only ever vetoed teammates. Non-Cyrillic text is rejected
        separately, which is what actually catches English game audio."""
        vl, results = make_listener(
            StubTranscriber("что то там", lang="ru", prob=0.3,
                            no_speech=0.9))
        vl._handle_utterance(loud_audio(4.0))
        assert results == []

    def test_long_russian_with_weak_language_label_is_kept(self):
        vl, results = make_listener(
            StubTranscriber("они идут на рошана", lang="en", prob=0.23,
                            no_speech=0.05))
        vl._handle_utterance(loud_audio(4.0))
        assert len(results) == 1

    def test_short_low_confidence_is_kept(self):
        """The regression this whole path exists for: real in-game calls
        are short, and lang_prob is unreliable on short audio. Dropping
        these lost the most useful messages."""
        vl, results = make_listener(
            StubTranscriber("беги", lang="ru", prob=0.3))
        vl._handle_utterance(loud_audio(1.2))
        assert len(results) == 1

    def test_short_noise_dropped_by_no_speech_prob(self):
        """Short clips lean on no_speech_prob instead of lang_prob, so
        music that transcribes to Cyrillic is still rejected."""
        vl, results = make_listener(
            StubTranscriber("что то там", lang="ru", prob=0.3,
                            no_speech=0.95))
        vl._handle_utterance(loud_audio(1.2))
        assert results == []

    def test_short_english_dropped(self):
        """Nothing vouches for Latin text on the short path."""
        vl, results = make_listener(
            StubTranscriber("first blood", lang="en", prob=0.3))
        vl._handle_utterance(loud_audio(1.2))
        assert results == []

    def test_low_acoustic_confidence_dropped(self):
        vl, results = make_listener(
            StubTranscriber("они идут на рошана", logprob=-2.5))
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_hallucinated_subtitle_credit_dropped(self):
        """The signature Whisper failure on music/silence."""
        vl, results = make_listener(
            StubTranscriber("Субтитры сделал DimaTorzok"))
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_repeat_loop_dropped(self):
        vl, results = make_listener(StubTranscriber("да да да да да да"))
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_empty_transcription_ignored(self):
        vl, results = make_listener(StubTranscriber("   "))
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_silent_audio_never_reaches_whisper(self):
        """Silence is the most reliable hallucination trigger, so it must
        be rejected before the model ever sees it."""
        stub = StubTranscriber("Продолжение следует...")
        vl, results = make_listener(stub)
        vl._handle_utterance(np.zeros(16000, dtype=np.float32))
        assert stub.calls == 0
        assert results == []

    def test_translation_echo_dropped(self):
        """Google echoes input back when it can't parse garbled text."""
        vl, results = make_listener(
            StubTranscriber("они идут на рошана"),
            StubTranslator("они идут на рошана"),
        )
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_cyrillic_output_dropped(self):
        """A 'translation' still in Russian means translation failed."""
        vl, results = make_listener(
            StubTranscriber("они идут на рошана"),
            StubTranslator("они бегут"),
        )
        vl._handle_utterance(loud_audio())
        assert results == []

    def test_empty_translation_dropped(self):
        vl, results = make_listener(
            StubTranscriber("они идут на рошана"), StubTranslator(""))
        vl._handle_utterance(loud_audio())
        assert results == []


# --------------------------------------------------------------------------
# duplicate suppression across utterances
# --------------------------------------------------------------------------

class TestDeduplication:
    def test_same_line_twice_shown_once(self):
        """Whisper often re-emits a phrase when an utterance is split
        across two VAD segments."""
        vl, results = make_listener(StubTranscriber("отступаем"))
        vl._handle_utterance(loud_audio())
        vl._handle_utterance(loud_audio())
        assert len(results) == 1


# --------------------------------------------------------------------------
# configurability
# --------------------------------------------------------------------------

class TestConfigThresholds:
    def test_lang_threshold_is_configurable(self):
        """Lowering lang_prob_min lets a borderline line through."""
        cfg = {"voice": {"lang_prob_min": 0.2}}
        vl, results = make_listener(
            StubTranscriber("они идут", lang="ru", prob=0.3),
            StubTranslator("they are coming"), cfg=cfg)
        vl._handle_utterance(loud_audio())
        assert len(results) == 1

    def test_logprob_threshold_is_configurable(self):
        cfg = {"voice": {"min_avg_logprob": -3.0}}
        vl, results = make_listener(
            StubTranscriber("они идут", logprob=-2.5),
            StubTranslator("they are coming"), cfg=cfg)
        vl._handle_utterance(loud_audio())
        assert len(results) == 1


# --------------------------------------------------------------------------
# real translation (network)
# --------------------------------------------------------------------------

class TestRealTranslation:
    def test_russian_actually_translates_to_english(self):
        """Proves the real Translator handles the RU->EN voice path.

        Skipped rather than failed when offline — the network is not what
        this suite is testing.
        """
        from dota_ocr.translator import Translator

        translator = Translator(target="en")
        probe = translator.translate("они идут на рошана", src="ru",
                                     target_language="en")
        if probe == "они идут на рошана":
            pytest.skip("translation API unreachable")

        vl, results = make_listener(
            StubTranscriber("они идут на рошана"), translator)
        vl._handle_utterance(loud_audio())

        assert len(results) == 1
        russian, english = results[0]
        assert russian == "они идут на рошана"
        assert "roshan" in english.lower()
