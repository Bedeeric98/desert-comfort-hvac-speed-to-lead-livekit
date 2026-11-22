"""Admin messages for the Desert Comfort LiveKit agent: the end-of-call report (Slack + email) and the immediate call check-up alert.

Built 2026-10-04 at Admin's request ("can't all these be done on LiveKit and the correct, formatted details be sent?"). The agent builds the
FINISHED texts and sends them in the call-end report; the n8n Slack and Gmail nodes only print them (with a plain fallback if a text is
missing). Pure Python, no network, no AI call, never raises into the report path (callers wrap it anyway).

FORMAT = the live "Desert Comfort Telnyx" canvas (Admin: "that is how I want it"): Slack = emoji + bold title, blank line, then
*Phone:* / *Outcome:* / *Summary:* / *Action Required:*; email = one intro sentence, blank line, Name / Phone / Summary / Recording /
Action Required. The wording per outcome is copied from that canvas; the places where this build differs are noted in the table below.

The rule wording (RULES) and the "what the correct result should be" logic are ALSO used by the daily digest, which stays in n8n
(checkup_digest.js). Keep the two copies identical.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import call_checkup as cc

# Spoken numbers in an address become digits ("two hundred West ..." -> "200 West ...") so the Slack text, the email and the summary match
# what was saved. Guarded: if address_digits.py is missing, the address is used as it was.
try:
    from address_digits import normalize_spoken_address as _digits
except Exception:  # pragma: no cover - only reachable on a bad deploy

    def _digits(text):  # type: ignore[misc]
        return text

PHOENIX = timezone(timedelta(hours=-7))  # Arizona does not observe daylight saving time, so a fixed offset is exact (and needs no tz database)

# ---------------------------------------------------------------------------
# Shared words (identical to RULES / OUTCOMES in checkup_digest.js)
# ---------------------------------------------------------------------------
RULES: dict[str, dict[str, str]] = {
    "A1_said_booked_not_booked": {"label": "Agent told the caller they were booked, but no booking was made", "todo": "Check the GHL calendar. If there is no appointment, call the customer and book them."},
    "A2_agreed_never_booked": {"label": "Caller agreed to a time, but no booking was made", "todo": "Check the GHL calendar. If there is no appointment, call the customer and book them."},
    "A2_accepted_then_retracted": {"label": "Caller agreed to a time, then took it back"},
    "A3_tool_failed": {"label": "A tool failed and never recovered", "todo": "Open the n8n execution and the agent log for this call to see which step failed."},
    "A3_tool_failed_then_recovered": {"label": "A tool failed once, then worked"},
    "A3_tool_slow": {"label": "A tool was slow to answer"},
    "A4_booked_but_recorded_wrong": {"label": "The booking and the recorded call result do not match", "todo": "Check the contact stage and the appointment in GHL and correct whichever is wrong."},
    "B1_wrong_tier_for_temperature": {"label": "The caller was given the wrong fee tier for the temperature they gave", "todo": "Listen to the recording. The customer may have been quoted the wrong fee."},
    "B2_emergency_missed": {"label": "The caller described an emergency but was treated as standard", "todo": "Listen to the recording and call the customer if they still need urgent help."},
    "B3_address_made_up": {"label": "The booked address does not match anything the caller said", "todo": "Confirm the address with the customer before the technician goes out."},
    "B4_optout_not_recorded": {"label": "The caller asked not to be contacted, but it was not recorded", "todo": "Mark this contact as do-not-contact in GHL now."},
    "B5_qualifying_question_skipped": {"label": "The fee was quoted before every qualifying question was asked", "todo": "Listen to the recording. A skipped question, like the temperature, can mean the customer was quoted the wrong fee."},
    "C1_caller_repeated_themselves": {"label": "Caller had to repeat themselves"},
    "C2_agent_reasked": {"label": "Agent asked something the caller had already said"},
    "C3_caller_corrected_agent": {"label": "Caller corrected the agent"},
    "C4_address_asked_3_times": {"label": "Agent asked for the address three or more times"},
    "C5_caller_did_not_understand": {"label": "Caller did not understand the agent"},
    "C6_temperature_heard_differently": {"label": "The transcript and the agent heard different temperatures"},
    "C7_optout_then_kept_talking": {"label": "Caller opted out, then tried to take it back or kept talking, and the take-back was not recorded"},
    "D1_caller_said_hello": {"label": 'Caller said "hello" or "are you there" (the agent went quiet)'},
    "D2_agent_stalled": {"label": "Agent used a stalling line more than once"},
    "D3_session_errors": {"label": "The call session logged errors"},
    "D4_said_checking_but_nothing_ran": {"label": "Agent said it was checking the schedule, but the check only ran much later"},
    "E1_walked_away_after_quote": {"label": "Caller walked away after hearing the fee"},
    "E2_long_call_no_outcome": {"label": "Long call that ended with no outcome"},
    "CHECKUP_ERROR": {"label": "The automatic check-up itself failed on this call", "todo": "Review this call by hand. The automatic check did not run."},
}

OUTCOME_LABELS: dict[str, str] = {
    "booked_standard": "Booked (standard)", "booked_emergency": "Booked (emergency)", "qualified_not_booked": "Qualified, but not booked",
    "transferred": "Transferred to a person", "out_of_area": "Outside the service area", "do_not_call": "Asked not to be contacted",
    "wrong_person": "Wrong person", "no_meaningful_conversation": "No meaningful conversation", "dead_air": "Dead air (no caller speech)",
}


def rule_label(rule_id: str) -> str:
    entry = RULES.get(rule_id)
    if entry:
        return entry["label"]
    tidy = re.sub(r"_", " ", re.sub(r"^[A-E]\d+_?", "", str(rule_id or ""))).strip()
    return tidy or str(rule_id or "unknown rule")


def rule_todo(rule_id: str) -> str:
    return (RULES.get(rule_id) or {}).get("todo", "")


def outcome_label(outcome: str) -> str:
    return OUTCOME_LABELS.get(outcome) or (str(outcome).replace("_", " ") if outcome else "unknown")


def clip(s, n: int) -> str:
    """Shorten at a word boundary, never mid-word."""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    sp = cut.rfind(" ")
    return re.sub(r"[\s,;:.]+$", "", cut[:sp] if sp > n * 0.6 else cut) + "..."


def _text(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _slack_escape(s) -> str:
    """In Slack & < > start links and mentions; callers' and the model's own words must not be able to make one."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------
def _hm(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def phoenix_short(dt: datetime) -> str:
    """Fri, Oct 2 at 8:10 AM (Phoenix time)"""
    d = dt.astimezone(PHOENIX)
    return f"{d:%a}, {d:%b} {d.day} at {_hm(d)} (Phoenix time)"


def phoenix_long(raw) -> str:
    """An ISO appointment time -> 'Saturday, October 3 at 10:00 AM' in Phoenix time. A string that does not parse is returned as written."""
    s = _text(raw)
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    d = dt.replace(tzinfo=PHOENIX) if dt.tzinfo is None else dt.astimezone(PHOENIX)
    return f"{d:%A}, {d:%B} {d.day} at {_hm(d)}"


# ---------------------------------------------------------------------------
# "What the correct result should be"
# ---------------------------------------------------------------------------
def correct_result(*, outcome="", quote_tier="", caller_temperature=None, flags=(), severity="RED", red_rules=()) -> str:
    serious = [f for f in (flags or []) if isinstance(f, dict) and f.get("severity") in ("RED", "ERROR")]
    ids = [str(f.get("rule", "")) for f in serious] if serious else [str(x) for x in (red_rules or [])]

    def has(prefix: str) -> bool:
        return any(i.startswith(prefix) for i in ids)

    def evidence(prefix: str) -> str:
        f = next((x for x in serious if str(x.get("rule", "")).startswith(prefix)), None)
        return str(f.get("evidence", "")) if f else ""

    try:
        temp = float(caller_temperature)
    except (TypeError, ValueError):
        temp = None
    was_booked = outcome in ("booked_standard", "booked_emergency")
    tier = "emergency" if outcome == "booked_emergency" else "standard" if outcome == "booked_standard" else (quote_tier if quote_tier in ("standard", "emergency") else "")

    def booked(t: str) -> str:
        return f"Booked ({t})" if t else "Booked"

    if severity == "ERROR" or has("CHECKUP_ERROR"):
        return "Unknown - the automatic check did not run"
    if has("B4_"):
        return "Asked not to be contacted - this contact should be marked do-not-contact"
    if has("A4_"):
        return f"{booked(tier)} - a booking did succeed" if re.match(r"a booking succeeded", evidence("A4_"), re.I) else "Qualified, but not booked - no booking actually succeeded"
    if has("B1_") or has("B2_"):
        want = "emergency" if has("B2_") else (("emergency" if temp >= 90 else "standard") if temp is not None else "")
        base = "Booked" if was_booked else "Qualified, but not booked"
        return f"{base}{f' ({want})' if want else ''} - {f'handled at the {want} tier' if want else 'handled at the right fee tier'}"
    if has("A1_") or has("A2_"):
        return f"{booked(tier)} - the customer was told a time was set, so an appointment should exist"
    if "A3_tool_failed" in ids:
        if re.search(r"book_appointment|check_availability", evidence("A3_tool_failed")):
            return f"{booked(tier)} - the tool that books the customer failed"
        return f"Same as recorded ({outcome_label(outcome)}) - the failed step did not change the result"
    if has("B3_"):
        return "Booked, with the address the customer actually gave"
    return "Not certain - listen to the call"


# ---------------------------------------------------------------------------
# End-of-call report (copy of the Desert Comfort Telnyx canvas wording)
# ---------------------------------------------------------------------------
_TRANSFER_ACTION = "Confirm the transfer was picked up. This build has no transfer handler yet, so confirm the caller was actually reached."
REPORT_OUTCOMES: dict[str, dict] = {
    "booked_emergency": {
        "emoji": "\U0001F6A8", "title": "Emergency Booked", "subject_title": "Emergency Booked", "with_name": True,
        "outcome": "Emergency criteria confirmed, appointment booked within the 90-minute window",
        "intro": "An emergency call was booked.",
        "action": "None — appointment is on the calendar. FYI only.", "email_action": "None — appointment is on the calendar. FYI only.",
    },
    "booked_standard": {
        "emoji": "✅", "title": "Standard Booked", "subject_title": "Standard Booked", "with_name": True,
        "outcome": "Standard appointment booked within the 2-4 hour window",
        "outcome_night": "Standard appointment booked for the soonest available time",  # the 2-4 hour window is a day-only promise
        "intro": "A standard appointment was booked.",
        "action": "None — appointment is on the calendar. FYI only.", "email_action": "None — appointment is on the calendar. FYI only.",
    },
    "qualified_not_booked": {  # Telnyx says "accepted pricing"; here a caller who hears the fee and leaves also lands in this outcome
        "emoji": "⚠️", "title": "Qualified But Not Booked", "subject_title": "Qualified Not Booked", "with_name": True,
        "outcome": "Caller was qualified but the booking did not complete — needs manual follow-up",
        "intro": "A caller was qualified but the booking did not complete.",
        "action": "Call back to complete the booking.", "email_action": "Call back to complete the booking.",
    },
    "transferred": {  # this build has no backend handler for the transfer yet (known, deferred), so the old warning stays
        "emoji": "\U0001F4DE", "title": "Transferred to Human", "subject_title": "Transferred", "with_name": True,
        "outcome": "Call was routed to a human (commercial property, billing dispute, or out-of-scope request)",
        "intro": "A call was transferred to a human.",
        "action": _TRANSFER_ACTION, "email_action": _TRANSFER_ACTION,
    },
    "out_of_area": {
        "emoji": "\U0001F4CD", "title": "Out Of Service Area", "subject_title": "Out Of Area", "with_name": True,
        "outcome": "Caller's address is outside the 60-mile service radius — declined",
        "intro": "A caller was outside the service radius and declined.",
        "action": "None. FYI only.", "email_action": "None. FYI only.",
    },
    "no_meaningful_conversation": {
        "emoji": "ℹ️", "title": "No Meaningful Conversation", "subject_title": "No Meaningful Conversation", "with_name": False,
        "outcome": "Call ended without a usable conversation (dropped, silence, or unparseable)",
        "intro": "A call ended without a usable conversation.",
        "action": "Review if this recurs — may indicate a call-quality issue.", "email_action": "Review if this recurs — may indicate a call-quality issue.",
    },
    "do_not_call": {
        "emoji": "\U0001F6AB", "title": "Do Not Call Requested", "subject_title": "Do Not Call", "with_name": True,
        "outcome": "Caller explicitly asked not to be contacted again",
        "intro": "A caller explicitly requested no further contact.",
        "action": "Confirm the contact is flagged do-not-call in GHL. Compliance-sensitive — do not call back.",
        "email_action": "Confirm the contact is flagged do-not-call in GHL. Compliance-sensitive.",
    },
}


def facts_summary(outcome: str, *, name: str = "", details: dict | None = None, quote_tier: str = "") -> str:
    """ONE plain sentence built only from facts we have (the Summary when no written summary is available)."""
    d = details or {}
    who = name or "The caller"
    appt = phoenix_long(d.get("appointment_time"))
    address = _text(d.get("address"))
    tier = quote_tier if quote_tier in ("standard", "emergency") else ""
    if outcome == "booked_emergency":
        return f"{who} booked an emergency appointment{f' for {appt}' if appt else ''}{f' at {address}' if address else ''}."
    if outcome == "booked_standard":
        return f"{who} booked a standard appointment{f' for {appt}' if appt else ''}{f' at {address}' if address else ''}."
    if outcome == "qualified_not_booked":
        return f"{who} was qualified{f' and quoted the {tier} fee' if tier else ''}{', and the schedule was checked' if d.get('availability_checked') is True else ''}, but the call ended before a time was confirmed and booked."
    if outcome == "transferred":
        reason = _text(d.get("transfer_reason"))
        return f"{who} was transferred to a person{f' (reason: {reason})' if reason else ''}."
    if outcome == "out_of_area":
        owner = f"{name}'s" if name else "The caller's"
        return f"{owner} address is outside the 60-mile service radius{f' ({address})' if address else ''}."
    if outcome == "no_meaningful_conversation":
        return f"{who} connected, but no useful conversation took place."
    if outcome == "do_not_call":
        return f"{who} asked not to be contacted again."
    return f'The call ended with the result "{outcome or "unknown"}".'


def outcome_sentence(meta: dict, hours_mode: str = "day") -> str:
    """The report's Outcome line. After hours a booked standard appointment is the soonest available time, not the day 2-4 hour window."""
    return meta["outcome_night"] if hours_mode == "night" and meta.get("outcome_night") else meta["outcome"]


def build_report(*, outcome: str, phone: str, name: str = "", details: dict | None = None, quote_tier: str = "", call_summary: str = "",
                 recording_url: str = "", hours_mode: str = "day") -> dict | None:
    """The Slack text, email subject and email body for one finished call, or None for an outcome with no report here (dead air has its own)."""
    meta = REPORT_OUTCOMES.get(outcome)
    if meta is None:
        return None
    phone = _text(phone) or "unknown"
    name = _text(name)
    summary = _text(call_summary) or facts_summary(outcome, name=name, details=details, quote_tier=quote_tier)
    slack = (f"{meta['emoji']} *LiveKit Call — {meta['title']}*\n\n*Phone:* {_slack_escape(phone)}\n*Outcome:* {outcome_sentence(meta, hours_mode)}\n"
             f"*Summary:* {_slack_escape(summary)}\n*Action Required:* {meta['action']}")
    lines = [meta["intro"], ""]
    if meta["with_name"]:
        lines.append(f"Name: {name or 'Not captured'}")
    lines += [f"Phone: {phone}", f"Summary: {summary}"]
    if _text(recording_url):
        lines.append(f"Recording: {_text(recording_url)}")
    lines.append(f"Action Required: {meta['email_action']}")
    return {
        "slack_text": slack,
        "email_subject": f"LiveKit Call — {meta['subject_title']} — {name if (meta['with_name'] and name) else phone}",
        "email_text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# Immediate call check-up alert (RED or check-up failed)
# ---------------------------------------------------------------------------
def build_alert(*, phone: str, job_id: str, room: str, outcome: str, checkup: dict, now: datetime | None = None) -> dict | None:
    """The alert for a RED or failed check-up, or None when there is nothing to alert (yellow-only and clean calls)."""
    cu = checkup if isinstance(checkup, dict) else {}
    has_red = cu.get("has_red") is True
    error = cu.get("error")
    if not has_red and not error:
        return None
    severity = "RED" if has_red else "ERROR"
    flags = [{"rule": "CHECKUP_ERROR", "severity": "ERROR", "evidence": str(error)}] if error else (cu.get("flags") if isinstance(cu.get("flags"), list) else [])
    flags = [f for f in flags if isinstance(f, dict)]
    phone = _text(phone)
    who = phone or f"caller ending {phone[-4:] or '????'}"
    serious = [f for f in flags if f.get("severity") in ("RED", "ERROR")]
    minor = [f for f in flags if f.get("severity") == "YELLOW"]
    failed = severity == "ERROR"
    when = phoenix_short(now or datetime.now(timezone.utc))
    quote_tier = cu.get("quote_tier") or ""
    temp = cu.get("caller_temperature")
    correct = correct_result(outcome=outcome, quote_tier=quote_tier, caller_temperature=temp, flags=flags, severity=severity, red_rules=cu.get("red_rules") or [])

    def build(slack: bool) -> str:
        label = (lambda s: f"*{s}:*") if slack else (lambda s: f"{s}:")
        bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
        esc = _slack_escape if slack else str
        dot = "•" if slack else "-"
        out: list[str] = []
        if slack:
            out += ["⚠️ *LiveKit Call Check-Up — The Check-Up Itself Failed*" if failed else "\U0001F534 *LiveKit Call Check-Up — Needs a Look*", ""]
        else:
            out += ["The automatic call check-up failed on this call." if failed else "The call check-up found something that needs a human look.", ""]
        out += [f"{label('Phone')} {phone or who.replace('caller ', '')}", f"{label('When')} {when}",
                f"{label('Call result')} {outcome_label(outcome)}", f"{label('What the correct result should be')} {correct}"]
        facts = []
        if temp not in (None, ""):
            facts.append(f"caller said about {temp} degrees")
        if quote_tier:
            facts.append(f"{quote_tier} fee quoted")
        if facts:
            out.append(f"{label('On the call')} {', '.join(facts)}")
        out += ["", label("What went wrong" if failed else "What the check-up found")]
        if serious:
            todos: list[str] = []
            for f in serious:
                out += ["", f"{dot} {bold(rule_label(f.get('rule', '')))}"]
                if f.get("evidence"):
                    out.append(f"  Detail: {esc(clip(f['evidence'], 240))}")
                todo = rule_todo(f.get("rule", ""))
                if todo and todo not in todos:
                    todos.append(todo)
            if todos:
                out += ["", f"{label('Action Required')} {' '.join(todos)}"]
        else:
            ids = [str(x) for x in (cu.get("red_rules") or []) if x]
            out += ["", f"{dot} {'; '.join(rule_label(i) for i in ids) if ids else f'Flagged {severity}'}"]
        if minor:
            out += ["", f"{bold('Also noted')} (minor, in tomorrow's digest):"]
            out += [f"{dot} {rule_label(f.get('rule', ''))}" for f in minor]
        out += ["", f"Reference: call {job_id or 'unknown'}{f', room {room}' if room else ''}"]
        return "\n".join(out)

    first = serious[0] if serious else None
    why = clip(rule_label(first.get("rule", "")), 80) if first else outcome_label(outcome)
    return {
        "slack_text": build(True),
        "email_subject": f"LiveKit Call Check-Up — {why} — {who}",
        "email_text": build(False),
    }


# ---------------------------------------------------------------------------
# Red-flag alert (the transcript scanner: prompt-injection attempts and promises the agent must not make)
# ---------------------------------------------------------------------------
RED_FLAG_LABELS: dict[str, str] = {
    "1A_instruction_override": "Caller tried to override the agent's instructions",
    "1B_named_jailbreak": "Caller used a known jailbreak phrase",
    "1C_prompt_extraction": "Caller tried to get the agent to reveal its instructions",
    "1D_roleplay_override": "Caller tried to make the agent play a different role",
    "response_window_pressure": "Caller pushed the agent to promise a faster arrival time",
    "pricing_pressure": "Caller pushed for a repair price or diagnosis",
    "compliance_violation": "The agent made a promise it should not make (a guaranteed time or a repair price)",
    "emergency_cutoff_disclosed": "The agent told the caller the temperature that counts as an emergency",
    "emergency_judgment_spoken": "The agent rated the caller's situation (for example, told them it is not an emergency)",
}
RED_FLAG_CONFIDENCE: dict[str, str] = {
    "HIGH": "High (two or more kinds of attack signals, or the agent made a promise it should not)",
    "MEDIUM": "Medium (one kind of attack signal, or repeated pressure from the caller)",
    "LOW": "Low (a weak signal)",
}
RED_FLAG_ACTION = "Review the transcript for a possible prompt-injection attempt or a promise the agent should not have made."


def build_red_flag_alert(*, phone: str, job_id: str, room: str, scan: dict, transcript_text: str = "") -> dict | None:
    """The alert for a call the transcript scanner flagged, or None when it flagged nothing. Replaces the raw-JSON alert."""
    s = scan if isinstance(scan, dict) else {}
    if not (s.get("has_security_flag") or s.get("has_commitment_flag")):
        return None
    flags = [f for f in (list(s.get("security_flags") or []) + list(s.get("commitment_flags") or [])) if isinstance(f, dict)]
    grouped: dict[str, list[str]] = {}
    for f in flags:  # one block per kind of problem, the same sentence never listed twice
        quotes = grouped.setdefault(str(f.get("category", "")), [])
        context = clip(f.get("matched_context", ""), 220)
        if context and context not in quotes:
            quotes.append(context)
    phone = _text(phone) or "unknown"
    confidence = RED_FLAG_CONFIDENCE.get(str(s.get("confidence", "")).upper(), _text(s.get("confidence")) or "unknown")
    ref = f"Reference: call {job_id or 'unknown'}{f', room {room}' if room else ''}"

    def build(slack: bool) -> str:
        bold = (lambda t: f"*{t}*") if slack else (lambda t: t)
        label = (lambda t: f"*{t}:*") if slack else (lambda t: f"{t}:")
        esc = _slack_escape if slack else str
        dot = "•" if slack else "-"
        out = ["\U0001F6A9 *LiveKit Call — Red Flags Detected*" if slack else "The transcript scanner flagged this call for review.", "",
               f"{label('Phone')} {esc(phone)}", f"{label('Confidence')} {confidence}", "", label("What the scan found")]
        for category, quotes in grouped.items():
            out += ["", f"{dot} {bold(RED_FLAG_LABELS.get(category) or rule_label(category))}"]
            who = "The agent" if category in ("compliance_violation", "emergency_cutoff_disclosed", "emergency_judgment_spoken") else "The caller"
            out += [f"  {who} said: \"{esc(q)}\"" for q in quotes[:3]]
            if len(quotes) > 3:
                out.append(f"  (and {len(quotes) - 3} more)")
        out += ["", f"{label('Action Required')} {RED_FLAG_ACTION}", "", ref]
        if not slack:
            out += ["", "Transcript:", (transcript_text or "")[:8000] + ("\n(cut here)" if len(transcript_text or "") > 8000 else "")]
        return "\n".join(out)

    return {"slack_text": build(True), "email_subject": f"LiveKit Call — Red Flags Detected — {phone}", "email_text": build(False)}


# ---------------------------------------------------------------------------
# Facts about the call, read from the session record (tool calls the agent really made)
# ---------------------------------------------------------------------------
def collect_details(chat_items, *, known_first_name: str = "", known_last_name: str = "") -> dict:
    """name / address / appointment time / transfer reason / whether the schedule was checked, from the TOOL CALLS in the session record.
    Deterministic: nothing is inferred from the transcript. Only non-empty values are returned."""
    turns, _ = cc.normalize(chat_items)
    out: dict = {}
    booking = cc.successful_booking_args(chat_items)
    if booking:
        out["first_name"] = _text(booking.get("first_name")) or _text(known_first_name)
        out["last_name"] = _text(booking.get("last_name")) or _text(known_last_name)
        out["address"] = _digits(_text(booking.get("address")))
        out["appointment_time"] = _text(booking.get("absolute_time"))
        out["urgent"] = bool(booking.get("urgent"))
    else:
        out["first_name"], out["last_name"] = _text(known_first_name), _text(known_last_name)
        radius = [t for t in turns if t.kind == "tool_call" and t.name == "radius_checker_desertcomfort" and _text(t.args.get("address"))]
        if radius:
            out["address"] = _digits(_text(radius[-1].args.get("address")))
    transfers = [t for t in turns if t.kind == "tool_call" and t.name == "transfer_to_human"]
    if transfers:
        out["transfer_reason"] = _text(transfers[-1].args.get("reason"))
    if any(t.kind == "tool_call" and t.name == cc.AVAILABILITY_TOOL for t in turns):
        out["availability_checked"] = True
    return {k: (v[:200] if isinstance(v, str) else v) for k, v in out.items() if v not in ("", None)}


def full_name(details: dict) -> str:
    return " ".join(p for p in (_text(details.get("first_name")), _text(details.get("last_name"))) if p)


def merge_known_facts(details: dict, captured: dict) -> dict:
    """Fills first_name / last_name / address from known_facts.py's deterministic transcript capture (regex over the live
    transcript, not an LLM guess) - ONLY when collect_details() found nothing for that field. A tool call always wins when it ran;
    this is the backstop for a caller who gave their name/address and then hung up before check_availability/radius_checker. Same
    text-cleaning and 200-char cap as collect_details() itself, so a caller downstream can't tell which path a value came from."""
    for key in ("first_name", "last_name", "address"):
        if not details.get(key) and captured.get(key):
            value = _digits(_text(captured[key])) if key == "address" else _text(captured[key])
            if value:
                details[key] = value[:200]
    return details


# A model-extracted name is display text, never a verified fact (see parse_name_and_summary) - same shape/placeholder
# sanity as desert_comfort_agent.py's clean_person_name(), kept separate to avoid importing that module here (it imports
# this one, so the reverse import would be circular).
_STATED_NAME_PLACEHOLDER_RE = re.compile(
    r"^(none|not captured|n/?a|null|nil|undefined|unknown|anonymous|no name|noname|name|first ?name|last ?name|customer|caller|client|guest)$", re.I)
_STATED_NAME_SHAPE_RE = re.compile(r"^[^\W\d_]+(?:[ .'’-]+[^\W\d_]+)*\.?$")


def parse_name_and_summary(raw: str) -> tuple[str, str]:
    """Parses the model's 'NAME: ...\\nSUMMARY: ...' reply. Returns (stated_name, summary) - stated_name is '' unless it
    is NAME-shaped (letters/spaces/hyphens only, 2-4 words, 2-50 chars) and not an obvious placeholder; falls back to
    treating the whole reply as the summary if it isn't in the expected two-line format (older/odd model replies)."""
    m = re.search(r"NAME:\s*(.*?)\s*\n+\s*SUMMARY:\s*(.*)", raw, re.I | re.S)
    if not m:
        return "", " ".join(raw.split())
    name_part, summary_part = m.group(1).strip(), " ".join(m.group(2).split())
    if 2 <= len(name_part) <= 50 and 1 < len(name_part.split()) <= 4 and _STATED_NAME_SHAPE_RE.match(name_part) \
            and not _STATED_NAME_PLACEHOLDER_RE.match(name_part):
        return name_part, summary_part
    return "", summary_part


def build_messages(*, outcome: str, phone: str, job_id: str, room: str, checkup: dict, details: dict, call_summary: str = "",
                   stated_name: str = "", recording_url: str = "", now: datetime | None = None, scan: dict | None = None,
                   transcript_text: str = "", hours_mode: str = "day") -> dict:
    """All the texts for one call (end-of-call report, check-up alert, red-flag alert). Each is built on its own: one failing never takes the others down.
    stated_name is display-only (see parse_name_and_summary) - used only when collect_details() found no verified name."""
    messages: dict = {"report": None, "alert": None, "red_flag": None}
    cu = checkup if isinstance(checkup, dict) else {}
    try:
        messages["report"] = build_report(outcome=outcome, phone=phone, name=full_name(details) or _text(stated_name), details=details,
                                          quote_tier=cu.get("quote_tier") or "", call_summary=call_summary, recording_url=recording_url,
                                          hours_mode=hours_mode)
    except Exception:
        messages["report"] = None
    try:
        messages["alert"] = build_alert(phone=phone, job_id=job_id, room=room, outcome=outcome, checkup=cu, now=now)
    except Exception:
        messages["alert"] = None
    try:
        messages["red_flag"] = build_red_flag_alert(phone=phone, job_id=job_id, room=room, scan=scan or {}, transcript_text=transcript_text)
    except Exception:
        messages["red_flag"] = None
    return messages


# ---------------------------------------------------------------------------
# The written summary: guard against the one failure we have seen before (a summary claiming a booking that did not happen)
# ---------------------------------------------------------------------------
_BOOKING_CLAIM_RE = re.compile(r"\b(has|have|had|was|were|is|are|been|got|get|gets|now)\s+(been\s+)?(booked|scheduled|confirmed)\b|\bappointment (is|was) (set|made|confirmed)\b", re.I)


def summary_is_safe(summary: str, outcome: str) -> bool:
    """A written summary is used only if it is a sensible length and does not claim a booking on a call that did not book."""
    s = _text(summary)
    if not (10 <= len(s) <= 500):
        return False
    if outcome not in ("booked_standard", "booked_emergency") and _BOOKING_CLAIM_RE.search(s):
        return False
    return True
