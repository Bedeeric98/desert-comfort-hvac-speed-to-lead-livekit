"""missing_piece.py: notices when the agent asked for the caller's name AND service address in one question and the caller has
given the name but not the address, so the agent can ask for the address instead of waiting in silence (pure Python, no
network, no secrets).

Why it exists (real call, 2026-10-05 22:05 UTC): the agent asked "Can I get your name and the service address, please?", the caller
answered "So my / name is / Uh Michael / Robinson" and the agent said nothing for about seven seconds (it was waiting for the
address it had asked for) until the caller said "Hello". The prompt now tells the model to ask for the missing piece at once; this
is the mechanical backstop, because a prompt is a nudge, never a lock.

Used by desert_comfort_agent.py through a guarded import; if this file is missing the agent simply runs without the backstop.
"""
from __future__ import annotations

import re

_ASK_BOTH_RE = re.compile(r"\bname\b.{0,70}\baddress\b|\baddress\b.{0,70}\bname\b", re.IGNORECASE | re.DOTALL)
_ASK_ADDRESS_RE = re.compile(r"\baddress\b", re.IGNORECASE)
_REQUEST_RE = re.compile(r"\b(can|could|may|what|what's|please|get|share|give|tell|provide|need)\b", re.IGNORECASE)

_STREET_RE = re.compile(
    r"\b(street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct|circle|place|pl|parkway|pkwy|terrace|trail)\b"
    r"|\d{2,}|\b(hundred|thousand)\b|\baddress is\b",
    re.IGNORECASE,
)
_NAME_PHRASE_RE = re.compile(r"\b(my name is|my name's|name is|name's|this is|i am|i'm|call me)\b[\s.,\-]*(?:uh|um|er)?[\s.,\-]*[A-Z][a-z]{2,}")
_NOT_NAMES = frozenset(
    """Hello Hey Hello Hi Okay Ok Yes Yeah Yep Um Umm Uh Mm Mmm Hmm Well Sure Thanks Thank Please The This That And But Alright Right
    Great Sorry Actually Maybe Nothing Nope Phoenix Arizona Monday Tuesday Wednesday Thursday Friday Saturday Sunday Today Tomorrow""".split()
)


def asked_name_and_address(text: str | None) -> bool:
    """True when the assistant's line is one question asking for BOTH the name and the address."""
    try:
        t = text or ""
        return "?" in t and bool(_ASK_BOTH_RE.search(t)) and bool(_REQUEST_RE.search(t))
    except Exception:
        return False


def asks_for_address(text: str | None) -> bool:
    try:
        return "?" in (text or "") and bool(_ASK_ADDRESS_RE.search(text or ""))
    except Exception:
        return False


def has_address(text: str | None) -> bool:
    """True when the caller's words look like (part of) an address: a street word, a number of two digits or more, hundred/thousand."""
    try:
        return bool(_STREET_RE.search(text or ""))
    except Exception:
        return False


def turn_has_name(text: str | None) -> bool:
    """True when one caller turn looks like (part of) a name: "my name is Michael", or a short turn with a capitalised word that is
    not a common opener ("Uh Michael", "Robinson"). Speech-to-text capitalises names, which is what this leans on."""
    try:
        t = (text or "").strip()
        if not t or "?" in t:
            return False
        if _NAME_PHRASE_RE.search(t):
            return True
        words = re.findall(r"[A-Za-z']+", t)
        if not words or len(words) > 3:
            return False
        return any(w[0].isupper() and len(w) > 2 and w not in _NOT_NAMES for w in words)
    except Exception:
        return False


NUDGE_INSTRUCTIONS = (
    "The caller has given their name but not yet the service address. In one short sentence, say thanks and ask for the full service "
    "address where the technician should come. Do not repeat their name back and do not ask anything else."
)


class MissingPiece:
    """One per call. Feed it every assistant and caller turn; ask wants_nudge() after a caller turn."""

    def __init__(self) -> None:
        self._armed = False
        self._got_name = False
        self._got_address = False

    def on_assistant_turn(self, text: str | None) -> None:
        if asked_name_and_address(text):
            self._armed, self._got_name, self._got_address = True, False, False
        elif self._armed and asks_for_address(text):
            self._armed = False  # the agent asked for the address itself

    def on_user_turn(self, text: str | None) -> None:
        if not self._armed:
            return
        if has_address(text):
            self._got_address = True
        if turn_has_name(text):
            self._got_name = True

    def wants_nudge(self) -> bool:
        return self._armed and self._got_name and not self._got_address

    def take_nudge(self) -> str | None:
        """The instruction to give the model, once per question, or None."""
        if not self.wants_nudge():
            return None
        self._armed = False
        return NUDGE_INSTRUCTIONS
