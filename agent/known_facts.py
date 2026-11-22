"""Known-facts tracker for the Desert Comfort voice agents (both caller variants).

Why it exists: on real calls (2026-09-29 and twice on 2026-10-02) a caller said "about eighty nine degrees in here" in their
opening sentence and the agent, several turns later, still asked "what is the thermostat showing?" The caller had to say "I just
said eighty nine". The prompt already tells the model not to do that; GPT-Live-1 followed it only about half the time, and OpenAI's
own docs say prompt instructions "guide the models; they do not enforce checks". So this module does the remembering in plain
Python: it reads each caller turn, spots the five qualifying facts, and the agent hands the model a short `KNOWN FACT:` note through
`append_thinking` (OpenAI's own "something to know without saying it" primitive) at the moment the caller says it.

Pure Python, no network, no dependencies, well under a millisecond per turn. It never raises: a failure here must never touch a call.
It only ever ADDS a note; it never blocks or rewrites anything the model would have done.

Honest limit: a note is a strong nudge placed right before the decision, not a mechanical lock on the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Spoken numbers ("eighty nine", "eighty, nine", "eighty-nine", "89", "a hundred and two")
# ---------------------------------------------------------------------------

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_TOKEN_RE = re.compile(r"[a-z]+|\d+|°")

# Plausible indoor temperatures for a failed AC in Phoenix. Anything outside this is not treated as an indoor reading.
MIN_INSIDE_F, MAX_INSIDE_F = 60, 125

_DEGREE_WORDS = {"degree", "degrees", "°", "fahrenheit"}
_READING_WORDS = {"thermostat", "showing", "shows", "reads", "reading", "says", "saying", "temperature", "temp", "inside"}
_INSIDE_WORDS = {"inside", "indoors", "indoor", "here", "thermostat", "house", "home", "apartment", "room", "bedroom",
                 "kitchen", "upstairs", "downstairs", "condo"}
_OUTSIDE_WORDS = {"outside", "outdoors", "outdoor", "forecast", "weather", "predicted", "supposed"}


@dataclass
class _Num:
    value: int
    start: int  # token index of the first token
    end: int  # token index AFTER the last token


def _tokens(text: str) -> list[tuple[str, int, int]]:
    low = text.lower()
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(low)]


def _parse_numbers(tokens: list[tuple[str, int, int]]) -> list[_Num]:
    """Every number in the token stream, spoken or digits. Gaps between tokens may be only punctuation/space ("eighty, nine")."""
    out: list[_Num] = []
    i, n = 0, len(tokens)
    while i < n:
        w = tokens[i][0]
        if w.isdigit():
            out.append(_Num(int(w), i, i + 1))
            i += 1
            continue
        if w in ("a", "one") and i + 1 < n and tokens[i + 1][0] == "hundred":
            j = i + 2
            if j < n and tokens[j][0] == "and":
                j += 1
            extra = 0
            if j < n and tokens[j][0] in _UNITS and _UNITS[tokens[j][0]] <= 9:
                extra = _UNITS[tokens[j][0]]
                j += 1
            elif j < n and tokens[j][0] in _TEENS:
                extra = _TEENS[tokens[j][0]]
                j += 1
            out.append(_Num(100 + extra, i, j))
            i = j
            continue
        if w in _TENS:
            if i + 1 < n and tokens[i + 1][0] in _UNITS and _UNITS[tokens[i + 1][0]] != 0:
                out.append(_Num(_TENS[w] + _UNITS[tokens[i + 1][0]], i, i + 2))
                i += 2
            else:
                out.append(_Num(_TENS[w], i, i + 1))
                i += 1
            continue
        if w in _TEENS:
            out.append(_Num(_TEENS[w], i, i + 1))
            i += 1
            continue
        if w in _UNITS:
            out.append(_Num(_UNITS[w], i, i + 1))
        i += 1
    return out


@dataclass
class TempReading:
    value: int
    inside_confirmed: bool  # the caller said "in here / inside / thermostat" near it (otherwise assumed indoor, unconfirmed)
    snippet: str


def _binding(toks: list[tuple[str, int, int]], num: _Num) -> str:
    """Which place the number belongs to: "inside", "outside" or "" (not said). The word right AFTER the number binds first
    ("91 degrees in here", "112 degrees outside"), otherwise the nearest one just BEFORE it ("the thermostat says eighty nine")."""
    for t in toks[num.end:num.end + 5]:
        if t[0] in _OUTSIDE_WORDS:
            return "outside"
        if t[0] in _INSIDE_WORDS:
            return "inside"
        if t[0] in ("and", "but", "while", "then"):
            break  # a new clause starts: the next place word belongs to the next number
    for t in reversed(toks[max(0, num.start - 4):num.start]):
        if t[0] in _OUTSIDE_WORDS:
            return "outside"
        if t[0] in _INSIDE_WORDS:
            return "inside"
    return ""


def find_inside_temperature(text: str) -> TempReading | None:
    """The caller's inside temperature if the text states one; None for anything that is not clearly a temperature reading
    (an address number, a time, an outdoor/forecast temperature)."""
    toks = _tokens(text)
    best: TempReading | None = None
    for num in _parse_numbers(toks):
        if not MIN_INSIDE_F <= num.value <= MAX_INSIDE_F:
            continue
        place = _binding(toks, num)
        if place == "outside":
            continue  # "it's 112 outside" is not the indoor temperature
        has_degree = bool({t[0] for t in toks[num.end:num.end + 2]} & _DEGREE_WORDS)
        has_reading = bool({t[0] for t in toks[max(0, num.start - 5):num.start]} & _READING_WORDS)
        # "it's hot in here, like eighty-nine": no "degrees", but a place word right before a plausible indoor number
        place_before = place == "inside" and num.value >= 70 and any(t[0] in _INSIDE_WORDS for t in toks[max(0, num.start - 4):num.start])
        if not (has_degree or has_reading or place_before):
            continue
        lo = toks[max(0, num.start - 6)][1]
        hi = toks[min(len(toks) - 1, num.end + 5)][2]
        said_thermostat = any(t[0] == "thermostat" for t in toks)
        reading = TempReading(num.value, place == "inside" or has_reading or said_thermostat, text[lo:hi].strip())
        # the LATEST reading wins (a caller who corrects themselves is right the second time), but a reading clearly tied to the
        # inside never loses to one that is not
        if best is None or reading.inside_confirmed or not best.inside_confirmed:
            best = reading
    return best


# ---------------------------------------------------------------------------
# The other four qualifying facts. These only decide "did the caller already say something about X?"; whether it is a real
# emergency is left to the model (it reads the caller's own words in the note), so a negation like "no burning smell" can
# never be misread by a regex as a gas leak.
# ---------------------------------------------------------------------------

_UNIT_RE = re.compile(r"\b(outdoor|outside|exterior|condenser|compressor|(?:ac|a\.c\.|air conditioner)\s+unit)\b.{0,60}|\bunit outside\b.{0,60}", re.I)
_UNIT_NOT_RUNNING_RE = re.compile(
    r"\b(not|isn't|isnt|won't|wont|doesn't|doesnt|never|no longer|stopped|stopping|died|dead|silent|quiet|off)\b", re.I)
_UNIT_RUNNING_RE = re.compile(r"\b(is|it's|its|still|keeps?)\s+(running|on|going|spinning|humming|working)\b|\brunning (fine|okay|ok|normally)\b", re.I)
_SMELL_RE = re.compile(r"\b(smell|smells|smelling|smelled|smoke|smoking|burning|burnt|gas|sparking|sparks?|electrical)\b", re.I)
_LEAK_RE = re.compile(r"\b(leak|leaks|leaking|leaked|dripping|drips|pooling|puddles?|flooding|flooded|water (?:on|all over|everywhere|coming|is))\b", re.I)
_PEOPLE_RE = re.compile(
    r"\b(elderly|grandma|grandmother|grandpa|grandfather|old (?:man|woman|lady|person|people)|my (?:mom|mother|dad|father|wife|husband|kids?|son|daughter)|"
    r"baby|babies|infant|newborn|toddler|kids|children|child|little ones?|medical|oxygen|asthma|disabled|pregnant|sick|dialysis|heart condition)\b", re.I)
_ALONE_RE = re.compile(r"\b(just me|only me|by myself|live alone|living alone|nobody else|no one else)\b", re.I)

# What the assistant's last turn was asking about. A caller's reply to a question just asked is an ANSWER, not a volunteered fact,
# so no note is needed for it (the model asked, so it is not about to ask again).
_ASKED = {
    "temp": re.compile(r"thermostat|what temperature|how hot|how warm|degrees", re.I),
    "unit": re.compile(r"outdoor unit|outside unit|condenser|compressor|unit running", re.I),
    "smell": re.compile(r"smell|burning or gas", re.I),
    "leak": re.compile(r"leak", re.I),
    "people": re.compile(r"elderly|young children|medical condition|anyone in the home|who'?s (?:home|there|in the house)|anyone at home", re.I),
}

# A SEPARATE dict from _ASKED on purpose: call_checkup.py's rule_c2_agent_reasked imports topics_asked()/_ASKED directly and
# re-runs them on its OWN KnownFacts() instance to catch the agent re-asking something - adding "name"/"address"/"spell" to
# _ASKED would have silently changed THAT rule too (confirmed: it did, and broke 2 of its real-call regression tests, because
# the booking confirmation step legitimately repeats the name/address once by design). Name/address/spell asked-tracking below
# is for this module's own capture logic only, never exported through topics_asked()'s default.
_CONTACT_ASKED = {
    "name": re.compile(r"\bname\b", re.I),
    "address": re.compile(r"service address|your address|the address", re.I),
    "spell": re.compile(r"\bspell\b", re.I),
}


_INTERROGATIVE_START = re.compile(r"^\W*(what|when|where|which|who|how|is|are|do|does|did|can|could|would|will|any|anyone|have|has)\b", re.I)


def question_sentences(text: str | None) -> list[str]:
    """The sentences of an assistant turn that actually ASK something (a "?" or an interrogative opening). A recap ("nothing strange on
    smells.") sitting in front of the next question must not count as asking about smells."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text or "") if "?" in s or _INTERROGATIVE_START.match(s)]


# A question that opens by REPEATING what the caller said ("You said about eighty nine degrees inside, so any unusual smells...?") is a recap
# plus a question about something else. The recap words must not make the question count as asking about the recapped topic (real call
# 2026-10-05: the recap of the temperature was flagged as "asked about temp again"). Only the recap clause is removed, up to its comma.
_RECAP_RE = re.compile(r"\byou (?:just )?(?:said|mentioned|told me|gave me)\b[^,;?!.]*[,;]?|\bwith (?:that|the|those)\b[^,;?!.]*,", re.I)


def topics_asked(text: str | None, asked: dict[str, re.Pattern] = _ASKED) -> set[str]:
    qs = [_RECAP_RE.sub(" ", q) for q in question_sentences(text)]
    return {topic for topic, rx in asked.items() if any(rx.search(q) for q in qs)}


def _excerpt(text: str, m: re.Match, pad: int = 50) -> str:
    return text[max(0, m.start() - pad):min(len(text), m.end() + pad)].strip()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace('"', "'").strip()


@dataclass
class Note:
    topic: str  # temp | unit | smell | leak | people
    key: str  # what was found, used to avoid repeating the same note
    text: str  # the KNOWN FACT note handed to the model


def _temp_note(r: TempReading) -> Note:
    head = (f"KNOWN FACT: the caller already told you the inside temperature is about {r.value} degrees "
            f"(their words: \"{_clean(r.snippet)}\"). Do not ask the thermostat or temperature question in any form - say it back instead. ")
    if r.value >= 90:
        tail = (f"{r.value} is 90 or above, which is an EMERGENCY under the Critical Emergency Rule: stop the remaining diagnostic "
                "questions and move to emergency pricing now.")
    else:
        tail = f"{r.value} is below 90, so the temperature alone is not an emergency: carry on with the facts still missing."
    if not r.inside_confirmed:
        tail += " (If it is unclear that this is the INSIDE temperature, confirm it once with 'is that inside the house?' - never ask for a thermostat reading.)"
    return Note("temp", f"temp:{r.value}", head + tail)


def _unit_note(not_running: bool, words: str) -> Note:
    state = "NOT running" if not_running else "running"
    tail = (" Use it: an at-risk person plus a unit that is not running at all is an emergency." if not_running else "")
    return Note("unit", f"unit:{state}", f"KNOWN FACT: the caller already said the outdoor unit is {state} (their words: \"{_clean(words)}\"). "
                f"Do not ask the outdoor-unit question - say it back instead.{tail}")


def _smell_note(words: str) -> Note:
    return Note("smell", "smell", f"KNOWN FACT: the caller already mentioned smells, gas or burning (their words: \"{_clean(words)}\"). "
                "The smells question is already addressed - do not ask it again. If what they said describes a real gas, burning or electrical "
                "smell, that is an EMERGENCY under the Critical Emergency Rule; if they said there is none, it is answered 'no'.")


def _leak_note(words: str) -> Note:
    return Note("leak", "leak", f"KNOWN FACT: the caller already mentioned water or a leak (their words: \"{_clean(words)}\"). "
                "The leak question is already addressed - do not ask it again. If water is actively leaking from the system, that is an "
                "EMERGENCY under the Critical Emergency Rule; if they said there is no leak, it is answered 'no'.")


def _people_note(words: str, alone: bool) -> Note:
    if alone:
        return Note("people", "people:alone", f"KNOWN FACT: the caller already said no one else is home (their words: \"{_clean(words)}\"). "
                    "The at-risk-people question is answered 'no' - do not ask it again.")
    return Note("people", "people:mentioned", f"KNOWN FACT: the caller already mentioned who is at home (their words: \"{_clean(words)}\"). "
                "The at-risk-people question is already addressed - do not ask it again unless it is truly unclear who is there. An at-risk "
                "person combined with an outdoor unit that is not running at all is an EMERGENCY under the Critical Emergency Rule; otherwise note it "
                "for the technician and continue.")


# ---------------------------------------------------------------------------
# Name and address. Different purpose from the five facts above: those only stop a re-ask; these are also captured onto
# `KnownFacts.captured` so a caller who gives their name/address and never completes a tool call still has it on file (the
# tool-call path - booking, radius_checker - still wins when it runs; this is the backstop for when it does not). Open text,
# so there is no bounded-value check like temperature's 60-125 range: the signature is an explicit introduction cue ("my
# name is...", "I stay at..."), or - since real callers often skip the cue entirely ("It's Tom Reyes, 12 Cholla Court") -
# whatever the caller says right after being asked for it.
#
# Honest limit: this is pattern-matching on sentence shape, not a name/address validator - it will occasionally mis-split an
# unusual sentence. It is a backstop for the hang-up-before-the-tool-call case, not a replacement for the tool-call path.
# ---------------------------------------------------------------------------

_NAME_CUE_RE = re.compile(r"\b(?:my|the)\s+name(?:'s|\s+is)\b\s*[:,]?\s*|\bname(?:'s|\s+is)\b\s*[:,]?\s*", re.I)
_ADDRESS_CUE_RE = re.compile(
    r"\bi\s+(?:stay|live)\s+at\b\s*[:,]?\s*|\bmy\s+address\s+is\b\s*[:,]?\s*|\bthe\s+address\s+is\b\s*[:,]?\s*|\blocated\s+at\b\s*[:,]?\s*",
    re.I)
_STREET_WORD_RE = re.compile(
    r"\b(street|st|avenue|ave|road|rd|drive|dr|lane|ln|court|ct|way|boulevard|blvd|circle|cir|place|pl)\b", re.I)
_FILLER_LEAD_RE = re.compile(
    r"^(?:(?:um+|uh+|so|well|okay|ok|it's|it is|this is|that's|that is|yeah|yes|same number)\b[,.\s]*)+", re.I)
_STOP_CLAUSE_RE = re.compile(
    r"\b(?:and\s+is\s+this|is\s+this\s+the\s+best|is\s+that\s+(?:the\s+)?best|and\s+(?:my|the)\s+address|and\s+i\s+stay)\b.*$", re.I)
_SPELLED_LETTERS_RE = re.compile(r"(?:\b[a-z]\b[\s,.-]+){2,}\b[a-z]\b", re.I)
_NAME_SHAPE_RE = re.compile(r"^[a-zA-Z][a-zA-Z.'-]*(?:\s+[a-zA-Z][a-zA-Z.'-]*){0,3}$")
_NAME_STOPWORDS = {"it", "is", "this", "that", "so", "um", "uh", "the", "a", "an", "and", "i", "my", "me", "yes", "no",
                   "okay", "ok", "well", "same", "number", "here", "there"}


def _looks_like_junk_name(candidate: str) -> bool:
    """A second check beyond _FILLER_LEAD_RE/_NAME_SHAPE_RE for a filler word that slipped through un-stripped - a real name
    is never just "it is" or "well that" even though those pass the pure-alphabetic shape check."""
    return any(w.lower() in _NAME_STOPWORDS for w in candidate.split())


def _strip_trailing_clause(text: str) -> str:
    return _STOP_CLAUSE_RE.sub("", text).strip(" ,.")


def _join_spelled(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha())


def _split_name_words(text: str) -> tuple[str, str]:
    words = [w.strip(".,") for w in re.split(r"\s+", text.strip()) if w.strip(".,")]
    if not words:
        return "", ""
    return words[0], " ".join(words[1:])


def _split_name_and_address(text: str) -> tuple[str, str]:
    """"Tom Reyes, 12 Cholla Court" -> ("Tom Reyes", "12 Cholla Court") - splits at the first clause with a digit or a
    street-type word; everything before is the name, everything from there on is the address."""
    parts = re.split(r",\s*", text)
    for i, p in enumerate(parts):
        if any(ch.isdigit() for ch in p) or _STREET_WORD_RE.search(p):
            return ", ".join(parts[:i]).strip(" ,."), ", ".join(parts[i:]).strip(" ,.")
    return text.strip(" ,."), ""


def _name_target(text: str | None) -> str | None:
    """Which field a 'what's your name' / 'could you spell that' question named specifically, from the question's own
    wording - "last name" -> last_name, "first name" -> first_name, "first and last" or a generic "your name" -> None
    (expect a full name, or use the recency fallback for a spelling correction)."""
    if not text:
        return None
    has_first, has_last = bool(re.search(r"\bfirst\b", text, re.I)), bool(re.search(r"\blast\b", text, re.I))
    if has_first and not has_last:
        return "first_name"
    if has_last and not has_first:
        return "last_name"
    return None


def _detect_name(window: str, just_asked: bool, target: str | None = None) -> tuple[str, str, str] | None:
    m = _NAME_CUE_RE.search(window)
    if m:
        tail = _FILLER_LEAD_RE.sub("", _strip_trailing_clause(window[m.end():]))
        am = _ADDRESS_CUE_RE.search(tail)  # "my name is Michael Robinson. I stay at..." - stop before the address cue
        if am:
            tail = tail[:am.start()]
        tail = tail.strip(" ,.")
        first, last = _split_name_words(tail)
        if first and _NAME_SHAPE_RE.match(f"{first} {last}".strip()):
            return first, last, window.strip()
        return None
    if just_asked:
        candidate = _FILLER_LEAD_RE.sub("", _strip_trailing_clause(window)).strip(" ,.")
        if candidate and not any(ch.isdigit() for ch in candidate) and not _STREET_WORD_RE.search(candidate) \
                and _NAME_SHAPE_RE.match(candidate) and not _looks_like_junk_name(candidate) and 2 <= len(candidate) <= 60:
            if target == "first_name":
                return candidate, "", window.strip()
            if target == "last_name":
                return "", candidate, window.strip()
            first, last = _split_name_words(candidate)
            return first, last, window.strip()
    return None


def _detect_address(window: str, just_asked: bool) -> tuple[str, str] | None:
    m = _ADDRESS_CUE_RE.search(window)
    if m:
        tail = _FILLER_LEAD_RE.sub("", _strip_trailing_clause(window[m.end():])).strip(" ,.")
        return (tail, window.strip()) if tail else None
    if just_asked:
        candidate = _FILLER_LEAD_RE.sub("", _strip_trailing_clause(window)).strip(" ,.")
        if candidate and (any(ch.isdigit() for ch in candidate) or _STREET_WORD_RE.search(candidate)):
            return candidate, window.strip()
    return None


def _name_note(first: str, last: str, snippet: str) -> Note:
    full = f"{first} {last}".strip()
    return Note("name", f"name:{full.lower()}", f"KNOWN FACT: the caller already gave their name (their words: \"{_clean(snippet)}\"). "
                f"Do not ask for the name again - use \"{full}\".")


def _address_note(address: str, snippet: str) -> Note:
    return Note("address", f"address:{address.lower()}", f"KNOWN FACT: the caller already gave their service address (their words: "
                f"\"{_clean(snippet)}\"). Do not ask for the address again - use \"{address}\".")


def _spelled_note(field: str, spelled: str) -> Note:
    which = "first" if field == "first_name" else "last"
    return Note("name_spelled", f"spelled:{field}:{spelled.lower()}",
                f"KNOWN FACT: the caller just spelled their {which} name letter by letter: \"{spelled}\". Read it back once to "
                f"confirm you have it right, then use that exact spelling for the rest of the call - never the earlier guess.")


class KnownFacts:
    """One instance per call. Feed it every assistant turn and every caller turn, in order; `on_user_turn` returns the notes (possibly
    none) that should be handed to the model right now."""

    def __init__(self) -> None:
        self._fragments: list[str] = []  # consecutive caller turns since the assistant last spoke: one sentence can arrive in pieces
        self._asked: set[str] = set()  # topics the assistant's most recent turn asked about
        self._contact_asked: set[str] = set()  # name/address/spell version of the above - see _CONTACT_ASKED comment
        self._told: set[str] = set()  # note keys already handed to the model
        self.volunteered: list[dict] = []  # everything that produced a note, for the call report / observability
        self.captured: dict[str, str] = {}  # first_name / last_name / address, deterministic - the GHL-persistence backstop
        self._last_name_field: str = "first_name"  # which field a spelled-letters turn right after corrects, recency fallback
        self._field_hint: str | None = None  # "first_name" / "last_name" if the assistant's last turn named one specifically

    def on_assistant_turn(self, text: str | None) -> None:
        try:
            self._fragments = []
            self._asked = topics_asked(text)
            self._contact_asked = topics_asked(text, _CONTACT_ASKED)  # separate from self._asked - see _CONTACT_ASKED comment
            self._field_hint = _name_target(text)  # "first_name" / "last_name" if THIS question named one specifically
        except Exception:  # never let tracking touch a call
            self._fragments, self._asked, self._contact_asked, self._field_hint = [], set(), set(), None

    def on_user_turn(self, text: str | None) -> list[str]:
        try:
            return [n.text for n in self._detect(text or "")]
        except Exception:
            return []

    def _detect(self, text: str) -> list[Note]:
        if not text.strip():
            return []
        self._fragments.append(text.strip())
        window = " ".join(self._fragments)
        found: list[Note] = []

        r = find_inside_temperature(window)
        if r and "temp" not in self._asked:
            found.append(_temp_note(r))

        m = _UNIT_RE.search(window)
        if m and "unit" not in self._asked:
            seg = _excerpt(window, m, 30)
            if _UNIT_NOT_RUNNING_RE.search(m.group(0)):
                found.append(_unit_note(True, seg))
            elif _UNIT_RUNNING_RE.search(m.group(0)):
                found.append(_unit_note(False, seg))

        if "smell" not in self._asked and (m := _SMELL_RE.search(window)):
            found.append(_smell_note(_excerpt(window, m)))
        if "leak" not in self._asked and (m := _LEAK_RE.search(window)):
            found.append(_leak_note(_excerpt(window, m)))
        if "people" not in self._asked:
            if m := _ALONE_RE.search(window):
                found.append(_people_note(_excerpt(window, m), True))
            elif m := _PEOPLE_RE.search(window):
                found.append(_people_note(_excerpt(window, m), False))

        found.extend(self._detect_contact(window))

        fresh = []
        for note in found:
            if note.key in self._told:
                continue
            # a changed temperature replaces the old one; everything else is told once
            self._told.add(note.key)
            fresh.append(note)
            self.volunteered.append({"topic": note.topic, "key": note.key})
        return fresh

    def _detect_contact(self, window: str) -> list[Note]:
        """Name and address capture (see the module-level comment above `_NAME_CUE_RE`). Always updates `self.captured`, win
        or lose on a note - capture matters even when no note is needed because the question was just asked."""
        notes: list[Note] = []
        name_asked, address_asked = "name" in self._contact_asked, "address" in self._contact_asked

        name_result = _detect_name(window, name_asked, self._field_hint)
        address_result = None

        if name_asked and address_asked and name_result is None \
                and not _ADDRESS_CUE_RE.search(window) and not _NAME_CUE_RE.search(window):
            # both asked together, no explicit cue for either ("Tom Reyes, 12 Cholla Court")
            candidate = _FILLER_LEAD_RE.sub("", window.strip())
            name_part, addr_part = _split_name_and_address(candidate)
            if name_part and _NAME_SHAPE_RE.match(name_part) and not _looks_like_junk_name(name_part):
                first, last = _split_name_words(name_part)
                name_result = (first, last, window.strip())
            if addr_part:
                address_result = (addr_part, window.strip())

        if address_result is None:
            address_result = _detect_address(window, address_asked)

        if name_result:
            first, last, snippet = name_result
            if first:
                self.captured["first_name"] = first
                self._last_name_field = "first_name"
            if last:
                self.captured["last_name"] = last
                self._last_name_field = "last_name"
            if not name_asked:
                notes.append(_name_note(first, last, snippet))

        if address_result:
            address, snippet = address_result
            self.captured["address"] = address
            if not address_asked:
                notes.append(_address_note(address, snippet))

        if "spell" in self._contact_asked and (sm := _SPELLED_LETTERS_RE.search(window)):
            spelled = _join_spelled(sm.group(0))
            if len(spelled) >= 2:
                field = self._field_hint or self._last_name_field
                self.captured[field] = spelled.capitalize()
                notes.append(_spelled_note(field, spelled.upper()))

        return notes
