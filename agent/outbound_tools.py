"""Plumbing the Desert Comfort OUTBOUND agent shares in kind with the inbound agent: calls to the n8n webhooks, phone formatting and the
opt-out / opt-in writes with their code backstops. Copied from desert_comfort_agent.py on purpose (importing that module would start the
inbound agent's setup) and kept to what outbound uses. No LiveKit imports.

If a rule changes here (an n8n URL, the opt-out words, the grace windows), the same rule in desert_comfort_agent.py needs the same change.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

import call_checkup
from address_digits import normalize_spoken_address
from closing_ack import ConsentFlow, is_assistant_takeback
from outbound_outcome import CONSENT_DISPUTE_RE

logger = logging.getLogger("desert-comfort-outbound-agent")

load_dotenv()

N8N_BASE_URL = os.getenv("N8N_API_URL", "https://your-n8n-instance.example.com")
INBOUND_LOOKUP_URL = f"{N8N_BASE_URL}/webhook/YOUR_WEBHOOK_PATH"
TOOL_ROUTER_URL = f"{N8N_BASE_URL}/webhook/YOUR_WEBHOOK_PATH"

# Same two secrets and the same signing source as the inbound agent: n8n checks the key header and an HMAC over "<timestamp_ms>.<source>".
HVAC_AGENT_KEY = os.getenv("HVAC_AGENT_KEY", "")
HVAC_SIGNING_SECRET = os.getenv("HVAC_SIGNING_SECRET", "")
WEBHOOK_SOURCE = "desert-comfort-livekit-agent"

# Same grace windows as the inbound agent (a dropped consent tool call is a known GPT-Live-1 failure, so code sends it if the model does not).
OPT_OUT_TOOL_BACKSTOP_GRACE_SECONDS = 3.5
OPT_IN_TOOL_BACKSTOP_GRACE_SECONDS = 3.5
OPT_OUT_TRIGGER_RE = re.compile(
    r"\b(?:don'?t|do not) (?:call|contact) me\b"
    r"|\btake me off (?:your|the|that) list\b"
    r"|\bi don'?t need you any ?more\b"
    r"|\bcancel it,? i don'?t want you guys\b",
    re.IGNORECASE,
)

_OUTBOUND_ROOM_PHONE_RE = re.compile(r"out-_(\d+)_")


def webhook_auth_headers() -> dict[str, str]:
    """Fresh auth headers for ONE request (the timestamp must be current, so never cache these)."""
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(HVAC_SIGNING_SECRET.encode("utf-8"), f"{timestamp}.{WEBHOOK_SOURCE}".encode("utf-8"), hashlib.sha256).hexdigest()
    return {"x-ce-key": HVAC_AGENT_KEY, "x-ce-timestamp": timestamp, "x-ce-signature": signature}


def normalize_phone_number(raw: str) -> str:
    """One E.164 number per lead. A bare 10-digit number sent to GHL without the +1 got the location's non-US default country code (real bug, 2026-09-28)."""
    raw = raw or ""
    digits = "".join(c for c in raw if c.isdigit())
    if raw.startswith("+"):
        return raw
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw


def phone_from_outbound_room(room_name: str) -> str:
    """The lead's number from the room name out-_<digits>_<tag> (set by the n8n dial step), or ''."""
    match = _OUTBOUND_ROOM_PHONE_RE.match(room_name or "")
    return match.group(1) if match else ""


def current_time_formatted() -> str:
    return datetime.now(ZoneInfo("America/Phoenix")).strftime("%A, %B %d, %Y at %I:%M %p %Z")


def unwrap(data: object) -> object:
    """n8n answers with a one-item JSON array on every Return node; the agent wants the item."""
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


async def lookup_lead(phone_number: str) -> dict:
    """The inbound lookup webhook (read only): {"route": "new_caller"} or the contact's fields. Any failure returns new_caller, so a lead
    whose lookup failed is still called, just by a plain 'Hello' opening and nothing assumed about them."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                INBOUND_LOOKUP_URL, json={"phone_number": phone_number}, headers=webhook_auth_headers(), timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                if resp.status in (401, 403):
                    logger.error("Lead lookup REJECTED by n8n (status=%s) - check HVAC_AGENT_KEY / HVAC_SIGNING_SECRET", resp.status)
                    return {"route": "new_caller"}
                data = unwrap(await resp.json())
                if isinstance(data, dict) and "route" in data:
                    return data
                logger.warning("Lead lookup returned no route, got: %r", data)
    except Exception:
        logger.exception("Lead lookup failed for %s - treating the lead as a new caller", phone_number)
    return {"route": "new_caller"}


async def call_tool(tool_name: str, lead_phone_number: str, **fields: object) -> dict:
    """POST to the shared Tool Call Router webhook (same body shape the inbound agent sends). Returns {"error": "request_failed"} on any failure,
    which the model is told how to handle."""
    if isinstance(fields.get("address"), str):
        fixed_address = normalize_spoken_address(fields["address"])
        if fixed_address != fields["address"]:
            logger.info("ADDRESS NORMALISED: %r -> %r", fields["address"], fixed_address)
            fields = {**fields, "address": fixed_address}
    body = {"tool_name": tool_name, "caller_phone_number": lead_phone_number, **fields}
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(TOOL_ROUTER_URL, json=body, headers=webhook_auth_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (401, 403):
                    logger.error("Tool call %s REJECTED by n8n (status=%s) - check HVAC_AGENT_KEY / HVAC_SIGNING_SECRET", tool_name, resp.status)
                    return {"error": "request_failed"}
                return unwrap(await resp.json())
    except Exception:
        logger.exception("Tool call %s failed", tool_name)
        return {"error": "request_failed"}


def tool_ok(result: object) -> bool:
    """A consent write counts only if n8n answered status ok (the tool wrapper hands back {"error": ...} as ordinary data, not an exception)."""
    return isinstance(result, dict) and result.get("status") == "ok"


async def record_opt_out(consent: ConsentFlow, lead_phone_number: str, channels: str) -> dict:
    """Send opt_out_desertcomfort - the ONLY place that does (the model's tool and the 3.5 s code backstop both come through here). The claim is
    made before anything is awaited so a repeated trigger phrase or the backstop sees it at once; the lock keeps opt-outs and opt-ins in order."""
    consent.opt_out_claimed(channels)
    async with consent.lock:
        return await call_tool("opt_out_desertcomfort", lead_phone_number, channels=channels)


async def record_opt_in(consent: ConsentFlow, lead_phone_number: str, channels: str, only_if_still_needed: bool = False) -> dict:
    """Send grant_opt_in_desertcomfort - the ONLY place that does. If the opt-out had not been sent yet it goes FIRST (a true ledger); only a write
    n8n confirms ends the opt-out state, so a failed write leaves the opt-out in force, the safe direction."""
    if consent.needs_backstop():
        await record_opt_out(consent, lead_phone_number, "call_and_sms")
    async with consent.lock:
        if only_if_still_needed and not consent.needs_optin_backstop():
            return {"status": "ok", "note": "already recorded"}
        result = await call_tool("grant_opt_in_desertcomfort", lead_phone_number, channels=channels)
        if tool_ok(result):
            consent.opt_in_granted()
            logger.info("OPT-OUT TAKEN BACK: grant_opt_in_desertcomfort recorded (channels=%s); the call carries on", channels)
        else:
            logger.warning("OPT-IN FAILED: the take-back could not be recorded, the opt-out stays in force: %r", result)
        return result


async def run_opt_out_backstop(consent: ConsentFlow, lead_phone_number: str, room_name: str, trigger_text: str) -> None:
    """Fires opt_out_desertcomfort from code when the lead's opt-out words were heard and the model did not call the tool within the grace window."""
    await asyncio.sleep(OPT_OUT_TOOL_BACKSTOP_GRACE_SECONDS)
    if not consent.needs_backstop():
        return
    logger.warning(
        "OPT-OUT TOOL BACKSTOP: lead said %r but opt_out_desertcomfort was never called within %.1fs - firing it directly (room=%s)",
        trigger_text[:120], OPT_OUT_TOOL_BACKSTOP_GRACE_SECONDS, room_name,
    )
    try:
        await record_opt_out(consent, lead_phone_number, "call_and_sms")
    except Exception:
        logger.exception("Opt-out tool backstop call failed (room=%s)", room_name)


async def run_opt_in_backstop(consent: ConsentFlow, lead_phone_number: str, room_name: str) -> None:
    """Fires grant_opt_in_desertcomfort from code when BOTH the lead's take-back words and the agent's own take-back line were seen while the
    opt-out is in force and the model did not call the tool within the grace window. A failed write leaves the opt-out in force."""
    await asyncio.sleep(OPT_IN_TOOL_BACKSTOP_GRACE_SECONDS)
    if not consent.needs_optin_backstop():
        return
    logger.warning(
        "OPT-IN TOOL BACKSTOP: the lead took the opt-out back and the agent said so, but grant_opt_in_desertcomfort was never called within %.1fs - sending it (room=%s)",
        OPT_IN_TOOL_BACKSTOP_GRACE_SECONDS, room_name,
    )
    try:
        await record_opt_in(consent, lead_phone_number, consent.channels or "call_and_sms", only_if_still_needed=True)
    except Exception:
        logger.exception("Opt-in tool backstop call failed (room=%s)", room_name)


def is_take_back(text: str) -> bool:
    """The lead's own take-back words (the check-up's one definition of them, call_checkup.REVERSAL_RE)."""
    return bool(text and call_checkup.REVERSAL_RE.search(text))


def consent_turn(consent: ConsentFlow, role: str, text: str) -> str:
    """Feed one finished turn to the consent state. Returns "opt_out_backstop" or "opt_in_backstop" when the caller must start that code
    backstop, else ""."""
    if role == "user":
        # "I never filled out a form" is an opt-out too (Admin 2026-10-30), so the 3.5 s code backstop covers it the same way
        if OPT_OUT_TRIGGER_RE.search(text) or CONSENT_DISPUTE_RE.search(text):
            consent.opt_out_heard()
            return "opt_out_backstop" if consent.needs_backstop() else ""
        if consent.active and is_take_back(text):
            consent.takeback_words_heard()
    elif consent.active and is_assistant_takeback(text):
        consent.takeback_said()
    return "opt_in_backstop" if consent.optin_backstop_due() else ""
