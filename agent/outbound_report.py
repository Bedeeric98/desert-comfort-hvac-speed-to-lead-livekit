"""Builds the OUTBOUND (speed to lead) end-of-call report payload. Pure Python apart from two injected model calls, so it tests offline.

The payload is the contract for the n8n call-end branch (build step 4). Everything in it is either a fact from the session record or a label from
outbound_outcome.py. Transcript and quotes are included ONLY when the lead clearly agreed to be recorded (recording_consent.py); a lead who declined
or never clearly agreed gets: outcome, details (name, address, appointment), a written summary (no verbatim quotes), the check-up and scan RESULTS with
the quoted evidence removed, and the finished messages. Never the transcript, never a quote.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

import call_checkup as cc
import call_messages as cm
import outbound_messages as om
import outbound_outcome as oo
from recording_consent import RecordingConsent

logger = logging.getLogger("desert-comfort-outbound-agent")

SUMMARY_TIMEOUT_SECONDS = 8


@dataclass
class CallState:
    """What _run_call knows about one call, read at the end of it. Callables so the values are current when the report is built."""
    lead_phone_number: str
    attempt: int
    texted: bool
    hours_mode: str
    first_name: str
    last_name: str
    address_on_file: str
    recording: RecordingConsent
    is_answered: Callable[[], bool] = lambda: False
    is_machine: Callable[[], bool] = lambda: False
    is_silent_line: Callable[[], bool] = lambda: False
    optout_active: Callable[[], bool] = lambda: False
    optout_restored: Callable[[], bool] = lambda: False
    speech_after_optout: list = field(default_factory=lambda: [0.0])
    known_facts: dict = field(default_factory=dict)
    contact_id: str = ""


async def _write_summary(summarize, *, transcript_text: str, outcome: str, reason: str, details: dict, quote_tier: str, attempt: int) -> tuple[str, str]:
    """(stated_name, summary), or ("", "") when unusable. summarize: async (system_prompt, user_text) -> raw model text."""
    if summarize is None or not (transcript_text or "").strip():
        return "", ""
    facts = om.summary_facts(outcome=outcome, reason=reason, details=details, quote_tier=quote_tier, attempt=attempt)
    try:
        raw = await asyncio.wait_for(summarize(om.SUMMARY_SYSTEM_PROMPT, "FACTS:\n- " + "\n- ".join(facts) + "\n\nTRANSCRIPT:\n" + transcript_text[:6000]), timeout=SUMMARY_TIMEOUT_SECONDS)
        stated_name, summary = cm.parse_name_and_summary((raw or "").strip())
        if cm.summary_is_safe(summary, outcome):
            return stated_name, summary
        logger.warning("Call summary discarded (empty, odd length, or it claimed a booking the call did not make)")
    except Exception:
        logger.exception("Call summary failed - the report will use the plain facts sentence")
    return "", ""


def dial_payload(*, room: str, job_id: str, phone: str, contact_id: str, attempt: int, outcome: str, reason: str, sip_status_code: int | None) -> dict:
    """The short report for a call that never connected (no_answer / dial_failed). Nothing was said, so there is nothing to hold back."""
    return {
        "direction": "outbound", "lead_phone_number": phone, "contact_id": contact_id, "room": room, "job_id": job_id, "attempt": attempt,
        "outcome": outcome, "outcome_reason": reason, "sip_status_code": sip_status_code,
        "transcript_text": "", "transcript_allowed": False, "recording_consent": "pending", "call_duration_secs": 0,
    }


async def build_payload(report, state: CallState, *, classify, summarize=None) -> dict | None:
    """The payload, or None when the phone was never answered (the agent's dial already sent dial_payload for that call)."""
    if not state.is_answered():
        return None
    rec = state.recording
    items = getattr(getattr(report, "chat_history", None), "items", None) or []
    out = await oo.derive_outcome(items, machine=state.is_machine(), silent_line=state.is_silent_line(), optout_in_force=state.optout_active(), classify=classify)
    allowed = rec.transcript_allowed()
    turns, _ = cc.normalize(items)
    transcript_text = "\n".join(f"{'Lead' if t.kind == 'user' else 'Assistant'}: {t.text}" for t in turns if t.kind in ("user", "assistant"))
    duration = round(report.timestamp - report.started_at) if getattr(report, "started_at", None) else None

    payload: dict = {
        "direction": "outbound", "lead_phone_number": state.lead_phone_number, "contact_id": state.contact_id, "room": getattr(report, "room", ""), "job_id": getattr(report, "job_id", ""),
        "attempt": state.attempt, "texted": state.texted, "hours_mode": state.hours_mode,
        "outcome": out.outcome, "outcome_reason": out.reason, "flags": out.flags, "emergency_unbooked": False,
        "recording_consent": rec.state, "recording_consent_reason": rec.reason, "transcript_allowed": allowed,
        "transcript_text": transcript_text if allowed else "", "call_duration_secs": duration,
    }
    if out.outcome in oo.UNREACHED_OUTCOMES:
        return payload  # a machine or a silent line: no conversation to summarise, scan or check up on

    scan = om.scan_red_flags(items)
    try:
        checkup = cc.run_from_session(report, outcome=out.outcome, known_addresses=[state.address_on_file] if state.address_on_file else [],
                                      spoke_after_optout_secs=round(state.speech_after_optout[0], 1), opt_out_restored=state.optout_restored())
    except Exception:
        logger.exception("Call check-up failed - the report is still sent without it")
        checkup = {"error": "checkup_failed"}
    details = cm.merge_known_facts(cm.collect_details(items, known_first_name=state.first_name, known_last_name=state.last_name), state.known_facts)
    quote_tier = (checkup.get("quote_tier") if isinstance(checkup, dict) else "") or ""
    payload["emergency_unbooked"] = oo.emergency_unbooked(out.outcome, quote_tier, checkup.get("caller_temperature") if isinstance(checkup, dict) else None)
    stated_name, summary = await _write_summary(summarize, transcript_text=transcript_text, outcome=out.outcome, reason=out.reason, details=details,
                                                quote_tier=quote_tier, attempt=state.attempt)
    if not allowed:  # no clear yes to recording: remove every quote from what is stored and sent
        scan, checkup = om.redact_scan(scan), om.redact_checkup(checkup)
    messages = om.build_messages(outcome=out.outcome, reason=out.reason, flags=out.flags, phone=state.lead_phone_number, job_id=payload["job_id"], room=payload["room"],
                                 checkup=checkup, details=details, call_summary=summary, stated_name=stated_name, scan=scan,
                                 transcript_text=transcript_text if allowed else "", recording_state=rec.state,
                                 emergency_unbooked=payload["emergency_unbooked"], attempt=state.attempt, hours_mode=state.hours_mode)
    payload.update(scan)
    payload.update({"checkup": checkup, "details": details, "call_summary": summary, "messages": messages})
    return payload
