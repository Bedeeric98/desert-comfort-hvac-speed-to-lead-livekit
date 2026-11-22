"""address_digits.py: turns a spoken-number address into digits (pure Python, no network, no secrets).

Why it exists (real call, 2026-10-05): the caller said "two hundred West Washington Street" and the agent passed exactly those words on, so
the GHL contact, the appointment, the Slack/email text and the call summary all carried "two hundred West Washington Street" instead of
"200 West Washington Street". A technician needs the digits.

Only three places in an address are converted, on purpose, so street names are never touched:
  1. the house number at the very start    "two hundred West Washington Street"  ->  "200 West Washington Street"
  2. a unit number after Apartment / Apt / Unit / Suite ...   "apartment four b"  ->  "apartment 4B"
  3. a 5-digit zip spoken digit by digit at the very end       "... eight five zero zero one"  ->  "... 85001"
Everything else ("Fifth Avenue", "Washington", "Phoenix, Arizona") stays exactly as it was. Text that already has digits is left alone.

Reading a number: with "hundred" or "thousand" in it, it is read as a normal cardinal ("fifteen hundred" = 1500, "two hundred and
twelve" = 212, "one thousand two hundred fifty" = 1250). Without them it is read the way people say house numbers, in groups of one or two
digits ("twenty one fifteen" = 2115, "twelve twenty three" = 1223, "one two three four" = 1234, "four oh one" = 401).

Used by desert_comfort_agent.py and call_messages.py through guarded imports; if this file is missing the address passes through unchanged.
Never raises.
"""
from __future__ import annotations

import re

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_BIG = {"hundred", "thousand"}
_ORDINAL_UNITS = {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"}
_OH = {"oh", "o"}  # "four oh one": only counts as a zero when it is inside a run, never at the start of one
_UNIT_MARKERS = {"apartment", "apt", "unit", "suite", "ste", "room", "rm", "lot", "building", "bldg", "space", "spc"}
_TOKEN_RE = re.compile(r"([A-Za-z']+|\d+)([^A-Za-z\d']*)")
_JOINER_RE = re.compile(r"[\s-]*")  # between two words of ONE number: spaces or a hyphen ("twenty-one"); a comma or period ends it


def _kind(word: str, first: bool) -> str | None:
    w = word.lower()
    if w in _UNITS:
        return "unit"
    if w in _TEENS:
        return "teen"
    if w in _TENS:
        return "tens"
    if w in _BIG:
        return "big"
    if w == "and":
        return "and"
    if w in _OH and not first:
        return "unit"
    return None


def _value(w: str) -> int:
    return _UNITS.get(w, _TEENS.get(w, _TENS.get(w, 0)))


def _take_run(tokens: list[tuple[str, str]], start: int) -> tuple[int, list[str]]:
    """The longest run of number words beginning at `start` -> (index just after it, the words). Trailing 'and' is given back."""
    i, words = start, []
    while i < len(tokens):
        word, sep = tokens[i]
        if _kind(word, first=(i == start)) is None:
            break
        words.append(word.lower())
        i += 1
        if not _JOINER_RE.fullmatch(sep):
            break
    while words and words[-1] == "and":
        words.pop()
        i -= 1
    return i, words


def _run_to_digits(words: list[str]) -> str | None:
    """'two hundred' -> '200'. None when the words do not make a number a house or unit number can be."""
    if not words or words[0] == "and":
        return None
    if any(w in _BIG for w in words):
        cur = total = 0
        for w in words:
            if w == "and":
                continue
            if w in _BIG:
                if cur == 0:
                    return None
                if w == "hundred":
                    cur *= 100
                else:
                    total += cur * 1000
                    cur = 0
            else:
                cur += 0 if w in _OH else _value(w)
        out = str(total + cur)
    else:
        if "and" in words:
            return None
        parts, i = [], 0
        while i < len(words):
            w = words[i]
            if w in _OH or w in _UNITS or w in _TEENS:
                parts.append("0" if w in _OH else str(_value(w)))
                i += 1
            else:  # tens: "twenty one" is one group, "twenty" alone is 20
                nxt = words[i + 1] if i + 1 < len(words) else ""
                if nxt in _UNITS and nxt != "zero":
                    parts.append(str(_value(w) + _value(nxt)))
                    i += 2
                else:
                    parts.append(str(_value(w)))
                    i += 1
        out = "".join(parts)
    if not out or (len(out) > 1 and out.startswith("0")) or len(out) > 6:
        return None
    return out


def _join(prefix: str, tokens: list[tuple[str, str]]) -> str:
    return prefix + "".join(w + s for w, s in tokens)


def _convert_zip(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(tokens) < 6:
        return tokens
    tail = tokens[-5:]
    if not all(w.lower() in _UNITS or w.lower() in _OH for w, _ in tail):
        return tokens
    before = tokens[-6][0].lower()
    if _kind(before, first=False) is not None:  # a longer run of number words: not a zip
        return tokens
    digits = "".join("0" if w.lower() in _OH else str(_UNITS[w.lower()]) for w, _ in tail)
    return tokens[:-5] + [(digits, tail[-1][1])]


def _convert_units(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(tokens):
        word, sep = tokens[i]
        out.append((word, sep))
        i += 1
        if word.lower() in _UNIT_MARKERS and i < len(tokens):
            end, words = _take_run(tokens, i)
            digits = _run_to_digits(words)
            if digits is not None:
                last_sep = tokens[end - 1][1]
                letter = ""
                if end < len(tokens) and len(tokens[end][0]) == 1 and tokens[end][0].isalpha() and _JOINER_RE.fullmatch(last_sep):
                    letter = tokens[end][0].upper()
                    last_sep = tokens[end][1]
                    end += 1
                out[-1] = (word, sep)
                out.append((digits + letter, last_sep))
                i = end
    return out


def _convert_house_number(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not tokens:
        return tokens
    end, words = _take_run(tokens, 0)
    digits = _run_to_digits(words)
    # a real house number is followed by a street word, never by nothing and never by more digits
    if digits is None or end >= len(tokens) or tokens[end][0].isdigit():
        return tokens
    # "Forty Second Street" is a street NAME ("Forty" + "Second"), not the house number 40
    if len(words) == 1 and words[0] in _TENS and tokens[end][0].lower() in _ORDINAL_UNITS:
        return tokens
    return [(digits, tokens[end - 1][1])] + tokens[end:]


def normalize_spoken_address(text):
    """The address with its house number, unit number and zip as digits. Anything else, and anything that is not a string, comes back as it was."""
    try:
        if not isinstance(text, str) or not text.strip() or len(text) > 600:
            return text
        m = _TOKEN_RE.search(text)
        if not m:
            return text
        prefix = text[: m.start()]
        tokens = [(t.group(1), t.group(2)) for t in _TOKEN_RE.finditer(text, m.start())]
        new = _convert_house_number(_convert_units(_convert_zip(tokens)))
        result = _join(prefix, new)
        return result if result != _join(prefix, tokens) else text
    except Exception:
        return text
