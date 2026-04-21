"""Post-OCR text cleanup.

PaddleOCR's Russian recognizer occasionally emits Latin letters that
are visually identical to Cyrillic (A-А, B-В, H-Н, M-М, p-р, ...).
The garbled string looks like gibberish to Google Translate, which
returns it unchanged — and our overlay then drops it.

Strategy: Dota chat lines have the format
    "[TAG] Username [SubTag]: the actual message"
Everything up to the first `:` is Latin metadata we want to preserve
(player names, clan tags). Everything after `:` is user-typed content
in the OCR language; for Russian we coerce its Latin homoglyphs to
Cyrillic aggressively.

If no `:` is found we assume the whole line is body (e.g. a
game-system message) and still normalize.
"""

from __future__ import annotations

# Latin letters that double as Cyrillic lookalikes in common fonts.
_HOMOGLYPH_MAP = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
    "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
    "x": "х", "y": "у",
    # These are "best-guess" mappings for OCR confusions we see in
    # Dota's chat font, not strict homoglyphs. They're safe to apply
    # *inside* the message body because that body is expected to be
    # Russian. They would corrupt Latin tokens, so only call the
    # aggressive path on the body, never on usernames.
    "n": "п",   # lowercase п often recognized as n
    "N": "И",   # uppercase И sometimes recognized as N
    "g": "д",   # lowercase д sometimes recognized as g
    "u": "и",   # lowercase и sometimes recognized as u
    "i": "и",
    "b": "ь",   # soft sign sometimes recognized as b
    "r": "г",   # lowercase г often recognized as r
    "t": "т",   # lowercase т often recognized as t  (matches uppercase T->Т)
    "m": "м",   # lowercase м often recognized as m
    # NOTE: we intentionally *don't* map digits (6→б, 3→з) even though
    # OCR sometimes confuses them, because real numbers ("5 minutes",
    # "killed 6") show up in chat and we'd corrupt them.
}


# Token-level OCR mistranscriptions that single-char homoglyph mapping
# can't fix — e.g. "из" (2 narrow Cyrillic chars) often gets merged and
# re-rendered as "m3". Applied before char-level mapping, word-boundary
# aware so we don't corrupt inside bigger tokens.
_TOKEN_FIXES = {
    "m3": "из",
    "M3": "Из",
    "u3": "из",
    "U3": "Из",
}

def _convert_body(text: str) -> str:
    # Token fixes first (word-boundary).
    for bad, good in _TOKEN_FIXES.items():
        text = re.sub(rf"(?<![A-Za-zА-Яа-я0-9]){re.escape(bad)}(?![A-Za-zА-Яа-я0-9])",
                      good, text)
    return "".join(_HOMOGLYPH_MAP.get(c, c) for c in text)


# OCR often misreads the ':' separator as one of these lookalikes.
_COLON_LOOKALIKES = "{};&"

# Single-char OCR misreads that appear at the START of a line in place
# of a ':' when the player-name portion was dropped by the brightness
# threshold. We only treat these as colon-equivalents when they're the
# FIRST non-space character AND followed by a space + body — never
# mid-line, to avoid corrupting real punctuation like "hi!".
_LEADING_COLON_MISREADS = "!‹<|>^*•·"


def normalize_colons(text: str) -> str:
    return _normalize_colons(text)


def _normalize_colons(text: str) -> str:
    """Replace the FIRST colon-lookalike with ':' so downstream
    parsing always sees a real colon. Only operates if no ':' is
    present, to avoid corrupting braces in real chat content."""
    if ':' in text:
        return text
    for ch in _COLON_LOOKALIKES:
        idx = text.find(ch)
        if idx >= 0:
            return text[:idx] + ':' + text[idx + 1:]
    # Fallback: a leading single-char junk prefix ('! body', '‹ body')
    # that OCR emitted in place of ': body' when the player name was
    # dropped. Require a following space so real punctuation isn't
    # affected.
    stripped = text.lstrip()
    if len(stripped) >= 2 and stripped[0] in _LEADING_COLON_MISREADS and stripped[1] == ' ':
        pad = len(text) - len(stripped)
        return text[:pad] + ':' + stripped[1:]
    return text


def _get_body(text: str) -> str:
    """Return the message body (after first ':'), or the full text."""
    text = _normalize_colons(text)
    idx = text.find(":")
    return text[idx + 1:] if idx >= 0 else text


import re

# Real chat lines contain a bracketed channel tag followed by ':', e.g.
#   [Allies] Name [Clan] : msg
#   [All]    Name        : msg
# OCR can prepend icon misreads like ">" or "v" before the tag, so we
# locate the tag *anywhere* in the line rather than anchoring to start.
_CHAT_TAG_RE = re.compile(
    r"\[\s*(Allies|All|Spectator|Observer|Party|Союзники|Все)\s*\]",
    re.IGNORECASE,
)
# Fallback: any [ABC] tag that precedes a ':'.
_ANY_TAG_RE = re.compile(r"\[[A-Za-zА-Яа-я][^\]]{0,20}\].*:")


# Require a player-name token between the channel tag and the ':'.
# Rejects OCR garbage like "[Allies]  : test" (empty name).
_EMPTY_NAME_RE = re.compile(
    r"\[\s*(?:Allies|All|Spectator|Observer|Party|Союзники|Все)\s*\]"
    r"\s*[^\s:\[\]]",   # at least ONE non-space non-colon non-bracket char
    re.IGNORECASE,
)
# Prefix before the channel tag is supposed to be nothing, or at most
# one or two icon misreads. If the prefix contains digits or sentence-
# like tokens, it's OCR noise ("22 55 [Allies] : test").
_GARBAGE_PREFIX_RE = re.compile(r"^[^\[]*[0-9]")


def is_chat_line(text: str) -> bool:
    """Return True if the line is a player-typed Dota chat message.

    Dota chat has a VERY specific layout:
        [ChannelTag] PlayerName [OptionalClan] : typed message

    Must:
      * Start with a bracketed channel tag (`[Allies]`, `[All]`, ...)
      * Contain a `:` separator
      * Have non-empty content after the `:`

    This cleanly rejects system messages, which look like:
      * `Unpausing in 3...`             (no brackets, no colon)
      * `... has reconnected to the game.`
      * `Rainbow51 [PLEYA] (Invoker) resumed the game.`  (no `:` body)
      * `(Earth Spirit) has reconnected`  (parenthesis, not bracket)
      * `among 5 heroes`
    """
    t = _normalize_colons(text.strip())
    if not t:
        return False

    # Must have a ':' separator — every chat line is "Name : message".
    if ':' not in t:
        return False

    # Body-only continuation: OCR dropped the blue player-name half of
    # the line and left us with just ": message" (or ":message"). The
    # body after the ':' still needs to be translated, so accept these
    # as chat lines. We gate on "body contains real letters" to reject
    # stray ':' from UI chrome.
    if t.startswith(':'):
        body = t[1:].strip()
        if len(body) >= 2 and any(c.isalpha() for c in body):
            return True
        return False

    prefix_to_colon = t[: t.index(':')]
    tag_match = _CHAT_TAG_RE.search(t)
    if tag_match:
        # Real channel tag detected. The ONLY hard-reject rule is
        # "digits before the channel tag" — that reliably identifies
        # OCR garbage like "22 55 [Allies] : test".
        #
        # We deliberately do NOT require a player name between the tag
        # and ':' — Dota renders player names in team blue, which the
        # OCR brightness threshold often drops, producing legitimate
        # chat lines like "[Allies]          :  тест".
        tag_start = tag_match.start()
        pre_tag = prefix_to_colon[:tag_start]
        if _GARBAGE_PREFIX_RE.match(pre_tag):
            return False
    elif '[' in prefix_to_colon:
        # Bracketed token that isn't a known channel keyword. Could be
        # a garbled channel tag. Reject only when the bracket content
        # shows *strong* corruption signals: pipes, slashes, digits,
        # semicolons — real clan/channel tags are letters only.
        first = re.search(r"\[([^\]]{1,12})\]", prefix_to_colon)
        if first is not None:
            inner = first.group(1)
            if re.search(r"[|/\\;0-9]", inner):
                return False

    # Accept any of these formats:
    #   [Allies] Ninjito [ISSUE] : msg   (channel tag + clan)
    #   [All] Ninjito : msg              (channel tag only)
    #   Ninjito [ISSUE] : msg            (clan tag only, no channel)
    #   Ninjito : msg                    (plain name)
    # Reject lines with no `[...]` AND no recognizable name structure,
    # which is what system messages usually look like.
    has_bracket_before_colon = '[' in t[: t.index(':')]
    if not (has_bracket_before_colon or _ANY_TAG_RE.search(t)):
        # No bracket anywhere before ':'. Still OK if the prefix looks
        # like a plain player name (word(s), not a whole sentence).
        prefix = t[: t.index(':')].strip()
        # Plain-name prefix: short, no sentence-ending punctuation,
        # doesn't contain common system-message verbs.
        if len(prefix) > 30 or len(prefix) < 2:
            return False
        if any(p in prefix for p in ('.', '!', '?')):
            return False

    prefix = t[: t.index(':')].strip()
    body = t[t.index(':') + 1:].strip()

    # Body must have real content.
    if len(body) < 2:
        return False

    # Reject lines that are clearly system events even if they fit the
    # regex — they mention actions Dota puts in *parens*, not chat text.
    lower_body = body.lower()
    system_keywords = (
        "has reconnected",
        "resumed the game",
        "paused the game",
        "unpausing in",
        "has abandoned",
        "has left",
        "glyph of fortification",
    )
    if any(k in lower_body for k in system_keywords):
        return False

    return True


# Characters that are DEFINITELY Cyrillic — they have no Latin lookalike.
# (А/A, В/B, С/C, Е/E, К/K, М/M, Н/H, О/O, Р/P, Т/T, Х/X, У/Y all
# look identical to Latin letters, so they don't count.)
_DEFINITE_CYRILLIC = set(
    "бвгджзийклмнптфцчшщъыьэюя"
    "БВГДЖЗИЙКЛМНПТФЦЧШЩЪЫЬЭЮЯ"
)


def has_cyrillic(text: str) -> bool:
    """Return True if the text is actually Russian (not English with
    a stray Cyrillic OCR homoglyph).

    We look ONLY at the message body (after the first ':') when one is
    present, because the prefix `[Allies] Nickname` is almost always
    Latin and would dilute the ratio.

    Rules (all must hold):
      * at least 3 Cyrillic chars in the body
      * Cyrillic letters >= 40% of all letters in the body

    This rejects lines like:
        "Name : i love you okay"              (0 cyr -> False)
        "Name : hello Вasd"                   (1 cyr, 95% latin -> False)
    but accepts:
        "Name : спасибо за помощь"            (100% cyr -> True)
        "Name : slаt ебаный"                  (5 cyr, 50% cyr -> True)
    """
    body = _get_body(text)
    cyr = 0
    lat = 0
    for c in body:
        if "\u0400" <= c <= "\u04FF":
            cyr += 1
        elif c.isalpha():
            lat += 1
    if cyr < 3:
        return False
    total = cyr + lat
    if total == 0:
        return False
    return (cyr / total) >= 0.4


def normalize_cyrillic(text: str) -> str:
    """Coerce Dota chat OCR output into valid Cyrillic.

    Only applied to lines that already contain Cyrillic characters in
    the message body.  Preserves the `[Allies] Nickname [Tag]:` prefix
    and rewrites only the body after the first colon.
    """
    if not text:
        return text
    # Don't touch lines that are already fully Latin/English.
    if not has_cyrillic(text):
        return text
    idx = text.find(":")
    if idx < 0:
        return _convert_body(text)
    prefix = text[: idx + 1]
    body = text[idx + 1 :]
    return prefix + _convert_body(body)
