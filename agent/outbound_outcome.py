"""How the OUTBOUND (speed to lead) agent labels a finished call. Pure Python, no LiveKit imports, so it tests offline.

The label set is the receptionist's own (desert_comfort_agent._derive_outcome, same rungs in the same spirit: deterministic facts from the
tool calls first, a language-model read only as the last resort), plus the few outcomes an inbound call can never produce:
  voicemail        a machine answered (voicemail, full mailbox, phone menu, beep, call screener). Alice hung up without a word.
  silent_pickup    the line was answered but nobody spoke after the opener.
  callback_requested  the lead asked to be called later, or wants texts only, or needs another language: a PERSON follows up.
  no_answer        never answered: rang out, busy or declined (dial_outcome, sent by the agent's own dial, 2026-10-31).
  dial_failed      the call could not be placed: bad number, trunk error. A person checks the number; no retry.
Sequence rules for n8n: ONLY voicemail, silent_pickup and no_answer continue to the next attempt. Every other outcome stops the sequence.

Where this deliberately differs from the inbound ladder (Admin decisions 2026-10-29/30, plan section 7b):
  - an opt-out in force wins before the qualified rungs (compliance first); inbound checks it after them.
  - a renter (Alice said the renter line) is qualified_not_booked with a reason, even if the fee was never quoted.
  - callback / texts-only / other-language hand-offs are read from Alice's own scripted lines (the prompt makes her say them nearly word for word).
"""
import re
from dataclasses import dataclass, field

import call_checkup as cc

UNREACHED_OUTCOMES = frozenset({"voicemail", "silent_pickup"})

# SIP answers that mean the lead simply did not pick up: request timeout, temporarily unavailable, busy, ring cancelled, busy everywhere, declined.
NO_ANSWER_SIP_CODES = frozenset({408, 480, 486, 487, 600, 603})


def dial_outcome(sip_status_code: int | None, twirp_code: str = "") -> str:
    """The outcome of a dial that never connected. Anything not clearly 'nobody picked up' is dial_failed, so a person looks at it."""
    if sip_status_code in NO_ANSWER_SIP_CODES or (sip_status_code is None and twirp_code == "deadline_exceeded"):
        return "no_answer"
    return "dial_failed"

# Alice's scripted hand-off lines (speed-to-lead-prompt.md). A drift in her wording falls through to the model read below.
_RENTER_LINE_RE = re.compile(r"since you.?re renting", re.I)
_CALLBACK_LINE_RE = re.compile(r"\bteam member will call you\b", re.I)
_TEXT_ONLY_LINE_RE = re.compile(r"\bteam member will text you\b", re.I)
_LANGUAGE_LINE_RE = re.compile(r"team member who speaks (?:your|their) language|miembro del equipo|alguien del equipo|persona del equipo", re.I)
# The lead denying they ever asked to be contacted (a consent dispute). Deliberately strict: the code treats a hit as an opt-out, so ordinary talk
# ("I didn't request a callback today") must not match. "Wrong number" is NOT here: a person who says it is the wrong-person case, not a dispute.
CONSENT_DISPUTE_RE = re.compile(
    r"\bnever (?:filled|fill|submitted|signed up|sent in)\b|\bdid(?:n'?t| not) (?:fill|submit|sign up|send in)\b|"
    r"\b(?:never|did(?:n'?t| not)) (?:ask|asked|request|requested) (?:for )?(?:this|any|a call|you)\b", re.I)


@dataclass
class Outcome:
    outcome: str
    reason: str = ""
    flags: dict = field(default_factory=dict)  # consent_dispute, renter, text_only, language_handoff, emergency_unbooked


def _assistant_said(turns, regex) -> bool:
    return any(regex.search(t.text) for t in turns if t.kind == "assistant")


def _user_said(turns, regex) -> bool:
    return any(regex.search(t.text) for t in turns if t.kind == "user")


def emergency_unbooked(outcome: str, quote_tier: str, caller_temperature) -> bool:
    """The lead matched the emergency criteria (the emergency fee was quoted, or they gave 90 degrees or more) and did not book. Admin 2026-10-30:
    a plain notification to the team, not a red alert."""
    if outcome in ("booked_standard", "booked_emergency", "voicemail", "silent_pickup", "dead_air", "do_not_call", "out_of_area", "transferred"):
        return False
    try:
        hot = caller_temperature is not None and float(caller_temperature) >= 90
    except (TypeError, ValueError):
        hot = False
    return quote_tier == "emergency" or hot


async def derive_outcome(items, *, machine: bool, silent_line: bool, optout_in_force: bool, classify) -> Outcome:
    """items: the session's chat history items. classify: async callable(transcript_text) -> one of the labels in CLASSIFIER_LABELS (the model
    read, injected so this file stays testable)."""
    if machine:
        return Outcome("voicemail", "a machine answered")
    if silent_line:
        return Outcome("silent_pickup", "nobody spoke after the opener")

    turns, _ = cc.normalize(items)
    if not any(t.kind == "user" for t in turns):
        return Outcome("dead_air", "the lead never said a word")
    flags: dict = {}

    booked = cc.successful_booking_args(items)
    if booked is not None:
        return Outcome("booked_emergency" if booked.get("urgent") else "booked_standard")
    if any(t.kind == "tool_call" and t.name == "transfer_to_human" for t in turns):
        return Outcome("transferred")
    for t in turns:
        if t.kind == "tool_result" and t.name == "radius_checker_desertcomfort" and isinstance(t.output, dict) and not t.output.get("in_service_area", True):
            return Outcome("out_of_area")

    if optout_in_force:
        if _user_said(turns, CONSENT_DISPUTE_RE):
            flags["consent_dispute"] = True
            return Outcome("do_not_call", "the lead says they never asked for this call", flags)
        return Outcome("do_not_call")

    if _assistant_said(turns, _RENTER_LINE_RE):
        flags["renter"] = True
        return Outcome("qualified_not_booked", "renter - landlord approval needed", flags)
    if _assistant_said(turns, _TEXT_ONLY_LINE_RE):
        flags["text_only"] = True
        return Outcome("callback_requested", "prefers text", flags)
    if _assistant_said(turns, _LANGUAGE_LINE_RE):
        flags["language_handoff"] = True
        return Outcome("callback_requested", "needs another language (Spanish speaker)", flags)
    if _assistant_said(turns, _CALLBACK_LINE_RE):
        return Outcome("callback_requested", "asked to be called back")

    if any(t.kind == "tool_call" and t.name == cc.AVAILABILITY_TOOL for t in turns):
        return Outcome("qualified_not_booked", "no booking was made after the schedule was checked")
    if cc.quote_reached_in(items) and not cc.optout_in(items):
        return Outcome("qualified_not_booked", "left after the fee quote")

    transcript = "\n".join(f"{'Lead' if t.kind == 'user' else 'Assistant'}: {t.text}" for t in turns if t.kind in ("user", "assistant"))
    label = await classify(transcript)
    if label == "wrong_person":
        return Outcome("wrong_person")
    if label == "do_not_call":
        return Outcome("do_not_call", "asked to stop (read from the conversation)")
    if label == "not_interested":
        return Outcome("no_meaningful_conversation", "not interested")
    return Outcome("no_meaningful_conversation")


CLASSIFIER_LABELS = ("do_not_call", "wrong_person", "not_interested", "no_meaningful_conversation")
CLASSIFIER_SYSTEM_PROMPT = (
    "Classify this HVAC service call transcript (a business calling someone who filled out a form) into exactly one outcome. "
    "Respond with only the outcome word, nothing else.\n\n"
    "do_not_call: the person explicitly asked to be removed from the call list or to stop being contacted.\n"
    "wrong_person: the person said this is not who was meant to be reached, or they have no relationship to the business.\n"
    "not_interested: the person said they no longer need help or changed their mind.\n"
    "no_meaningful_conversation: none of the above - the default."
)


def parse_classifier_label(raw: str) -> str:
    label = (raw or "").strip().lower()
    return label if label in CLASSIFIER_LABELS else "no_meaningful_conversation"
