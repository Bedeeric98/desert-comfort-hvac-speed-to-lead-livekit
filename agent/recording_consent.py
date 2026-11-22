"""Recording consent for the outbound (speed to lead) agent. Admin's rule, 2026-10-27: the lead says yes to recording -> the normal process (audio,
transcript, summary). The lead says no -> Alice says she is NOT recording, the call carries on, and nothing of the conversation is kept except a
summary and the call's details: no audio, no transcript.

Pure Python (no LiveKit), so it is tested offline. This file only holds the STATE and the word checks; the agent decides what to do with them:
  - the log: lines that quote the conversation are held until the lead has answered, then written (yes) or thrown away (no / never answered)
  - the debug audio files: stopped and deleted on a no, and deleted at the end of any call that never got a clear yes
  - the end-of-call report (build step 2): carries no transcript or quotes unless transcript_allowed()

Two ways to decline, so a dropped tool call cannot record someone who said no (the model has dropped consent tool calls before):
  1. the model calls decline_recording
  2. the code hears it: a clear no, "stop recording", or a second reply that is neither yes nor no. Privacy-safe side: anything unclear counts as no.
"""
import re

PENDING, GRANTED, DECLINED = "pending", "granted", "declined"

# Said by Alice when a lead declines. Wording for attorney review. "brief notes" because the summary IS kept: saying nothing is kept would be untrue.
ACK_LINE = "Understood, I won't record this call or keep a transcript of it. I'll just keep brief notes so we can get you scheduled."
ACK_BACKSTOP_INSTRUCTIONS = (
    f"Say exactly: '{ACK_LINE}' Then carry on with the call from where it was, without repeating anything already said or asked."
)

# a clear refusal that mentions recording, anywhere in the reply ("I don't want to be recorded", "please stop recording", "not okay with that")
_REFUSE_RE = re.compile(
    r"\b(don'?t|do not|not|rather not|no)\b[^.?!]{0,25}\b(record(?:ed|ing)?|tape|taped)\b"
    r"|\b(stop|quit)\b[^.?!]{0,15}\brecord"
    r"|\bi'?m not (?:okay|ok|fine|comfortable)\b|\bnot (?:okay|ok|comfortable) with (?:that|it|this)\b",
    re.I,
)
# a plain no at the start of the reply ("no", "nope", "no thanks", "not really"); "no problem" / "no worries" are yeses
_NO_RE = re.compile(r"^\W*(?:no|nope|nah|negative|not really|i'?d rather not|rather not)\b(?!\s+(?:problem|worries))", re.I)
_YES_RE = re.compile(
    r"^\W*(?:(?:oh|ah|well|um|uh|hmm|right)\W+)*(?:yes|yeah|yep|yup|sure|okay|ok|alright|all right|of course|absolutely|go ahead|fine|no problem|no worries|mm-?hm+|uh-?huh"
    r"|that'?s (?:fine|okay|ok|alright)|it'?s (?:fine|okay|ok))\b",
    re.I,
)
_NEGATION_RE = re.compile(r"\b(?:no|not|never|nope|nah|don'?t|do not|won'?t|can'?t|cannot)\b|n'?t\b", re.I)
# Alice's own acknowledgement ("I won't record", "we're not recording")
_ACK_RE = re.compile(r"\b(?:won'?t|will not|not|isn'?t|aren'?t)\b[^.?!]{0,20}\brecord", re.I)

UNCLEAR_REPLIES_BEFORE_DECLINE = 2  # the prompt asks once, then once more; a second reply that is still unclear counts as a no
MAX_HELD_LINES = 300


def classify(text: str) -> str:
    """"no", "yes" or "other" for one reply to the recording question. "other" is anything that is not clearly one or the other."""
    text = (text or "").strip()
    if not text:
        return ""
    if _REFUSE_RE.search(text) or _NO_RE.match(text):
        return "no"
    # a negation or a "but" counts only inside the first clause: "Yes, my AC isn't working" is a yes, "Yes, but I don't care" is not
    first_clause = re.sub(r"\bno (?:problem|worries)\b", "", re.split(r"[.!?;,]", text, maxsplit=1)[0], flags=re.I)
    if _YES_RE.match(text) and not _NEGATION_RE.search(first_clause) and not re.search(r"\bbut\b", re.split(r"[.!?;]", text, maxsplit=1)[0], re.I):
        return "yes"
    return "other"


class RecordingConsent:
    def __init__(self) -> None:
        self.state = PENDING
        self.reason = ""
        self.unclear_replies = 0
        self.ack_owed = False  # declined, and Alice has not yet said she is not recording
        self._armed = False  # becomes true when Alice's first turn (the opener) is in: earlier words from the line are not an answer
        self._ack_seen_since_user_turn = False
        self._held: list[str] = []
        self._released: list[str] = []  # held lines freed by a yes, waiting for take_granted_lines()

    # --- feeding the conversation in ---
    def on_assistant_turn(self, text: str) -> None:
        self._armed = True
        if _ACK_RE.search(text or ""):
            self._ack_seen_since_user_turn = True
            self.ack_owed = False

    def on_user_turn(self, text: str) -> str:
        """Returns "decline" or "grant" when this reply settled the question (the caller then acts on it), else ""."""
        text = (text or "").strip()
        if not text:
            return ""
        self._ack_seen_since_user_turn = False
        if self.state == DECLINED:
            return ""
        if self.state == GRANTED:
            # a lead may take the yes back at any time
            return "decline" if _REFUSE_RE.search(text) and self.decline("withdrawn") else ""
        if not self._armed:
            return ""
        kind = classify(text)
        if kind == "no":
            return "decline" if self.decline("declined") else ""
        if kind == "yes":
            self._released = self.grant()
            return "grant"
        self.unclear_replies += 1
        if self.unclear_replies >= UNCLEAR_REPLIES_BEFORE_DECLINE:
            return "decline" if self.decline("unclear") else ""
        return ""

    # --- the two decisions ---
    def decline(self, reason: str) -> bool:
        """True the first time only. Everything held for the log is thrown away; Alice now owes the acknowledgement unless she just said it."""
        if self.state == DECLINED:
            return False
        self.state = DECLINED
        self.reason = reason
        self._held.clear()
        self.ack_owed = not self._ack_seen_since_user_turn
        return True

    def grant(self) -> list[str]:
        """The log lines held while the question was open (write them now). Does nothing once declined."""
        if self.state != PENDING:
            return []
        self.state = GRANTED
        held, self._held = self._held, []
        return held

    def take_granted_lines(self) -> list[str]:
        """After on_user_turn returned "grant": the log lines that were held, to write now (once)."""
        released, self._released = self._released, []
        return released

    # --- what the rest of the agent asks ---
    def log(self, line: str) -> list[str]:
        """Lines to write to the log right now for a line that quotes the conversation: granted -> it; pending -> held; declined -> dropped."""
        if self.state == GRANTED:
            return [line]
        if self.state == PENDING:
            self._held.append(line)
            del self._held[:-MAX_HELD_LINES]
        return []

    def transcript_allowed(self) -> bool:
        return self.state == GRANTED

    def audio_allowed(self) -> bool:
        """May the debug audio file keep growing? Not once declined; before the answer it does (and is deleted if no yes ever comes)."""
        return self.state != DECLINED

    def audio_may_remain(self) -> bool:
        """At the end of the call: keep the debug audio only with a clear yes."""
        return self.state == GRANTED

    def summary(self) -> str:
        return f"state={self.state} reason={self.reason or '-'} unclear_replies={self.unclear_replies}"
