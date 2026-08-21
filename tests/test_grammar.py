"""Tests for whole-sentence grammar correction.

The network is mocked throughout — these tests pin down offset
arithmetic, caching, and the fallback path, none of which need a live
LanguageTool server. The rate-limit case matters most: the free tier
cuts us off at roughly 20 requests a minute, and a 429 must degrade to
the round-trip translator instead of surfacing as a broken feature.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dota_ocr.grammar import GrammarFixer


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _Session:
    """Stands in for requests.Session, recording every call."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, data=None, timeout=None):
        self.calls += 1
        r = self._responses.pop(0) if self._responses else _Resp({"matches": []})
        if isinstance(r, Exception):
            raise r
        return r


def _match(offset, length, value):
    return {"offset": offset, "length": length,
            "replacements": [{"value": value}]}


def _fixer(session, **kw):
    """A fixer whose fallback never touches the network by accident."""
    kw.setdefault("fallback", lambda _t: None)
    return GrammarFixer(session=session, **kw)


class TestCorrection:
    def test_single_replacement_applied(self):
        s = _Session(_Resp({"matches": [_match(0, 1, "I")]}))
        assert _fixer(s).fix("i need help") == "I need help"

    def test_multiple_replacements_applied_right_to_left(self):
        """Applying left-to-right would shift every later offset."""
        s = _Session(_Resp({"matches": [
            _match(0, 1, "I"),
            _match(7, 4, "help"),
        ]}))
        assert _fixer(s).fix("i need halp") == "I need help"

    def test_replacement_of_different_length_keeps_the_tail(self):
        s = _Session(_Resp({"matches": [_match(0, 2, "We")]}))
        assert _fixer(s).fix("us need help") == "We need help"

    def test_no_matches_returns_none(self):
        s = _Session(_Resp({"matches": []}))
        assert _fixer(s).fix("i am fine now") is None

    def test_result_identical_to_input_returns_none(self):
        s = _Session(_Resp({"matches": [_match(0, 1, "i")]}))
        assert _fixer(s).fix("i need help") is None

    def test_match_without_replacement_is_skipped(self):
        s = _Session(_Resp({"matches": [{"offset": 0, "length": 1,
                                         "replacements": []}]}))
        assert _fixer(s).fix("i need help") is None

    def test_out_of_range_offset_is_ignored(self):
        s = _Session(_Resp({"matches": [_match(50, 5, "nope")]}))
        assert _fixer(s).fix("i need help") is None

    def test_malformed_payload_is_survivable(self):
        s = _Session(_Resp(None))
        assert _fixer(s).fix("i need help") is None


class TestCaching:
    def test_repeat_text_does_not_hit_the_network_twice(self):
        s = _Session(_Resp({"matches": [_match(0, 1, "I")]}),
                     _Resp({"matches": []}))
        g = _fixer(s)
        assert g.fix("i need help") == "I need help"
        assert g.fix("i need help") == "I need help"
        assert s.calls == 1

    def test_none_results_are_cached_too(self):
        s = _Session(_Resp({"matches": []}), _Resp({"matches": []}))
        g = _fixer(s)
        g.fix("all good here")
        g.fix("all good here")
        assert s.calls == 1

    def test_cache_evicts_oldest_first(self):
        s = _Session(*[_Resp({"matches": []}) for _ in range(5)])
        g = _fixer(s, cache_size=2)
        g.fix("first line here")
        g.fix("second line here")
        g.fix("third line here")
        assert len(g._cache) == 2
        assert "first line here" not in g._cache


class TestFallback:
    def test_network_error_uses_fallback(self):
        s = _Session(RuntimeError("no network"))
        g = GrammarFixer(session=s, fallback=lambda t: t.upper())
        assert g.fix("i need help") == "I NEED HELP"

    def test_rate_limit_uses_fallback(self):
        s = _Session(_Resp({}, status=429))
        g = GrammarFixer(session=s, fallback=lambda t: t.upper())
        assert g.fix("i need help") == "I NEED HELP"

    def test_failing_fallback_returns_none(self):
        def _boom(_t):
            raise RuntimeError("translator down")

        s = _Session(RuntimeError("no network"))
        assert GrammarFixer(session=s, fallback=_boom).fix("i need help") is None

    def test_fallback_returning_the_input_is_treated_as_no_change(self):
        s = _Session(RuntimeError("no network"))
        g = GrammarFixer(session=s, fallback=lambda t: t)
        assert g.fix("i need help") is None


class TestGuards:
    def test_short_text_is_not_checked(self):
        s = _Session()
        assert _fixer(s).fix("gg") is None
        assert s.calls == 0

    def test_empty_text_is_not_checked(self):
        s = _Session()
        assert _fixer(s).fix("   ") is None
        assert s.calls == 0

    def test_missing_session_falls_back(self):
        g = GrammarFixer(session=None, fallback=lambda t: t.upper())
        g._session = None
        assert g.fix("i need help") == "I NEED HELP"


class TestSuggestLine:
    def test_produces_one_line_scoped_suggestion(self):
        s = _Session(_Resp({"matches": [_match(0, 1, "I")]}))
        out = _fixer(s).suggest_line("i need help")
        assert len(out) == 1
        assert out[0].text == "I need help"
        assert out[0].kind == "sentence"
        assert out[0].scope == "line"

    def test_no_correction_produces_nothing(self):
        s = _Session(_Resp({"matches": []}))
        assert _fixer(s).suggest_line("i am fine now") == []
