"""Post-call check-up for the Desert Comfort voice agents: "what went wrong on this call?"

Built 2026-10-02 at Admin's request (an observability layer that checks for the things that went wrong on a call and points them out).
Runs inside the agent process, once, when the call ends. Reads LiveKit's own session record (every message, every tool call and its
result, error events, timestamps), applies plain rules, and returns flags. The agent puts them in the call-end report; n8n only
routes the alert. Pure Python, no network, no AI call, a few milliseconds. It can never touch a live call (it runs after hang-up) and
the caller of `run_from_session` must wrap it so a bug here can only cost the flags, never the report.

Severity (Admin, 2026-10-02): group A (lost or wrong bookings) and group B (wrong answer to the customer) are RED = alert at once.
Groups C (caller felt unheard), D (silence and lag) and E (business signals) are YELLOW = counted and shown in the daily digest during
a two-week learning mode, then reviewed. Every rule was written against real calls (2026-09-29 to 2026-10-02).

Honest limits: rules catch only patterns we know to look for; a transcript cannot show tone or pronunciation (that needs audio review);
"emergency missed" and "address made up" read the caller's words with simple rules, so evidence is always attached for a human to judge.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field

from closing_ack import is_assistant_takeback
from known_facts import (KnownFacts, MAX_INSIDE_F, MIN_INSIDE_F, TempReading, _DEGREE_WORDS, _INSIDE_WORDS, _PEOPLE_RE, _SMELL_RE,
                         _UNIT_NOT_RUNNING_RE, _UNIT_RE, _parse_numbers, _tokens, find_inside_temperature, topics_asked)

CHECKUP_VERSION = 1
SLOW_TOOL_SECONDS = 8.0
BOOKING_TOOL = "book_appointment_desertcomfort"
AVAILABILITY_TOOL = "check_availability_desertcomfort"
# Tools that legitimately hand back nothing (the SDK records an empty string for them). An empty result from one of these is not a failure.
NO_OUTPUT_TOOLS = frozenset({"hangup"})


def parse_tool_output(raw):
    """A tool result as the SDK stores it -> the data. The SDK stores a dict result as Python's str(dict) (single quotes, True/False/None),
    which is NOT JSON (found 2026-10-02 on the first real call after the check-up shipped: a real booking was read as "no booking").
    Tries JSON first, then Python literal syntax (literals only, never executes anything); anything else comes back unchanged."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        pass
    try:
        return ast.literal_eval(raw)
    except Exception:  # ValueError, SyntaxError, MemoryError, RecursionError: it is plain text, not data
        return raw

# ---------------------------------------------------------------------------
# The call as a flat list of turns
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    seq: int
    kind: str  # user | assistant | tool_call | tool_result
    t: float | None = None  # seconds (the SDK's created_at), None when unknown
    text: str = ""
    name: str = ""  # tool name
    args: dict = field(default_factory=dict)
    output: object = None  # parsed tool output
    is_error: bool = False
    call_id: str = ""


def normalize(items, error_events=()) -> tuple[list[Turn], list[str]]:
    """LiveKit chat-history items -> [Turn]. Reads attributes with getattr so it works with the SDK's own objects and with plain test
    doubles. Unknown item types are ignored."""
    turns: list[Turn] = []
    try:
        items = list(items or [])
    except TypeError:
        items = []
    for i, it in enumerate(items):
        typ = getattr(it, "type", None)
        t = getattr(it, "created_at", None)
        if typ == "message":
            role = getattr(it, "role", "")
            text = (getattr(it, "text_content", None) or "").strip()
            if role in ("user", "assistant") and text:
                turns.append(Turn(i, role, t, text))
        elif typ == "function_call":
            try:
                args = json.loads(getattr(it, "arguments", "") or "{}")
            except (TypeError, ValueError):
                args = {}
            turns.append(Turn(i, "tool_call", t, name=getattr(it, "name", ""), args=args if isinstance(args, dict) else {}, call_id=getattr(it, "call_id", "")))
        elif typ == "function_call_output":
            out = parse_tool_output(getattr(it, "output", ""))
            if isinstance(out, list) and out and isinstance(out[0], dict):  # the router can wrap the reply in a one-item list
                out = out[0]
            turns.append(Turn(i, "tool_result", t, name=getattr(it, "name", ""), output=out, is_error=bool(getattr(it, "is_error", False)), call_id=getattr(it, "call_id", "")))
    errors = [repr(getattr(e, "error", e))[:160] for e in (error_events or [])]
    return turns, errors


def _result_ok(turn: Turn) -> bool:
    """A tool result counts as success only if it did not crash AND did not hand back an error message. The tool wrapper returns
    {"error": "request_failed"} / {"error": "validation_failed", ...} as ordinary data, so `is_error` alone says "fine" for a failed
    booking (found 2026-10-02: the old outcome ladder called any booking call that did not crash a booking)."""
    if turn.is_error:
        return False
    out = turn.output
    if isinstance(out, dict):
        return not out.get("error")
    if turn.name in NO_OUTPUT_TOOLS:  # hangup returns nothing by design; the SDK records "" for it
        return True
    return out is not None and out != ""


def booking_succeeded(turn: Turn) -> bool:
    """A real booking comes back with an appointment id (and status "booked"); anything else is not a booking."""
    if turn.name != BOOKING_TOOL or not _result_ok(turn):
        return False
    out = turn.output
    return isinstance(out, dict) and bool(out.get("id"))


# ---------------------------------------------------------------------------
# Words and patterns
# ---------------------------------------------------------------------------

CLAIM_RE = re.compile(r"\b(you'?re|you are) (all set|confirmed|booked|scheduled)\b|\ball booked\b|\blocked in\b|\bi'?ve (booked|scheduled)\b|"
                      r"\b(it'?s|that'?s|appointment (is|has been)) (booked|confirmed|scheduled)\b|\bwe'?ve got you (booked|confirmed|scheduled)\b", re.I)
OFFER_RE = re.compile(r"\b(availab\w+|open slot|next open|opening)\b|\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(:\d\d)? ?(a\.?m\.?|p\.?m\.?)", re.I)
ACCEPT_RE = re.compile(r"\b(yes|yeah|yep|sure|okay|ok|works|sounds good|book it|let'?s do|let'?s go|go ahead|perfect|that'?s fine|please do)\b", re.I)
# A caller who agrees then takes it back before the agent can act did not lose a booking - the agent has nothing to act on. Found
# 2026-10-07/08: two RED A2 hits on the same day were both this shape (real calls, verified against the agent's own session log),
# not a dropped booking.
RETRACT_RE = re.compile(r"\b(call (you |me )?back|never ?mind|actually,? (hold|wait)|hold on|wait a (sec|second|minute)|"
                        r"let me think|think (it over|about it)|forget it|cancel that|i'?ll (get back to you|call back|think))\b", re.I)
# Fees changed 2026-10-13: emergency one hundred twenty, standard eighty (were 150 / 120). "one hundred fifty" / "150" stay recognised as emergency so older
# transcripts still read correctly. "eighty" alone is never a fee: it is also every "eighty-nine degrees".
# ("one hundred and twenty" is how the FAQ doc words it, so the optional "and" is accepted everywhere)
QUOTE_RE = re.compile(r"trip fee|\$\s?(80|120|150)\b|\b(one hundred (and )?(twenty|fifty)|eighty|80|120|150)\b[^.?!]{0,25}dollar", re.I)
FEE_EMERGENCY_RE = re.compile(r"one hundred (and )?fifty|one hundred (and )?twenty|\b150\b|\b120\b|ninety minutes|90 minutes", re.I)
FEE_STANDARD_RE = re.compile(r"eighty dollars?|\$\s?80\b|\b80 dollars?\b|four to six|4 to 6|4-6|two to four|2 to 4|2-4", re.I)
OPTOUT_RE = re.compile(r"\b(stop|quit) (calling|contacting|texting)\b|\bdon'?t (call|text|contact) (me|us)\b|\bdo not (call|contact)\b|\btake me off\b|\bremove me\b|\bunsubscribe\b|\bnever call (me )?again\b", re.I)
_ACTIVE_LEAK_RE = re.compile(r"\b(leak|leaks|leaking|leaked|dripping|pooling|puddles?|flooding|flooded)\b", re.I)
_NEGATION_RE = re.compile(r"\b(no|not|nothing|nobody|never|don'?t|doesn'?t|isn'?t|aren'?t|without|none|nope)\b", re.I)
_ADDR_STOP = {"street", "avenue", "road", "drive", "lane", "court", "boulevard", "circle", "place", "phoenix", "arizona", "north", "south", "east",
              "west", "unit", "apartment", "suite", "mesa", "tempe", "chandler", "scottsdale", "glendale", "gilbert", "peoria", "usa"}


@dataclass
class Flag:
    rule: str
    severity: str  # RED | YELLOW
    group: str  # A..E
    evidence: str
    seq: int | None = None

    def to_dict(self) -> dict:
        d = {"rule": self.rule, "severity": self.severity, "group": self.group, "evidence": self.evidence[:200]}
        if self.seq is not None:
            d["turn"] = self.seq
        return d


def _clip(s: str, n: int = 160) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def spoken_temperature(text: str) -> TempReading | None:
    """The inside temperature the AGENT said out loud, e.g. "so eighty-six inside" / "eighty-six degrees in here". Only a number that is
    followed (optionally after "degrees") by an inside word counts, so "ninety minutes", "an eighty dollar fee", an address or a threshold
    such as "ninety degrees or above" are never read as the caller's temperature."""
    toks = _tokens(text)
    found: TempReading | None = None
    for num in _parse_numbers(toks):
        if not MIN_INSIDE_F <= num.value <= MAX_INSIDE_F:
            continue
        j = num.end
        if j < len(toks) and toks[j][0] in _DEGREE_WORDS:
            j += 1
        w = [toks[k][0] if k < len(toks) else "" for k in (j, j + 1, j + 2)]
        if w[0] in _INSIDE_WORDS or (w[0] == "in" and (w[1] in _INSIDE_WORDS or (w[1] in ("the", "your") and w[2] in _INSIDE_WORDS))):
            lo = toks[max(0, num.start - 3)][1]
            hi = toks[min(len(toks) - 1, j + 2)][2]
            found = TempReading(num.value, True, text[lo:hi].strip())
    return found


class _Call:
    """Everything the rules need, computed once."""

    def __init__(self, turns: list[Turn], outcome: str, errors: list[str], known_addresses, spoke_after_optout_secs=None, opt_out_restored=False):
        self.turns, self.outcome, self.errors = turns, outcome or "", errors
        self.spoke_after_optout_secs = spoke_after_optout_secs
        self.opt_out_restored = bool(opt_out_restored)  # the agent recorded a take-back (grant_opt_in) and the call carried on: a normal call
        self.known_addresses = [a for a in (known_addresses or []) if a]
        self.users = [t for t in turns if t.kind == "user"]
        self.assistants = [t for t in turns if t.kind == "assistant"]
        self.tool_calls = [t for t in turns if t.kind == "tool_call"]
        self.tool_results = [t for t in turns if t.kind == "tool_result"]
        self.booking_results = [t for t in self.tool_results if t.name == BOOKING_TOOL]
        self.booking_ok = any(booking_succeeded(t) for t in self.booking_results)
        self.booking_args = next((c.args for c in self.tool_calls if c.name == BOOKING_TOOL and any(booking_succeeded(r) and r.call_id == c.call_id for r in self.booking_results)), {})
        # consecutive caller turns are one statement (a sentence can arrive in fragments)
        self.statements: list[tuple[int, str]] = []
        prev_user = False
        for t in turns:
            if t.kind == "user":
                if prev_user and self.statements:
                    self.statements[-1] = (self.statements[-1][0], self.statements[-1][1] + " " + t.text)
                else:
                    self.statements.append((t.seq, t.text))
                prev_user = True
            elif t.kind == "assistant":
                prev_user = False
        quote_turns = [a for a in self.assistants if QUOTE_RE.search(a.text)]
        self.quote_reached = bool(quote_turns)
        # The quoted tier comes from the last turn that names ONE tier. A later answer that only says "trip fee" (the repair-cost and trust FAQ
        # answers) or names both fees (the price-objection answers) must not overwrite what the caller was actually quoted (found 2026-10-13).
        def _tiers(text: str) -> tuple[bool, bool]:
            return bool(FEE_EMERGENCY_RE.search(text)), bool(FEE_STANDARD_RE.search(text))
        clear = [a for a in quote_turns if sum(_tiers(a.text)) == 1]
        named = [a for a in quote_turns if sum(_tiers(a.text)) >= 1]
        pick = (clear or named or quote_turns or [None])[-1]
        last_quote = pick.text if pick else ""
        self.quote_tier = "emergency" if FEE_EMERGENCY_RE.search(last_quote) and not FEE_STANDARD_RE.search(last_quote) else ("standard" if FEE_STANDARD_RE.search(last_quote) else "")
        # Inside temperature. The caller's words come from speech-to-text, which can mishear an accent ("eighty-six" -> "eighty sea"); the agent hears
        # the audio and says the number back so the caller can correct it. When the two disagree the agent's spoken number is the record (Admin,
        # 2026-10-13); rule C6 shows every disagreement in the daily digest.
        self.temp_text, text_seq = None, None
        for seq, text in self.statements:
            r = find_inside_temperature(text)
            if r:
                self.temp_text, text_seq = r, seq  # the latest reading wins
        self.temp_spoken = None
        if text_seq is not None:  # only ever a correction of a number the caller gave; never a reading on its own (the agent also states rules)
            for a in self.assistants:
                if a.seq <= text_seq:
                    continue
                r = spoken_temperature(a.text)
                if r:
                    self.temp_spoken = r
                    break  # the first thing the agent said back after the caller's last reading
        self.temp = self.temp_spoken or self.temp_text
        self.temp_who = "the agent repeated back" if self.temp_spoken else "caller said"
        self.temp_mismatch = bool(self.temp_text and self.temp_spoken and self.temp_text.value != self.temp_spoken.value)
        self.all_caller_text = " ".join(t.text for t in self.users)

    def tier_booked(self) -> str:
        if not self.booking_ok:
            return ""
        return "emergency" if self.booking_args.get("urgent") else "standard"


def _unnegated(text: str, rx: re.Pattern) -> re.Match | None:
    """First match of rx in text that is not preceded by a negation in the same clause ("no burning smell" does not count)."""
    for m in rx.finditer(text):
        clause = re.split(r"[.;!?]", text[: m.start()])[-1]
        if not _NEGATION_RE.search(clause[-40:]) and not _NEGATION_RE.search(text[m.end():m.end() + 14]):
            return m
    return None


def _emergency_evidence(c: _Call) -> list[str]:
    """Reasons the caller's own words made this an emergency regardless of temperature (smell, active leak, vulnerable person with the
    unit not running)."""
    why: list[str] = []
    unit_off = False
    asked_unit = False
    for t in c.turns:
        if t.kind == "assistant":
            asked_unit = "unit" in topics_asked(t.text)
        elif t.kind == "user":
            m = _UNIT_RE.search(t.text)
            if (m and _UNIT_NOT_RUNNING_RE.search(m.group(0))) or (asked_unit and re.match(r"\W*(no|nope|not|it'?s not|it isn'?t|dead|off)\b", t.text, re.I)):
                unit_off = True
            asked_unit = False
    for _, text in c.statements:
        if m := _unnegated(text, _SMELL_RE):
            why.append(f"smell/gas/burning: '{_clip(text[max(0, m.start() - 40):m.end() + 40], 90)}'")
        if m := _unnegated(text, _ACTIVE_LEAK_RE):
            why.append(f"active leak: '{_clip(text[max(0, m.start() - 40):m.end() + 40], 90)}'")
        if unit_off and (m := _unnegated(text, _PEOPLE_RE)):
            why.append(f"vulnerable person with unit off: '{_clip(text[max(0, m.start() - 40):m.end() + 40], 90)}'")
    return why


# ---------------------------------------------------------------------------
# Group A - lost or wrong bookings (RED)
# ---------------------------------------------------------------------------


def rule_a1_said_booked_not_booked(c: _Call) -> list[Flag]:
    if c.booking_ok:
        return []
    for a in c.assistants:
        if CLAIM_RE.search(a.text):
            return [Flag("A1_said_booked_not_booked", "RED", "A", f"agent said '{_clip(a.text)}' but no booking was made", a.seq)]
    return []


def rule_a2_agreed_never_booked(c: _Call) -> list[Flag]:
    if c.booking_ok:
        return []
    offer_seq = next((a.seq for a in c.assistants if OFFER_RE.search(a.text) and re.search(r"\?|work|fit|which|does that", a.text, re.I)), None)
    avail_ok = any(t.name == AVAILABILITY_TOOL and _result_ok(t) for t in c.tool_results)
    if offer_seq is None and not avail_ok:
        return []
    after = [u for u in c.users if u.seq > (offer_seq if offer_seq is not None else -1)]
    accept = next((u for u in after if ACCEPT_RE.search(u.text) and len(u.text) < 120), None)
    if not (accept and (offer_seq is not None)):
        return []
    tried_after = any(t.seq > accept.seq and t.name in (AVAILABILITY_TOOL, BOOKING_TOOL) for t in c.tool_calls)
    if tried_after:
        return []  # a real attempt followed the accept; A3/A4 judge that outcome, not this rule
    stalled_after = any(STALL_RE.search(a.text) for a in c.assistants if a.seq > accept.seq)
    if any(RETRACT_RE.search(u.text) for u in c.users if u.seq > accept.seq) and not stalled_after:
        # Only a clean, unprompted retraction downgrades. If the agent was visibly stalling first (D2_agent_stalled already
        # flags that), the caller giving up is a symptom of a real stuck booking, not a change of mind - stays RED (found
        # 2026-09-29 real call 18018: caller only bailed after 3 stall lines and two unanswered "hello"s).
        return [Flag("A2_accepted_then_retracted", "YELLOW", "A",
                      f"caller said '{_clip(accept.text, 80)}' then took it back before any booking attempt", accept.seq)]
    return [Flag("A2_agreed_never_booked", "RED", "A", f"after a time was offered the caller said '{_clip(accept.text, 80)}' but no booking was made", accept.seq)]


def rule_a3_tool_failed_or_slow(c: _Call) -> list[Flag]:
    flags: list[Flag] = []
    by_name: dict[str, list[Turn]] = {}
    for r in c.tool_results:
        by_name.setdefault(r.name, []).append(r)
    calls_by_id = {t.call_id: t for t in c.tool_calls}
    for name, results in by_name.items():
        final = results[-1]
        if not _result_ok(final):
            err = final.output.get("error") if isinstance(final.output, dict) else (str(final.output)[:60] if final.output else "error")
            flags.append(Flag("A3_tool_failed", "RED", "A", f"{name} ended in an error ({err}) and never recovered", final.seq))
        elif any(not _result_ok(r) for r in results[:-1]):
            flags.append(Flag("A3_tool_failed_then_recovered", "YELLOW", "A", f"{name} failed once, then succeeded", final.seq))
    for r in c.tool_results:
        call = calls_by_id.get(r.call_id)
        if call and call.t is not None and r.t is not None and r.t - call.t > SLOW_TOOL_SECONDS:
            flags.append(Flag("A3_tool_slow", "YELLOW", "A", f"{r.name} took {r.t - call.t:.0f}s", r.seq))
    return flags


def rule_a4_booked_recorded_wrong(c: _Call) -> list[Flag]:
    recorded_booked = c.outcome in ("booked_standard", "booked_emergency")
    if c.booking_ok and not recorded_booked:
        return [Flag("A4_booked_but_recorded_wrong", "RED", "A", f"a booking succeeded but the call outcome is '{c.outcome}'")]
    if recorded_booked and not c.booking_ok:
        return [Flag("A4_booked_but_recorded_wrong", "RED", "A", f"the call outcome is '{c.outcome}' but no booking succeeded")]
    return []


# ---------------------------------------------------------------------------
# Group B - wrong answer to the customer (RED)
# ---------------------------------------------------------------------------


def _tier_given(c: _Call) -> str:
    return c.tier_booked() or c.quote_tier


def rule_b1_wrong_tier_for_temperature(c: _Call) -> list[Flag]:
    if not c.temp:
        return []
    tier = _tier_given(c)
    if not tier:
        return []
    n = c.temp.value
    if n >= 90 and tier == "standard":
        return [Flag("B1_wrong_tier_for_temperature", "RED", "B", f"{c.temp_who} about {n} degrees (90 or above is an emergency) but got the standard tier: '{_clip(c.temp.snippet, 90)}'")]
    if n < 90 and tier == "emergency" and not _emergency_evidence(c):
        return [Flag("B1_wrong_tier_for_temperature", "RED", "B", f"{c.temp_who} about {n} degrees (below 90) with no other emergency reason but got the emergency tier: '{_clip(c.temp.snippet, 90)}'")]
    return []


def rule_b2_emergency_missed(c: _Call) -> list[Flag]:
    tier = _tier_given(c)
    if tier == "emergency" or not (tier or c.quote_reached or c.booking_ok):
        return []
    why = _emergency_evidence(c)
    if why:
        return [Flag("B2_emergency_missed", "RED", "B", "caller reported an emergency criterion but the call went standard - " + "; ".join(why[:2]))]
    return []


def _address_tokens(address: str) -> list[str]:
    toks = re.findall(r"[A-Za-z]{4,}", address or "")
    return [t.lower() for t in toks if t.lower() not in _ADDR_STOP]


def rule_b3_address_made_up(c: _Call) -> list[Flag]:
    addr = (c.booking_args or {}).get("address", "")
    toks = _address_tokens(addr)
    if not c.booking_ok or not toks:
        return []
    heard = c.all_caller_text.lower() + " " + " ".join(c.known_addresses).lower()
    if not any(t in heard for t in toks):
        return [Flag("B3_address_made_up", "RED", "B", f"the booked address '{_clip(addr, 80)}' shares no street name with anything the caller said or the address on file")]
    return []


def rule_b4_optout_not_recorded(c: _Call) -> list[Flag]:
    if c.opt_out_restored:  # 2026-10-15: the caller took the opt-out back and that was recorded; the outcome is rightly not do_not_call
        return []
    for u in c.users:
        if OPTOUT_RE.search(u.text) and c.outcome != "do_not_call":
            return [Flag("B4_optout_not_recorded", "RED", "B", f"caller said '{_clip(u.text, 80)}' but the outcome is '{c.outcome}', not do_not_call", u.seq)]
    return []


# The five facts the prompt makes her establish before the fee, in the words a human reads in the alert. Real call 2026-10-26 (H1+H2): she asked the outdoor unit,
# smells, leaks and elderly/children, quoted the standard fee, and never asked the temperature until the caller said so. A skipped temperature can mean the wrong tier.
QUALIFYING_FACTS = (("unit", "the outdoor unit"), ("temp", "the inside temperature"), ("smell", "smells"), ("leak", "leaks"), ("people", "elderly people, children or medical conditions"))
PRICE_ASK_RE = re.compile(r"\b(how much|cost|costs|price|charge|charges|fee|pay|expensive)\b", re.I)


def rule_b5_qualifying_question_skipped(c: _Call) -> list[Flag]:
    """A standard fee was quoted and one of the five qualifying facts had been neither asked by the agent nor said by the caller by then. Silent on every call that
    never got to the fee (dead air, caller left, opt-out, out of area before it) and on an emergency quote (the prompt stops qualifying on purpose). An early answer
    to the caller's own "how much?" is not the quote. Asking the question AFTER the fee (because the caller noticed) still counts as skipped."""
    if c.quote_tier != "standard":
        return []
    kf, covered, said, quote_seq = KnownFacts(), set(), [], None
    for t in c.turns:
        if t.kind == "user":
            kf.on_user_turn(t.text)
            said.append(t.text)
        elif t.kind == "assistant":
            is_quote = bool(QUOTE_RE.search(t.text)) and not PRICE_ASK_RE.search(" ".join(said))
            said = []
            kf.on_assistant_turn(t.text)
            if not is_assistant_takeback(t.text):
                covered |= topics_asked(t.text)  # a question asked in the same turn as the fee counts too (real call 18040)
            if is_quote:
                quote_seq = t.seq
                break
    if quote_seq is None:
        return []
    covered |= {v["topic"] for v in kf.volunteered}
    missing = [label for key, label in QUALIFYING_FACTS if key not in covered]
    if not missing:
        return []
    return [Flag("B5_qualifying_question_skipped", "RED", "B", "the fee was quoted without ever asking (or hearing) about: " + ", ".join(missing), quote_seq)]


# ---------------------------------------------------------------------------
# Group C - the caller felt unheard (YELLOW)
# ---------------------------------------------------------------------------

REPEAT_RE = re.compile(r"\b(i (just )?said|i just told you|i already (said|told)|like i said|as i said|i('ve| have) (said|told)|i mentioned)\b", re.I)
CORRECTION_RE = re.compile(r"you got me wrong|that'?s not what i said|that'?s not right|that'?s wrong|\bno,? i said\b|not what i (said|meant)|\byou misheard\b", re.I)
CONFUSED_RE = re.compile(r"didn'?t (get|catch|hear|understand)|what did you say|say that again|can you repeat|\bpardon\b|come again|i don'?t understand", re.I)
ASK_ADDR_RE = re.compile(r"what('s| is) (the |your )?(right |correct )?(service |full |complete )?address|(can|could) i (get|have) (your |the )?(name and )?(the |your )?(service |full )?address|"
                         r"(street )?number (comes|is)|what street|zip ?code|full address|complete address|spell (that|the street)|"
                         r"i (still )?(need|don'?t have) (that |the |your )?(full |complete )?(street )?(number|zip|address)", re.I)


def rule_c1_caller_repeated_themselves(c: _Call) -> list[Flag]:
    hits = [u for u in c.users if REPEAT_RE.search(u.text)]
    if not hits:
        return []
    return [Flag("C1_caller_repeated_themselves", "YELLOW", "C", f"{len(hits)}x, e.g. '{_clip(hits[0].text, 90)}'", hits[0].seq)]


_BARE_YES_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|yea|sure|right|mm-?hm+|uh huh)(?:\W+(?:yes|yeah|yep|yup|sure|please|it is|i do|there is))*\W*$", re.I)


def rule_c2_agent_reasked(c: _Call) -> list[Flag]:
    kf = KnownFacts()
    told_at: dict[str, int] = {}
    asked_count: dict[str, int] = {}
    flags: list[Flag] = []
    seen: set[tuple[str, int]] = set()
    after_bare_yes = False  # the caller's last turn was only "yes" / "yeah" (a bare yes to a safety question needs a follow-up like "what kind?")
    for t in c.turns:
        if t.kind == "assistant":
            kf.on_assistant_turn(t.text)
            clarifying, after_bare_yes = after_bare_yes, False
            # only real questions count, never a recap of what the caller said; and never the pending question asked again on purpose right after a take-back
            for topic in ([] if is_assistant_takeback(t.text) else topics_asked(t.text)):
                asked_count[topic] = asked_count.get(topic, 0) + 1
                if topic in told_at and (topic, t.seq) not in seen:
                    seen.add((topic, t.seq))
                    flags.append(Flag("C2_agent_reasked", "YELLOW", "C", f"agent asked about {topic} after the caller had already said it: '{_clip(t.text, 90)}'", t.seq))
                elif asked_count[topic] >= 2 and not clarifying and (topic, t.seq) not in seen:
                    # asking again right after a bare "yes" is a follow-up, not a re-ask (real call 2026-10-05: "Yes, yes" -> "what kind of smells?")
                    seen.add((topic, t.seq))
                    flags.append(Flag("C2_agent_reasked", "YELLOW", "C", f"agent asked about {topic} {asked_count[topic]} times: '{_clip(t.text, 90)}'", t.seq))
        elif t.kind == "user":
            after_bare_yes = bool(_BARE_YES_RE.match(t.text or ""))
            before = len(kf.volunteered)
            kf.on_user_turn(t.text)
            for item in kf.volunteered[before:]:
                told_at.setdefault(item["topic"], t.seq)
    return flags[:3]


def rule_c3_caller_corrected_agent(c: _Call) -> list[Flag]:
    hits = [u for u in c.users if CORRECTION_RE.search(u.text)]
    return [Flag("C3_caller_corrected_agent", "YELLOW", "C", f"'{_clip(hits[0].text, 90)}'", hits[0].seq)] if hits else []


def rule_c4_detail_asked_3_times(c: _Call) -> list[Flag]:
    asks = [a for a in c.assistants if ASK_ADDR_RE.search(a.text)]
    return [Flag("C4_address_asked_3_times", "YELLOW", "C", f"agent asked for the address {len(asks)} times", asks[2].seq)] if len(asks) >= 3 else []


def rule_c5_confused_caller(c: _Call) -> list[Flag]:
    hits = [u for u in c.users if CONFUSED_RE.search(u.text)]
    return [Flag("C5_caller_did_not_understand", "YELLOW", "C", f"'{_clip(hits[0].text, 90)}'", hits[0].seq)] if hits else []


def rule_c6_temperature_heard_differently(c: _Call) -> list[Flag]:
    """The transcript of the caller and the agent's own spoken number disagree (added 2026-10-13, real call: "eighty sea degrees" vs "eighty-six").
    The agent's number is the one recorded; this flag only makes the disagreement visible, and says so when the two sit on different sides of the
    90-degree emergency line, because that is the one case where a wrong number can mean a wrong fee."""
    if not c.temp_mismatch:
        return []
    t, s = c.temp_text.value, c.temp_spoken.value
    note = f"caller's words read as {t} ('{_clip(c.temp_text.snippet, 45)}'), agent said back {s} ('{_clip(c.temp_spoken.snippet, 45)}'); agent's number used"
    if (t >= 90) != (s >= 90):
        note += " - DIFFERENT SIDES OF 90, check the recording"
    return [Flag("C6_temperature_heard_differently", "YELLOW", "C", note)]


REVERSAL_RE = re.compile(r"\bnever ?mind\b|\bactually,? wait\b|\bforget (that|i said)\b|\bchanged my mind\b|\bgo ahead\b|\blet'?s (continue|go on|keep going)\b|\bscratch that\b|\bi didn'?t mean\b", re.I)
KEPT_TALKING_SECS = 2.0


def rule_c7_optout_then_kept_talking(c: _Call) -> list[Flag]:
    """The caller opted out and then tried to take it back, or kept talking (added 2026-10-14, real Call 7). The opt-out stands and the call ends
    about 3 seconds after the agent's goodbye. Since 2026-10-15 a caller CAN take it back (the agent calls grant_opt_in_desertcomfort and the call carries on, which is not
    flagged); this flag now means the take-back or the extra talk was NOT recorded, so every such caller stays visible so we can decide in
    production whether the opt-out needs a way back. The call-end transcript often misses the reversal (the last turn never finishes
    transcribing), so the agent also passes how long the caller kept speaking after the opt-out."""
    if c.opt_out_restored:  # 2026-10-15: a take-back the agent recorded is a handled case, not a finding
        return []
    first = next((u for u in c.users if OPTOUT_RE.search(u.text)), None)
    if first is None:
        return []
    said = next((u for u in c.users if u.seq >= first.seq and REVERSAL_RE.search(u.text)), None)
    if said is not None:
        return [Flag("C7_optout_then_kept_talking", "YELLOW", "C", f"opted out, then the caller said '{_clip(said.text, 90)}'", said.seq)]
    secs = c.spoke_after_optout_secs
    if secs and secs >= KEPT_TALKING_SECS:
        return [Flag("C7_optout_then_kept_talking", "YELLOW", "C", f"opted out, then the caller kept talking for {secs:.1f} s (the transcript never caught it) and no take-back was recorded")]
    return []


# ---------------------------------------------------------------------------
# Group D - silence and lag (YELLOW during learning mode)
# ---------------------------------------------------------------------------

SILENCE_RE = re.compile(r"^\W*(hello( hello)?|are you there|can you hear me|what'?s going on|hello\?)\W*$", re.I)
STALL_RE = re.compile(r"still (on it|confirming|checking|working)|hang on|one more moment|just about there|bear with me", re.I)


def rule_d1_caller_said_hello(c: _Call) -> list[Flag]:
    hits = [u for u in c.users if SILENCE_RE.match(u.text.strip())]
    return [Flag("D1_caller_said_hello", "YELLOW", "D", f"caller said 'hello' / 'are you there' {len(hits)} times (the agent went quiet)", hits[1].seq)] if len(hits) >= 2 else []


def rule_d2_agent_stalled(c: _Call) -> list[Flag]:
    hits = [a for a in c.assistants if STALL_RE.search(a.text)]
    return [Flag("D2_agent_stalled", "YELLOW", "D", f"agent used a stalling line {len(hits)} times, e.g. '{_clip(hits[0].text, 70)}'", hits[1].seq)] if len(hits) >= 2 else []


# The agent says it is checking the schedule. Real call 2026-10-25 (Step 3 Call 7a redo): after a plain "Yes" it said "Okay, checking that now. Uh-huh." and the
# schedule check reached the system 24 s later (31 s after the "Yes"), only after the caller confirmed a third time; nothing else in the check-up saw it.
CHECKING_RE = re.compile(r"\bchecking (?:that|it)(?: now)?\b|\bchecking (?:\w+['\u2019]?s? )?availability\b|\bpull(?:ing)? up the (?:schedule|calendar)\b"
                         r"|\b(?:check|checking|look(?:ing)? at) the (?:schedule|calendar)\b", re.I)
STALL_BEFORE_TOOL_SECONDS = 10.0


def rule_d4_said_checking_but_nothing_ran(c: _Call) -> list[Flag]:
    """A "checking" line (after the fee was quoted) whose schedule check came more than 10 s later. A schedule check logged BEFORE the line is fine: the voice line is
    committed after the tool started. Only the late check is judged: a call where no check ever ran is caught by A2 / D1 / E2, and the old text-only real-call fixtures
    carry no tool calls at all."""
    if not c.quote_reached:
        return []
    checks = [t for t in c.tool_calls if t.name == AVAILABILITY_TOOL]
    handled = -1
    for claim in c.assistants:
        if claim.seq <= handled or not CHECKING_RE.search(claim.text):
            continue
        nxt = next((t for t in checks if t.seq > claim.seq), None)
        if nxt is None:  # no later schedule check: it belongs to an earlier one, or none ran at all (other rules cover that: A2, D1, E2)
            continue
        handled = nxt.seq
        if claim.t is not None and nxt.t is not None and nxt.t - claim.t > STALL_BEFORE_TOOL_SECONDS:
            return [Flag("D4_said_checking_but_nothing_ran", "YELLOW", "D", f"agent said '{_clip(claim.text, 70)}' and the schedule check ran {nxt.t - claim.t:.0f}s later", claim.seq)]
    return []


def rule_d3_session_errors(c: _Call) -> list[Flag]:
    return [Flag("D3_session_errors", "YELLOW", "D", f"{len(c.errors)} error event(s): {c.errors[0]}")] if c.errors else []


# ---------------------------------------------------------------------------
# Group E - business signals (YELLOW, tracked)
# ---------------------------------------------------------------------------

PRICE_OBJECTION_RE = re.compile(r"too (much|expensive|high)|expensive|steep|cheaper|another quote|shop around|can'?t afford|that'?s a lot|think about it", re.I)


def rule_e1_walked_away_after_quote(c: _Call) -> list[Flag]:
    if not c.quote_reached or c.booking_ok or c.outcome in ("transferred", "out_of_area", "do_not_call"):
        return []
    last_user = c.users[-1].text if c.users else ""
    objection = next((u for u in reversed(c.users) if PRICE_OBJECTION_RE.search(u.text)), None)
    first_quote = next(a for a in c.assistants if QUOTE_RE.search(a.text))
    accepted = any(ACCEPT_RE.search(u.text) for u in c.users if u.seq > first_quote.seq)
    if accepted and not objection:
        return []  # said yes to the price and still did not book: that is a broken booking (group A), not a walk-away
    note = f"after the {c.quote_tier or 'fee'} quote the caller did not book; last words: '{_clip(last_user, 80)}'"
    if objection:
        note += f"; price objection: '{_clip(objection.text, 70)}'"
    return [Flag("E1_walked_away_after_quote", "YELLOW", "E", note)]


def rule_e2_long_call_no_outcome(c: _Call) -> list[Flag]:
    if c.outcome != "no_meaningful_conversation" or len(c.users) < 6:
        return []
    return [Flag("E2_long_call_no_outcome", "YELLOW", "E", f"{len(c.users)} caller turns but the outcome is no_meaningful_conversation")]


RULES = [rule_a1_said_booked_not_booked, rule_a2_agreed_never_booked, rule_a3_tool_failed_or_slow, rule_a4_booked_recorded_wrong,
         rule_b1_wrong_tier_for_temperature, rule_b2_emergency_missed, rule_b3_address_made_up, rule_b4_optout_not_recorded, rule_b5_qualifying_question_skipped,
         rule_c1_caller_repeated_themselves, rule_c2_agent_reasked, rule_c3_caller_corrected_agent, rule_c4_detail_asked_3_times, rule_c5_confused_caller,
         rule_c6_temperature_heard_differently, rule_c7_optout_then_kept_talking, rule_d1_caller_said_hello, rule_d2_agent_stalled, rule_d3_session_errors, rule_d4_said_checking_but_nothing_ran,
         rule_e1_walked_away_after_quote, rule_e2_long_call_no_outcome]


def run_checkup(turns: list[Turn], *, outcome: str, errors=(), known_addresses=(), spoke_after_optout_secs=None, opt_out_restored=False) -> dict:
    """Runs every rule. One rule failing never stops the others; the failure is itself recorded (so a broken rule is visible, not silent)."""
    c = _Call(turns, outcome, list(errors), known_addresses, spoke_after_optout_secs, opt_out_restored)
    flags: list[Flag] = []
    broken: list[str] = []
    for rule in RULES:
        try:
            flags += rule(c)
        except Exception as e:  # a bug in one rule must not hide the others
            broken.append(f"{rule.__name__}: {type(e).__name__}")
    red = [f for f in flags if f.severity == "RED"]
    return {
        "version": CHECKUP_VERSION,
        "flags": [f.to_dict() for f in flags],
        "has_red": bool(red),
        "red_rules": sorted({f.rule for f in red}),
        "yellow_rules": sorted({f.rule for f in flags if f.severity == "YELLOW"}),
        "quote_reached": c.quote_reached,
        "quote_tier": c.quote_tier,
        "booking_ok": c.booking_ok,
        "caller_temperature": c.temp.value if c.temp else None,
        "broken_rules": broken,
    }


def run_from_session(report, *, outcome: str, known_addresses=(), spoke_after_optout_secs=None, opt_out_restored=False) -> dict:
    """The one call the agent makes. `report` is livekit's SessionReport."""
    error_events = [e for e in (getattr(report, "events", None) or []) if getattr(e, "type", None) == "error"]
    turns, errors = normalize(getattr(getattr(report, "chat_history", None), "items", None), error_events)
    return run_checkup(turns, outcome=outcome, errors=errors, known_addresses=known_addresses, spoke_after_optout_secs=spoke_after_optout_secs,
                       opt_out_restored=opt_out_restored)


# ---------------------------------------------------------------------------
# Small helpers the agent's outcome ladder uses (same definitions as the rules, so the two can never disagree)
# ---------------------------------------------------------------------------


def booking_ok_in(chat_items) -> bool:
    turns, _ = normalize(chat_items)
    return any(booking_succeeded(t) for t in turns if t.kind == "tool_result")


def successful_booking_args(chat_items) -> dict | None:
    """The arguments of the LAST booking call that really succeeded, or None if no booking succeeded."""
    turns, _ = normalize(chat_items)
    ok = next((t for t in reversed(turns) if t.kind == "tool_result" and booking_succeeded(t)), None)
    if ok is None:
        return None
    call = next((t for t in turns if t.kind == "tool_call" and t.call_id == ok.call_id), None)
    return call.args if call else {}


def quote_reached_in(chat_items) -> bool:
    turns, _ = normalize(chat_items)
    return any(QUOTE_RE.search(t.text) for t in turns if t.kind == "assistant")


def optout_in(chat_items) -> bool:
    turns, _ = normalize(chat_items)
    return any(OPTOUT_RE.search(t.text) for t in turns if t.kind == "user")
