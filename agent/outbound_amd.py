"""Pure decisions for the Desert Comfort outbound agent (no LiveKit imports, so it tests offline).

Outbound rooms are named out-_<digits>_<tag>; the inbound agent refuses them (see _decline_outbound_rooms in
desert_comfort_agent.py) and desert_comfort_outbound_agent.py is dispatched into them by name.
"""

import re

from closing_ack import backstop_should_end, decide_user_turn, is_assistant_signoff, is_closing_question

OUTBOUND_ROOM_PREFIX = "out-"

# same caller-side closing words as the inbound agent (_CLOSING_SIGNAL_RE in desert_comfort_agent.py)
CLOSING_SIGNAL_RE = re.compile(r"\b(bye|goodbye|talk soon|hang ?up)\b", re.IGNORECASE)

# Only a positive 'human' verdict from AMD (livekit.agents.AMD) keeps the call. Voicemail, a full mailbox, a phone menu, 'uncertain'
# (including "heard nothing for N seconds") and anything unrecognised all end the call silently: this agent must never talk into a
# voicemail (Admin 2026-10-26, after a ~50 s message was left on his voicemail). LiveKit's own default treats 'uncertain' as a person; we do not.
_TALK_CATEGORIES = frozenset({"human"})

# Telnyx penalises outbound calls of 6 seconds or less once they pass 15% of a month's traffic (support.telnyx.com article 1130707), and a
# voicemail counts as a connected call. So nothing this agent decides to hang up on may end sooner than this, measured from the answer
# (6 s plus a margin, since we do not know whether Telnyx counts from the answer or from setup). Alice stays silent in the meantime.
MIN_HANGUP_AGE_SECONDS = 7.5
# Speak first: how long the callee gets to say their own "hello" before Alice opens, and how long a silent line then gets after the opener.
SPEAK_FIRST_SILENCE_SECONDS = 1.5  # was 3.0; Admin chose 1.5 on 2026-10-27 (retune from real recordings of how fast people say hello)
OPENER_SILENCE_SECONDS = 8.0


def hangup_wait_seconds(call_age_seconds: float) -> float:
    return max(0.0, MIN_HANGUP_AGE_SECONDS - call_age_seconds)


def is_outbound_room(room_name: str) -> bool:
    return (room_name or "").startswith(OUTBOUND_ROOM_PREFIX)


def action_for_category(category: str | None) -> str:
    return "talk" if category in _TALK_CATEGORIES else "hangup"


def action_after_amd(category: str | None, reason: str | None, machine_signal: bool) -> str:
    """talk / speak_first / hangup. A machine signal seen while AMD was still deciding overrides everything. Silence from the callee
    ('uncertain' because nothing was heard for SPEAK_FIRST_SILENCE_SECONDS) means a person waiting for us: speak first, and keep the
    machine checks running. Any other doubt hangs up."""
    if machine_signal:
        return "hangup"
    if category == "uncertain" and reason == "no_speech_timeout":
        return "speak_first"
    return action_for_category(category)


def machine_reason(burst_long: bool, tone_fired: bool, words_heard: bool, human_confirmed: bool) -> str | None:
    """Why the callee looks like a machine, or None. A beep always counts; words and a long greeting count only until a person is confirmed
    (a person may say 'voicemail' or talk for a while once the conversation is going)."""
    if tone_fired:
        return "beep"
    if human_confirmed:
        return None
    if words_heard:
        return "voicemail words"
    if burst_long:
        return "long greeting"
    return None


# What a voicemail system says. A second, independent safety net: the callee's own words end the call even if the detector missed it.
# Phrases are deliberately specific so ordinary speech ("I'm not available tomorrow") never matches.
_VOICEMAIL_WORDS_RE = re.compile(
    r"(leave (a|your) message|record your message|after the (tone|beep)|at the (tone|beep)|voice ?mail|mailbox"
    r"|unable to take your call|cannot take your call|can'?t (come|get) to the phone|not available to (take|answer))",
    re.IGNORECASE,
)


def is_voicemail_text(text: str | None) -> bool:
    return bool(_VOICEMAIL_WORDS_RE.search(text or ""))


class ClosingWatcher:
    """The hang-up backstop's memory, reusing the inbound agent's closing_ack rules: a model that says goodbye does not always call its
    hangup tool (seen on the first outbound pickup test, 2026-10-26). The caller feeds it each finished turn; a True return means
    "(re)start the 3 s timer for this item id", and should_end() is the timer's last check. The call is then ended through the same
    OutboundAgent.end_call() as the hangup tool, so there is still only one ending path.

    Same behaviour as the inbound agent's call loop: once the agent has asked the closing question ("any questions before I let you go?")
    a plain decline from the lead ends the call, and the agent's goodbye after an opt-out (consent.take_goodbye_turn) arms the timer."""

    def __init__(self, consent=None) -> None:
        self.consent = consent
        self.active = False
        self.closing_step = False
        self.assistant_last: str | None = None
        self.last_item_id: str | None = None

    def user_turn(self, item_id: str, text: str) -> bool:
        self.last_item_id = item_id
        action = decide_user_turn(text, self.active, self.assistant_last, CLOSING_SIGNAL_RE, self.closing_step)
        if action == "match":
            self.active = True
        elif action == "stand_down":
            self.active = False
        return action in ("match", "keep")

    def assistant_turn(self, item_id: str, text: str) -> bool:
        self.last_item_id = item_id
        self.assistant_last = text
        if is_closing_question(text):
            self.closing_step = True
        elif not self.active and ((self.consent is not None and self.consent.take_goodbye_turn()) or is_assistant_signoff(text)):
            self.active = True
        return self.active

    def should_end(self, trigger_item_id: str, caller_quiet_seconds: float) -> bool:
        return backstop_should_end(trigger_item_id, self.last_item_id, caller_quiet_seconds)
