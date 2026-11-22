"""Reliable delivery of the OUTBOUND (speed to lead) end-of-call report: save to disk first, send, delete only when n8n confirms, re-send leftovers in the
background, plus the crash alert. Copied in kind from desert_comfort_agent.py's outbox (importing that module would start the inbound agent).

Differences from the inbound copy, on purpose:
  - its own folder (outbox-outbound/) and its own n8n webhook, so the inbound agent's replay thread can never send an outbound report to the wrong place;
  - no heartbeat yet: the agent-down heartbeat needs its own n8n branch (build step 4/5) so it cannot mask an inbound outage.
Re-sending is safe: the n8n call-end branch dedupes on job_id (Redis guard, built in step 4).
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

from outbound_tools import N8N_BASE_URL, webhook_auth_headers

logger = logging.getLogger("desert-comfort-outbound-agent")

CALL_END_URL = f"{N8N_BASE_URL}/webhook/YOUR_WEBHOOK_PATH"
OUTBOX_DIR = Path(__file__).parent / "outbox-outbound"
OUTBOX_DEAD_DIR = OUTBOX_DIR / "dead"
OUTBOX_REQUEST_TIMEOUT_SECONDS = 15
OUTBOX_SEND_ATTEMPTS = 3
OUTBOX_RETRY_DELAYS_SECONDS = (2.0, 8.0)
OUTBOX_REPLAY_INTERVAL_SECONDS = 300
OUTBOX_REPLAY_MIN_AGE_SECONDS = 150  # older than n8n's in-flight lease and this process's own retries, so a replay never races a live send
OUTBOX_STALE_ERROR_AFTER_SECONDS = 3600
OUTBOX_DEAD_AFTER_SECONDS = 7 * 24 * 3600  # n8n's "done" mark expires at 7 days
OUTBOX_USER_AGENT = "convertecho-livekit-agent/1.0"  # Cloudflare in front of n8n refuses urllib's default user agent


def report_was_received(status: int, body_text: str) -> bool:
    """True when n8n's reply means the report is safely handled and the saved copy can go: {"status":"ok"}, or duplicate_ignored with
    already_done true. duplicate_ignored with already_done false means another attempt is still running, so the copy must stay."""
    if status != 200:
        return False
    try:
        reply = json.loads(body_text)
    except ValueError:
        return True
    if isinstance(reply, dict) and reply.get("status") == "duplicate_ignored":
        return reply.get("already_done") is not False
    return True


def spool_report(payload: dict) -> Path | None:
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("job_id") or ""))
        path = OUTBOX_DIR / f"{safe_id or 'nojob-' + str(int(time.time() * 1000))}.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, path)  # atomic: the replay thread never sees half a file
        return path
    except Exception:
        logger.exception("Could not save the call report to the outbox - will still try a direct send")
        return None


async def deliver_report(payload: dict, spool_path: Path | None) -> None:
    room = payload.get("room")
    for attempt in range(1, OUTBOX_SEND_ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(CALL_END_URL, json=payload, headers=webhook_auth_headers(),
                                     timeout=aiohttp.ClientTimeout(total=OUTBOX_REQUEST_TIMEOUT_SECONDS)) as resp:
                    body_text = await resp.text()
                    if report_was_received(resp.status, body_text):
                        if spool_path:
                            spool_path.unlink(missing_ok=True)
                        return
                    logger.warning("Call-end webhook not confirmed (status=%s, attempt %d/%d, room=%s): %s", resp.status, attempt, OUTBOX_SEND_ATTEMPTS, room, body_text[:200])
        except Exception:
            logger.exception("Call-end webhook failed (attempt %d/%d, room=%s)", attempt, OUTBOX_SEND_ATTEMPTS, room)
        if attempt < OUTBOX_SEND_ATTEMPTS:
            await asyncio.sleep(OUTBOX_RETRY_DELAYS_SECONDS[attempt - 1])
    logger.error("Call report for room=%s NOT delivered after %d attempts - kept in %s for the background resend", room, OUTBOX_SEND_ATTEMPTS, spool_path or "(no outbox copy - spool failed)")


def replay_outbox_once() -> None:
    if not OUTBOX_DIR.exists():
        return
    now = time.time()
    for path in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            age = now - path.stat().st_mtime
            if age < OUTBOX_REPLAY_MIN_AGE_SECONDS:
                continue
            if age > OUTBOX_DEAD_AFTER_SECONDS:
                OUTBOX_DEAD_DIR.mkdir(parents=True, exist_ok=True)
                os.replace(path, OUTBOX_DEAD_DIR / path.name)
                logger.error("Outbox report %s is over 7 days old - moved to %s and NOT resent (n8n's duplicate window has passed)", path.name, OUTBOX_DEAD_DIR)
                continue
            request = urllib.request.Request(CALL_END_URL, data=path.read_bytes(), method="POST",
                                             headers={"Content-Type": "application/json", "User-Agent": OUTBOX_USER_AGENT, **webhook_auth_headers()})
            try:
                with urllib.request.urlopen(request, timeout=OUTBOX_REQUEST_TIMEOUT_SECONDS) as resp:
                    status, body_text = resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                status, body_text = err.code, ""
            if report_was_received(status, body_text):
                path.unlink(missing_ok=True)
                logger.info("Outbox: resent %s (status=%s)", path.name, status)
            elif age > OUTBOX_STALE_ERROR_AFTER_SECONDS:
                logger.error("Outbox: %s still unsent after %.0f min (status=%s)", path.name, age / 60, status)
            else:
                logger.warning("Outbox: resend of %s not confirmed (status=%s), will retry", path.name, status)
        except Exception:
            logger.exception("Outbox: resend failed for %s", path.name)


def outbox_replay_loop() -> None:
    time.sleep(20)  # let the CLI finish configuring logging before the first pass
    while True:
        try:
            replay_outbox_once()
        except Exception:
            logger.exception("Outbox replay pass failed")
        time.sleep(OUTBOX_REPLAY_INTERVAL_SECONDS)


# --- crash alert: the model failed to generate a reply (see the inbound copy for the 2026-10-05 incident) ---
crash_ctx: dict = {"room": None, "job_id": None, "phone": None, "attempt": 1, "contact_id": "", "fired": False}


def reset_crash_ctx(room_name: str, job_id: str, phone: str, attempt: int = 1, contact_id: str = "") -> None:
    crash_ctx.update(room=room_name, job_id=job_id, phone=phone, attempt=attempt, contact_id=contact_id, fired=False)


def crash_payload(room: str, job_id: str, phone: str, attempt: int = 1, contact_id: str = "") -> dict:
    """The "-crash" job_id suffix keeps this early alert from colliding with (and suppressing) the call's real report if the call recovers."""
    return {
        "direction": "outbound", "lead_phone_number": phone, "contact_id": contact_id, "room": room, "attempt": attempt,
        "job_id": f"{job_id}-crash" if job_id else f"unknown-crash-{int(time.time())}",
        "outcome": "dead_air", "transcript_text": "", "call_duration_secs": None, "recording_consent": "pending",
        "checkup": {"crash_detected": True, "reason": "the model failed to generate a reply mid-call - a diagnostic alert, not the call's final outcome"},
    }


async def send_crash_alert(room: str, job_id: str, phone: str, attempt: int = 1, contact_id: str = "") -> None:
    try:
        payload = crash_payload(room, job_id, phone, attempt, contact_id)
        logger.error("CRASH ALERT: model reply failure detected - spooling an immediate dead_air alert (room=%s)", room)
        await deliver_report(payload, spool_report(payload))
    except Exception:
        logger.exception("Could not send the crash alert (room=%s)", room)


class CrashReplyFailureHandler(logging.Handler):
    """Watches the SDK's own "livekit.agents" logger for the one error that means the model failed to generate a reply at all."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name != "livekit.agents" or record.levelno < logging.ERROR:
                return
            if "failed to generate a reply" not in record.getMessage():
                return
            if crash_ctx["fired"] or not crash_ctx["room"]:
                return
            crash_ctx["fired"] = True
            asyncio.get_event_loop().create_task(send_crash_alert(crash_ctx["room"], crash_ctx["job_id"], crash_ctx["phone"], crash_ctx["attempt"], crash_ctx["contact_id"]))
        except Exception:
            pass  # a logging handler must never itself raise
