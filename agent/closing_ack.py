"""closing_ack.py: decides what a caller's turn means once a "bye" has been heard, or right after the end-of-call "any questions?"
step (pure Python, no network, no secrets).

Why it exists (real call, 2026-10-05): the caller said "Okay, no problems. Thank you. Bye", the agent said "You're welcome. Take care."
and the caller then said "Yeah". The old rule treated ANY non-goodbye turn after a closing signal as "the caller kept talking" and
stood the hang-up down, so the session stayed open until the caller said "Bye" again ten seconds later. People answer a goodbye with a
bare acknowledgement all the time ("yeah", "okay", "you too", "thanks"). That is still the end of the call.

The rule: after a closing signal, a turn that is ONLY an acknowledgement does not cancel the hang-up (the 3 second quiet window
restarts from it). Anything with real content still cancels it. If the agent's last line was a question, a bare "yeah" is an
ANSWER to it, so the call is not over and the hang-up stands down as before.

2026-10-05, two more real calls: the prompt's own Stage 5 ("Do you have any questions before I let you go?") lets a caller close
with a plain decline and no "bye" at all — the prompt's own worked examples even show one case with no "bye" ("No, thanks anyway."
-> "No problem at all, take care."). Two real calls answered that question with "Um, no, nothing, nothing at all again. Thank you"
and "No nothing else, I'm good" — the model said a goodbye-shaped reply but never called its own hang-up tool (the same gap this
file's docstring already notes below: real calls show zero "hangup" tool-call log lines). Neither phrase matched the bye/goodbye
backstop, so the caller had to add an explicit "bye" before the call ended. The fix: when the ASSISTANT's last line was that one
specific closing-type question, a plain decline in the caller's reply now also starts the hang-up timer — scoped tightly to that
one moment so a bare "no" anywhere else in the call (e.g. "is anyone else home?" -> "no") is never mistaken for a goodbye.

2026-10-05 22:07 UTC, the first real call after that fix, still failed: the question was answered "Yeah" then "Mm, no" (the leading
"Mm" hid the decline), the agent said "Stay as cool as you can until they arrive. Take care." (nothing armed the timer from the
agent's own sign-off), and the caller's "Nothing" after it cancelled any hope of a hang-up. So: leading filler words no longer hide a
decline, "nothing" counts as one, the agent's own sign-off arms the timer once the Stage 5 question has been asked, and a late
"Nothing"/"No" after that sign-off keeps the timer instead of standing it down.

Used by desert_comfort_agent.py through a guarded import; if this file is missing the agent falls back to the old behaviour.
"""
from __future__ import annotations

import asyncio
import re

# Words that, on their own, are a polite acknowledgement or a sign-off tail and carry no new information.
_ACK_WORDS = frozenset(
    """
    yeah yes yep yup yea ya yah ok okay k kay alright all right sure surely right
    mm mmm mhm mhmm hmm hm uh huh um uhhuh ah oh
    thanks thank you thx ty cheers appreciate it
    great cool perfect good fine nice awesome wonderful lovely sounds sound
    got gotcha understood noted will do
    take care have a day night one weekend you too same to
    see soon later then
    """.split()
)
_MAX_WORDS = 5

# Matches ONLY the Stage 5 "any questions before I let you go?" / "anything else I can help with?" question — the one moment
# where a plain decline (no "bye" required) is allowed to end the call. Deliberately narrow: this must never match an ordinary
# yes/no question asked earlier in the call (e.g. "is anyone else home?"), or a bare "no" there would wrongly end the call.
_CLOSING_QUESTION_RE = re.compile(
    r"(any (?:other )?questions\b.{0,30}?let you go)|(anything else\b.{0,25}?(?:help|i can|for you))",
    re.IGNORECASE,
)

# The assistant's own sign-off. The agent saying one of these means IT has decided the call is over, so the hang-up timer is
# armed from its line too, at any point in the call (second real failure, 2026-10-05 22:07 UTC: the caller said "Mm, no", the
# agent said "...Take care.", and nothing armed the timer because only the caller's words were watched; third, 2026-10-07: an
# opt-out ended the flow before the Stage 5 question was ever reached, so a gate requiring that step first blocked this
# entirely — removed, see desert_comfort_agent.py's call site).
# "thanks? for calling" deliberately does NOT match when followed by "back" — the returning-caller instructions have the
# agent say "thanks for calling back" in response to being asked about a prior visit, early in a call, not as a goodbye.
# Every real goodbye line in the prompt says "thanks for calling" full stop, never "...calling back".
_ASSISTANT_SIGNOFF_RE = re.compile(
    r"\b(take care|good ?bye|bye|talk soon|thanks? for calling\b(?!\s*back)|have a (?:great|good|nice|wonderful|lovely) (?:day|evening|night|one|afternoon)|(?:we|i) won['\u2019]?t (?:contact|call|text|reach out to|be contacting) you again)\b",
    re.IGNORECASE,
)

# The agent's own spoken take-back confirmation after an opt-out (the prompt's line is "No problem at all, I've taken that back."). Used only
# while an opt-out is in force, together with the caller's own take-back words (ConsentFlow.optin_backstop_due), never on its own.
_ASSISTANT_TAKEBACK_RE = re.compile(r"\b(?:taken|took) (?:that|it|this) back\b|\bput you back\b|\bback as you were\b", re.IGNORECASE)


def is_assistant_takeback(text: str | None) -> bool:
    return bool(text and _ASSISTANT_TAKEBACK_RE.search(text))


# A caller turn that only declines, with no new content — the words themselves carry no information, same spirit as
# _ACK_WORDS/is_bare_acknowledgement above, but for "no" rather than "yes".
_DECLINE_WORDS = frozenset("no nope nah none nothing".split())
# Throat-clearing before the real answer. Real call 2026-10-05 22:07: "Mm, no" did not match because "mm" came first.
_FILLER_WORDS = frozenset("mm mmm hmm hm um umm uh uhm er ah oh well so okay ok alright".split())
_DECLINE_PHRASES = (
    "thats all", "that is all", "thats it", "thats everything", "im good", "im all set", "all set", "all good",
    "no thanks", "no thank you", "not right now", "nothing else", "nothing at all", "nothing more", "no more",
    "no questions", "good thanks",
)
# If any of these appear, the turn is raising something new (a question, a correction, a fresh ask) even if it starts with
# "no" — never treat it as a closing decline. STT often drops the "?" (confirmed on a real call: "do you guys do duct
# cleaning" was transcribed with no question mark at all), so this cannot rely on punctuation alone.
_NEW_CONTENT_RE = re.compile(
    r"\b(what|when|where|why|how|who|which|do you|does|can you|could you|would you|is there|are there|also|actually|wait|but)\b",
    re.IGNORECASE,
)
_MAX_DECLINE_WORDS = 12


def is_bare_acknowledgement(text: str | None) -> bool:
    """True when the turn is only acknowledgement words ("Yeah", "Okay, thanks", "You too", "Mm-hmm"). Never raises."""
    try:
        if not text or "?" in text:
            return False
        words = re.sub("[^a-z' ]", " ", text.lower()).replace("'", "").split()
        if not words or len(words) > _MAX_WORDS:
            return False
        return all(w in _ACK_WORDS for w in words)
    except Exception:
        return False


def is_plain_decline(text: str | None) -> bool:
    """True when the whole turn is a short decline with no new content ("No", "No, nothing else, thank you", "I'm good",
    "That's all"). Used ONLY right after the Stage 5 closing question (see _CLOSING_QUESTION_RE) — never call this on its own
    to decide a hang-up. Never raises."""
    try:
        if not text or "?" in text or _NEW_CONTENT_RE.search(text):
            return False
        words = re.sub("[^a-z' ]", " ", text.lower()).replace("'", "").split()
        if not words or len(words) > _MAX_DECLINE_WORDS:
            return False
        collapsed = " ".join(words)
        if any(phrase in collapsed for phrase in _DECLINE_PHRASES):
            return True
        core = list(words)
        while core and core[0] in _FILLER_WORDS:
            core.pop(0)
        return bool(core) and core[0] in _DECLINE_WORDS and all(w in _ACK_WORDS or w in _DECLINE_WORDS or w in _FILLER_WORDS for w in core)
    except Exception:
        return False


def is_closing_question(text: str | None) -> bool:
    """True when the assistant's line is the Stage 5 "any questions before I let you go?" / "anything else I can help with?" step."""
    try:
        return bool(_CLOSING_QUESTION_RE.search(text or ""))
    except Exception:
        return False


def is_assistant_signoff(text: str | None) -> bool:
    """True when the assistant's line is a goodbye ("Take care.", "Thanks for calling.", "Have a great day"). A goodbye is the END of a turn:
    the phrase must be in the LAST sentence and the turn must not end in a question. Real call 2026-10-15 (Step 3 Call 11): "Thanks for
    calling, Michael. I'm sorry to hear that. ... Is your outdoor unit running at all?" armed the hang-up and the call was cut off."""
    try:
        t = (text or "").strip()
        if t.endswith("?"):
            return False
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", t) if s]
        return bool(sentences and _ASSISTANT_SIGNOFF_RE.search(sentences[-1]))
    except Exception:
        return False


# The caller's words are only transcribed ~1-3 s after they stop, so "no new turn yet" does not mean "the caller said nothing": the timer also
# waits until the caller has been quiet this long (real call 2026-10-15: the caller was mid-answer when the timer deleted the room).
BACKSTOP_CALLER_QUIET_SECONDS = 3.0


def backstop_should_end(trigger_item_id, last_item_id, caller_quiet_seconds: float) -> bool:
    """The hang-up timer's last check: end the call only if nobody spoke a finished turn since it was armed AND the caller has been quiet."""
    return trigger_item_id == last_item_id and caller_quiet_seconds >= BACKSTOP_CALLER_QUIET_SECONDS


def decide_user_turn(text: str | None, closing_active: bool, assistant_last_text: str | None, closing_re, closing_step: bool = False) -> str:
    """What to do with a caller turn. `closing_step` is True once the Stage 5 question has been asked at any point in the call.
    Returns one of:

    "match"      the turn itself is a closing signal (bye / goodbye / talk soon / hang up), OR it's a plain decline right
                 after the Stage 5 "any questions?" question (or, once that step is reached, after any statement of the
                 agent's rather than a question): start or restart the hang-up timer
    "ignore"     no closing signal is being watched: nothing to do
    "keep"       a closing signal is being watched and the caller only acknowledged (or, in the closing step, only declined
                 again, e.g. "Nothing" after the agent's goodbye): keep it, restart the quiet window from this turn
    "stand_down" a closing signal is being watched and the caller said something real (or answered a question): cancel it
    """
    try:
        if closing_re.search(text or ""):
            return "match"
        last = assistant_last_text or ""
        assistant_just_asked = last.rstrip().endswith("?")
        if not closing_active:
            if is_plain_decline(text) and (_CLOSING_QUESTION_RE.search(last) or (closing_step and not assistant_just_asked)):
                return "match"
            return "ignore"
        if not assistant_just_asked and is_bare_acknowledgement(text):
            return "keep"
        if closing_step and is_plain_decline(text) and (not assistant_just_asked or _CLOSING_QUESTION_RE.search(last)):
            # a second decline ("Nothing" after "Mm, no"): the agent's goodbye line may not be recorded yet, so the closing question can
            # still be the agent's last recorded line
            return "keep"
        return "stand_down"
    except Exception:
        return "stand_down" if closing_active else "ignore"


class GoodbyeGuard:
    """Has the agent said anything since the caller began the turn that opted out? Real call 2026-10-14 (job AJ_mUax35cVgvSD): the model
    fired opt_out_desertcomfort and then `hangup` without speaking, so the call just ended and the caller had to repeat themselves.
    `agent_started` / `caller_started` are fed from the session's speaking-state events. A caller turn starting resets the flag only until
    the opt-out is seen: once it is, a later "thanks" must not make the agent say its goodbye twice."""

    def __init__(self) -> None:
        self._agent_spoke = False

    def agent_started(self) -> None:
        self._agent_spoke = True

    def caller_started(self, optout_seen: bool) -> None:
        if not optout_seen:
            self._agent_spoke = False

    def owed(self, optout_seen: bool) -> bool:
        return optout_seen and not self._agent_spoke


class ConsentFlow:
    """One per call: is the caller's opt-out IN FORCE right now, and may they still take it back?

    Real-life case (Admin, 2026-10-15, Step 3 Call 7 job AJ_jeBCLF93Ygf3): the caller said "don't contact me again", the agent said its goodbye,
    and a second or two LATER the caller said "wait, never mind, let's continue". The old design hung up in the same turn as the goodbye, so the
    take-back was lost, and the call was filed as "no meaningful conversation" because a transcript read saw the take-back.

    The rules this class holds (the agent code only asks it questions):
      - `heard`: the caller's opt-out words were heard (code watches the caller's words). `fired`: opt_out_desertcomfort has been sent (by the
        model or by the 3.5 s code backstop). Either one means the opt-out is in force (`active`).
      - While active, the model's own `hangup` is ignored: the existing hang-up timer ends the call if the caller stays quiet after the goodbye,
        and that short wait is what lets a caller change their mind. The first agent turn after the opt-out IS the goodbye and arms that timer.
      - A successful grant_opt_in_desertcomfort (`opt_in_granted`) ends the opt-out state; the call carries on as a normal call, and a second
        opt-out later starts a fresh cycle.
      - `lock` makes the opt-out and opt-in requests go out strictly one after the other, so a take-back can never overtake the opt-out and leave
        the contact opted out.
      - The model can SAY "I've taken that back" and still drop the grant_opt_in tool call (Step 3 Call 7a, 2026-10-23: the contact stayed opted out and the
        confirmation text was blocked). So when BOTH the caller's take-back words and the agent's own spoken take-back line have been seen while the opt-out
        is in force, `optin_backstop_due` says so once and the agent code sends the opt-in itself after a short grace window - the model decided, the code only
        makes sure the write follows. A repeated opt-out cancels it.
    Safe direction: nothing here ever restores consent by itself. Only a recorded opt-in does (the model's call, or that backstop once the caller AND the
    agent have both said so); if the write fails the opt-out stays in force."""

    def __init__(self) -> None:
        self.heard = False
        self.fired = False
        self.channels = ""
        self.restored = False
        self._goodbye_pending = False
        self._goodbye_taken = False
        self._takeback_words = False
        self._takeback_said = False
        self._optin_armed = False
        self.lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.heard or self.fired

    def opt_out_heard(self) -> None:
        self.heard = True
        self._clear_takeback()  # a (repeated) opt-out cancels any take-back that was still waiting for its backstop
        if not self._goodbye_taken:
            self._goodbye_pending = True

    def opt_out_claimed(self, channels: str) -> None:
        self.fired = True
        self.channels = channels
        if not self._goodbye_taken:
            self._goodbye_pending = True

    def needs_backstop(self) -> bool:
        """The caller's opt-out words were heard but the tool has not been sent yet."""
        return self.heard and not self.fired

    def take_goodbye_turn(self) -> bool:
        """True exactly once per opt-out: for the first agent turn after it (that turn is the goodbye, so it arms the hang-up timer)."""
        if self.active and self._goodbye_pending:
            self._goodbye_pending = False
            self._goodbye_taken = True
            return True
        return False

    def opt_in_granted(self) -> None:
        self.heard = False
        self.fired = False
        self.restored = True
        self._goodbye_pending = False
        self._goodbye_taken = False
        self._clear_takeback()

    def _clear_takeback(self) -> None:
        self._takeback_words = False
        self._takeback_said = False
        self._optin_armed = False

    def takeback_words_heard(self) -> None:
        """The caller said take-back words (never mind / let's continue ...) - counted only while an opt-out is in force."""
        if self.active:
            self._takeback_words = True

    def takeback_said(self) -> None:
        """The agent said its take-back confirmation line - counted only while an opt-out is in force."""
        if self.active:
            self._takeback_said = True

    def needs_optin_backstop(self) -> bool:
        """An opt-out is in force and the caller AND the agent have both said it is taken back, but no opt-in has been recorded."""
        return self.active and self._takeback_words and self._takeback_said

    def optin_backstop_due(self) -> bool:
        """True exactly once per take-back: the moment both signals are in (the agent then starts the backstop timer)."""
        if self.needs_optin_backstop() and not self._optin_armed:
            self._optin_armed = True
            return True
        return False

    def hangup_allowed(self) -> bool:
        return not self.active
