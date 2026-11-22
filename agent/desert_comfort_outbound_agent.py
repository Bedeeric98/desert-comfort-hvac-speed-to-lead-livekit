"""
Desert Comfort HVAC - outbound agent (speed to lead). Build stage 5 wiring (2026-10-27), on top of the stage 4 skeleton.

Joins a room we dispatched it into by name, waits for the phone to be answered, tells a person from a voicemail (AMD plus the audio
checks in outbound_audio.py), and with a person runs the real speed-to-lead call: the one-line opening (greeting, digital-assistant
disclosure, recording notice and consent question), then the two-layer prompt in speed-to-lead-prompt.md with the
Desert Comfort tools (service radius, availability, booking, transfer, opt-out, opt-in, current time, hang up), the known-facts notes,
the opt-out / take-back code backstops and ONE call-ending path. A voicemail, a full mailbox or a phone menu gets a silent hang-up,
never a message.

End of call (build step 2, 2026-10-30): _on_session_end builds the report (outbound_report.py: outcome, details, summary, check-up, scan, finished
messages) and sends it through the outbox (outbound_outbox.py) to the n8n call-end webhook. Nothing quotes the lead unless they clearly agreed to
recording (recording_consent.py). The n8n side of all this does not exist yet (build steps 3-5): until it does, reports wait in outbox-outbound/.

Separate worker and service from desert_comfort_agent.py (inbound). Rooms are named out-_<number>_<tag>; the inbound worker refuses
them. n8n only dispatches; the agent dials the lead itself (_dial_lead) and sends the short no_answer /
dial_failed report when the phone never connects, so every call sends exactly one report.
"""

import asyncio
import json
import logging
import os
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from google.protobuf import duration_pb2
from openai import AsyncOpenAI

from livekit import api, rtc
from livekit.agents import AMD, Agent, AgentServer, AgentSession, JobContext, JobProcess, RunContext, cli
from livekit.agents.llm import function_tool
from livekit.plugins import deepgram, openai, silero

import outbound_prompt as op
from closing_ack import ConsentFlow, GoodbyeGuard
from known_facts import KnownFacts
from missing_piece import MissingPiece
from outbound_amd import (
    ClosingWatcher,
    OPENER_SILENCE_SECONDS,
    SPEAK_FIRST_SILENCE_SECONDS,
    action_after_amd,
    hangup_wait_seconds,
    is_voicemail_text,
    machine_reason,
)
import outbound_outbox as outbox
import outbound_outcome as oo
from outbound_audio import BurstTracker, ToneDetector
from outbound_report import CallState, build_payload, dial_payload
from recording_consent import ACK_BACKSTOP_INSTRUCTIONS, RecordingConsent
from outbound_tools import (
    call_tool,
    consent_turn,
    current_time_formatted,
    lookup_lead,
    normalize_phone_number,
    phone_from_outbound_room,
    record_opt_in,
    record_opt_out,
    run_opt_in_backstop,
    run_opt_out_backstop,
)

logger = logging.getLogger("desert-comfort-outbound-agent")

load_dotenv()

AGENT_NAME = "desert-comfort-outbound"

# Same switch and folder as the inbound agent: both sides of every call saved as wav so lag and hang-up problems can be measured.
RECORD_DEBUG_AUDIO = os.getenv("RECORD_DEBUG_AUDIO", "false").lower() == "true"
RECORDINGS_DIR = Path(__file__).parent / "recordings"
# Same quiet window as the inbound agent's hang-up backstop (HANGUP_BACKSTOP_GRACE_SECONDS).
HANGUP_BACKSTOP_GRACE_SECONDS = 3.0

# Hard cap so a stuck call can never run on. A booking call is longer than the 5-minute test calls were; the inbound agent has no cap.
MAX_CALL_SECONDS = 900
# No-recording mode (recording_consent.py): if a lead declines and Alice has not said she is not recording, the code says it after this wait,
# checking again every ACK_BACKSTOP_RETRY_SECONDS (up to ACK_BACKSTOP_TRIES times) while she is still speaking or thinking.
ACK_BACKSTOP_GRACE_SECONDS = 4.0
ACK_BACKSTOP_RETRY_SECONDS = 2.0
ACK_BACKSTOP_TRIES = 3
# Same wait-for-speech-to-finish idea as the inbound agent's hangup (HANGUP_TOOL_SPEECH_POLL_*): delete the room once the agent has
# stopped talking, capped because the agent state can stay "thinking" while the hangup tool itself is running.
# After the opener finishes, wait this long before listening for the callee's answer so the tail of our own echo is not mistaken for it.
OPENER_ECHO_SETTLE_SECONDS = 0.5
SPEECH_POLL_INTERVAL_SECONDS = 0.1
SPEECH_POLL_MAX_SECONDS = 4.0

# The agent's own dial (2026-10-31): the outbound trunk from .env, the Desert Comfort LiveKit number, 30 s of ringing.
OUTBOUND_SIP_TRUNK_ID = os.getenv("OUTBOUND_SIP_TRUNK_ID", "")
OUTBOUND_FROM_NUMBER = "+12125550103"
RINGING_TIMEOUT_SECONDS = 30

# How long the lead lookup (started at dispatch, so it runs while the phone rings) may still hold up the start of the call.
LOOKUP_WAIT_SECONDS = 6.0
# Same as the inbound agent: ask for the address if the lead gave only the name and the agent went quiet (missing_piece.py).
MISSING_PIECE_DELAY_SECONDS = 3.0
# Same as the inbound agent (OPT_OUT_GOODBYE_LINE): if the lead opts out and the agent never speaks, it says this before the call ends.
OPT_OUT_GOODBYE_LINE = "Understood, we won't contact you again. Take care."
OPT_OUT_GOODBYE_START_WAIT_SECONDS = 2.0

# AMD runs its own text model and speech-to-text because the call itself uses a speech-to-speech model (no text transcript of the
# greeting) and our server is self-hosted (LiveKit's default models run on LiveKit Cloud). These are LiveKit's own evaluated defaults
# (livekit.com/blog/answering-machine-detection): Gemini 3.1 Flash Lite through OpenRouter (OPENROUTER_API_KEY) and Deepgram nova-3
# (DEEPGRAM_API_KEY), both read from the environment by the plugins. AMD's no-speech wait is how long the callee gets to say their own
# "hello" before we speak first (SPEAK_FIRST_SILENCE_SECONDS).

server = AgentServer(port=8082, num_idle_processes=1)  # 8081 is the inbound worker's health port

# --- end-of-call report (build step 2, 2026-10-30) ---------------------------------------------------------------------------------------
# _run_call registers one CallState per room; _on_session_end (runs in the same job process after the call) builds the report from LiveKit's session
# record and sends it through the outbox. The model is used for two small things only: the last-resort outcome read and the written summary. Both are
# sent with store=False so OpenAI keeps no stored copy for application state (its 30-day abuse-monitoring logs still apply: see PLAN section 0).
_CALL_STATE: dict[str, CallState] = {}
_OUTCOME_MODEL = "gpt-5.6-luna"
_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI()
    return _openai_client


async def _classify_outcome(transcript_text: str) -> str:
    try:
        resp = await _get_openai_client().chat.completions.create(
            model=_OUTCOME_MODEL,
            messages=[{"role": "system", "content": oo.CLASSIFIER_SYSTEM_PROMPT}, {"role": "user", "content": transcript_text[:8000] or "(empty transcript)"}],
            max_completion_tokens=10,  # this model rejects max_tokens and any non-default temperature
            store=False,
        )
        return oo.parse_classifier_label(resp.choices[0].message.content or "")
    except Exception:
        logger.exception("Outcome classification call failed - defaulting to no_meaningful_conversation")
        return "no_meaningful_conversation"


async def _summarize(system_prompt: str, user_text: str) -> str:
    resp = await _get_openai_client().chat.completions.create(
        model=_OUTCOME_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}], max_completion_tokens=250, store=False,
    )
    return (resp.choices[0].message.content or "").strip()


async def _on_session_end(ctx: JobContext) -> None:
    room_name = ctx.job.room.name
    state = _CALL_STATE.pop(room_name, None)
    if state is None:
        logger.warning("OUTBOUND REPORT: no call state for room=%s (the job ended before the call was set up) - no report sent", room_name)
        return
    try:
        report = ctx.make_session_report()
        payload = await build_payload(report, state, classify=_classify_outcome, summarize=_summarize)
        if payload is None:
            logger.info("OUTBOUND REPORT: the phone was never answered, so the agent sends no report (n8n sees that from the dial result) (room=%s)", room_name)
            return
        logger.info("OUTBOUND REPORT: outcome=%s reason=%r recording=%s emergency_unbooked=%s (room=%s)",
                    payload["outcome"], payload.get("outcome_reason"), payload["recording_consent"], payload.get("emergency_unbooked"), room_name)
        await outbox.deliver_report(payload, outbox.spool_report(payload))
    except Exception:
        logger.exception("OUTBOUND REPORT: building or sending the report failed (room=%s)", room_name)


logging.getLogger("livekit.agents").addHandler(outbox.CrashReplyFailureHandler())


def _prewarm(proc: JobProcess) -> None:
    # Silero VAD (local, no key). The answering-machine detector learns that the callee started or stopped talking only from the session's
    # VAD events; without one it always saw speech=0.0s and timed out before the first transcript (2026-10-27 live tests).
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = _prewarm


async def _end_call(ctx: JobContext, session: AgentSession, room_name: str, wait_for_speech: bool = True) -> None:
    """Deleting the room is what makes livekit-sip send the SIP BYE (session.aclose() only closes the agent's own pipeline).
    wait_for_speech=False ends the call at once (voicemail: nothing the agent says is worth finishing)."""
    elapsed = 0.0
    while wait_for_speech and elapsed < SPEECH_POLL_MAX_SECONDS and str(getattr(session, "agent_state", "")) in ("speaking", "thinking"):
        await asyncio.sleep(SPEECH_POLL_INTERVAL_SECONDS)
        elapsed += SPEECH_POLL_INTERVAL_SECONDS
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        logger.exception("Failed to delete room to end call (room=%s)", room_name)


async def _speak_opt_out_goodbye(session: AgentSession, room_name: str) -> None:
    logger.warning("OPT-OUT GOODBYE BACKSTOP: the lead opted out but the agent never spoke - saying the goodbye before ending the call (room=%s)", room_name)
    try:
        session.generate_reply(instructions=f"Say exactly: '{OPT_OUT_GOODBYE_LINE}'")
        elapsed = 0.0
        while elapsed < OPT_OUT_GOODBYE_START_WAIT_SECONDS and str(getattr(session, "agent_state", "")) != "speaking":
            await asyncio.sleep(SPEECH_POLL_INTERVAL_SECONDS)
            elapsed += SPEECH_POLL_INTERVAL_SECONDS
    except Exception:
        logger.exception("Could not speak the opt-out goodbye backstop (room=%s)", room_name)


async def _record_track_to_wav(track: rtc.Track, out_path: Path, rec: RecordingConsent) -> None:
    # like _record_track_to_wav in desert_comfort_agent.py (duplicated on purpose: importing that module would start the inbound agent's setup),
    # plus the no-recording mode: writing stops the moment the lead declines, and the file is deleted unless the lead gave a clear yes
    audio_stream = rtc.AudioStream(track)
    wav_file: "wave.Wave_write | None" = None
    try:
        async for event in audio_stream:
            if not rec.audio_allowed():
                break
            frame = event.frame
            if wav_file is None:
                wav_file = wave.open(str(out_path), "wb")
                wav_file.setnchannels(frame.num_channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(frame.sample_rate)
                logger.info("Recording started: %s", out_path)
            data = frame.data
            wav_file.writeframes(data.tobytes() if hasattr(data, "tobytes") else bytes(data))
    except Exception:
        logger.exception("Recording to %s stopped early", out_path)
    finally:
        if wav_file is not None:
            wav_file.close()
            if rec.audio_may_remain():
                logger.info("Recording saved: %s", out_path)
            else:
                out_path.unlink(missing_ok=True)
                logger.info("Debug recording deleted: the lead did not give a clear yes to recording (%s)", rec.summary())
        await audio_stream.aclose()


class OutboundAgent(Agent):
    """The ONE way this agent ends a call is end_call(): the voicemail path, the model's hangup tool and the hang-up backstop all go through it."""

    def __init__(
        self,
        ctx: JobContext,
        room_name: str,
        call_age: "Callable[[], float]",
        *,
        instructions: str,
        lead_phone_number: str,
        hours_mode: str,
        consent: ConsentFlow,
        recording: RecordingConsent,
    ) -> None:
        super().__init__(instructions=instructions)
        self._ctx = ctx
        self._room_name = room_name
        self._call_age = call_age
        self._lead_phone_number = lead_phone_number
        self._hours_mode = hours_mode
        self._consent = consent
        self._recording = recording
        self._ended = False
        # set by _run_call: starts the "say you are not recording" backstop when the model's tool is what declined the recording
        self.on_recording_declined: "Callable[[], None]" = lambda: None
        # set by _run_call: has the lead opted out without the agent saying goodbye yet? (closing_ack.GoodbyeGuard)
        self.goodbye_owed: "Callable[[], bool]" = lambda: False
        # set once the room is deleted: with the model's audio input switched off the session may never report a close, so the job waits on this too
        self.finished = asyncio.Event()

    async def end_call(self, wait_for_speech: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        # Telnyx short-duration-call rule: no hang-up sooner than MIN_HANGUP_AGE_SECONDS after the answer (the agent is silent meanwhile)
        await asyncio.sleep(hangup_wait_seconds(self._call_age()))
        if wait_for_speech and self.goodbye_owed():
            await _speak_opt_out_goodbye(self.session, self._room_name)
        await _end_call(self._ctx, self.session, self._room_name, wait_for_speech)
        self.finished.set()

    @function_tool
    async def get_current_time(self, context: RunContext) -> dict:
        """Get the current date and time in Arizona (America/Phoenix). Call
        this whenever you need to know the current time or resolve a
        relative date/time reference mid-call - the time given at the start
        of the call is only a snapshot and won't update on its own."""
        return {"current_time_formatted": current_time_formatted()}

    @function_tool
    async def radius_checker_desertcomfort(self, context: RunContext, address: str) -> dict:
        """Check whether the lead's address is within the 60-mile service
        radius of central Phoenix. Call once the lead's full service
        address is known."""
        self.duplex_session.append_commentary("One moment, let me check that.")
        return await call_tool("radius_checker_desertcomfort", self._lead_phone_number, address=address)

    @function_tool
    async def check_availability_desertcomfort(self, context: RunContext, urgent: bool) -> dict:
        """Check the calendar for open appointment slots. Call only after
        collecting all qualifying information and confirming the lead
        accepts the trip-fee pricing."""
        self.duplex_session.append_commentary("Just a second, pulling up the schedule.")
        # hours_mode is set by code from the call-start lookup, never by the model (n8n answers a night call with ONE finished time)
        return await call_tool("check_availability_desertcomfort", self._lead_phone_number, urgent=urgent, hours_mode=self._hours_mode)

    @function_tool
    async def book_appointment_desertcomfort(
        self,
        context: RunContext,
        phone_number: str,
        first_name: str,
        last_name: str,
        address: str,
        absolute_time: str,
        urgent: bool,
    ) -> dict:
        """Book the appointment after the lead has confirmed they'll pay
        the trip fee and selected a specific available time slot."""
        self.duplex_session.append_commentary("Hang tight, I'm getting that booked.")
        return await call_tool(
            "book_appointment_desertcomfort",
            self._lead_phone_number,
            phone_number=phone_number,
            first_name=first_name,
            last_name=last_name,
            address=address,
            absolute_time=absolute_time,
            urgent=urgent,
        )

    @function_tool
    async def transfer_to_human(self, context: RunContext, reason: str) -> dict:
        """Transfer the call to a human - commercial properties, a human
        request, billing/warranty disputes, pricing negotiation beyond the
        standard script, hostile callers, or anything outside scope."""
        # transfer_to_human's n8n handler only logs a follow-up message (it has never performed a live transfer); same wording as inbound by day
        self.duplex_session.append_commentary("One moment, connecting you now.")
        return await call_tool("transfer_to_human", self._lead_phone_number, reason=reason)

    @function_tool
    async def opt_out_desertcomfort(self, context: RunContext, channels: str) -> dict:
        """Call the moment the lead explicitly says they don't want
        Desert Comfort to contact them again, or asks to be taken off any
        list. channels: "call_and_sms" for "stop calling me" / "don't
        contact me" / anything general - "sms_only" only if they
        specifically object to texts and nothing else. Do not argue or ask
        why; say your one acknowledgment line per Opt-Out in your
        instructions, in that same turn. Do NOT call `hangup` after it:
        the call ends by itself if the lead says nothing more, and the
        lead may still take it back. Do not ask anything else first."""
        return await record_opt_out(self._consent, self._lead_phone_number, channels)

    @function_tool
    async def grant_opt_in_desertcomfort(self, context: RunContext, channels: str) -> dict:
        """Call (1) right after the lead takes back an opt-out in this
        same call ("never mind", "wait", "let's continue", "I didn't mean
        that"): use the same channels you passed to opt_out_desertcomfort,
        no confirming question needed; or (2) after the lead themselves
        volunteers that Desert Comfort can contact them again AND you have
        asked one explicit confirming question naming the channel, and
        they said yes. Never call this on your own initiative otherwise.
        channels: "call_and_sms", "call_only", or "sms_only" - whatever
        the lead actually agreed to, never more."""
        return await record_opt_in(self._consent, self._lead_phone_number, channels)

    @function_tool
    async def decline_recording(self, context: RunContext) -> dict:
        """Call the moment the lead says they do NOT want the call recorded
        ("no", "I don't want to be recorded", "stop recording"), or when
        their answer to the recording question is still unclear after you
        asked it a second time. Say your one acknowledgment line per
        Consent in your instructions, in that same turn, then carry on
        with the call as normal. NEVER call hangup because of it."""
        newly = self._recording.decline("tool")
        logger.info("RECORDING DECLINED via the model's tool (newly=%s, %s, room=%s)", newly, self._recording.summary(), self._room_name)
        if newly:
            self.on_recording_declined()
        return {"status": "ok", "recording": "off"}

    @function_tool
    async def hangup(self, context: RunContext) -> None:
        """Trigger the moment the lead gives any closing signal,
        immediately after the goodbye line, same turn. Never after an
        opt-out: there the call ends by itself so the lead can still
        change their mind."""
        if not self._consent.hangup_allowed():
            logger.info("HANGUP TOOL IGNORED: an opt-out is in force and the lead may still take it back; the hang-up timer ends the call if they stay quiet (room=%s)", self._room_name)
            return
        logger.info("HANGUP TOOL: model called hangup directly (room=%s)", self._room_name)
        await self.end_call()


async def _run_call(ctx: JobContext, room_name: str, metadata: dict, rec: RecordingConsent) -> None:
    # The lead's number: the room name carries it (out-_<digits>_<tag>); the dispatch metadata is the fallback. The GHL lookup starts now and
    # runs while the phone rings, so it normally costs the call nothing.
    lead_phone_number = normalize_phone_number(phone_from_outbound_room(room_name) or str(metadata.get("phone", "")))
    if not lead_phone_number:
        logger.warning("OUTBOUND: no lead phone number in the room name or metadata, the tools will have no number to work with (room=%s)", room_name)
    lookup_task = asyncio.create_task(lookup_lead(lead_phone_number)) if lead_phone_number else None

    participant = await ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    logger.info("OUTBOUND: callee line joined, waiting for the answer (identity=%s, room=%s)", participant.identity, room_name)

    # The call's age starts when LiveKit reports the SIP call active (the answer). If that is never seen, the age stays 0 and every
    # hang-up waits the full minimum, which is the safe direction.
    answered_at: list[float | None] = [None]

    def _note_status(attributes) -> None:
        if answered_at[0] is None and attributes.get("sip.callStatus") == "active":
            answered_at[0] = time.monotonic()
            logger.info("ANSWERED: call is active (room=%s)", room_name)

    def _call_age() -> float:
        return time.monotonic() - answered_at[0] if answered_at[0] is not None else 0.0

    _note_status(participant.attributes)

    @ctx.room.on("participant_attributes_changed")
    def _on_participant_attributes_changed(changed, changed_participant) -> None:
        if changed_participant.identity == participant.identity:
            _note_status(changed_participant.attributes)

    # What we know about the lead: dispatch metadata first, the GHL lookup for the rest. A slow or failed lookup means a plain "Hello" opening.
    lookup: dict = {"route": "new_caller"}
    if lookup_task is not None:
        try:
            lookup = await asyncio.wait_for(lookup_task, timeout=LOOKUP_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("OUTBOUND: the lead lookup was still running after %.0fs, going on without it (room=%s)", LOOKUP_WAIT_SECONDS, room_name)
    lead = op.lead_from(metadata, lookup)
    hours_mode = str(lookup.get("hours_mode", "day"))
    now_text = current_time_formatted()
    voice_instructions = op.render_voice_layer(
        first_name=lead["first_name"], last_name=lead["last_name"], current_time_formatted=now_text, lead_phone_number=lead_phone_number,
        on_file_address=lead["address"], faq_objection_content=op.load_faq_content(),
    )
    backend_instructions = op.render_backend_layer(current_time_formatted=now_text, lead_phone_number=lead_phone_number)
    opening_line = op.render_opening(lead["first_name"], lead["attempt"], lead["texted"])
    logger.info(
        "OUTBOUND LEAD: route=%s name_on_file=%s address_on_file=%s attempt=%s texted=%s hours_mode=%s (room=%s)",
        lookup.get("route"), bool(lead["first_name"]), bool(lead["address"]), lead["attempt"], lead["texted"], hours_mode, room_name,
    )

    # The call's opt-out state: one source of truth for the model's tools, the 3.5 s code backstops, the hang-up timer and the goodbye guard.
    consent = ConsentFlow()
    goodbye_guard = GoodbyeGuard()
    # One per call: remembers what the lead already said (temperature, unit, smells, leaks, who is home, name, address) and hands the model a
    # short "KNOWN FACT" note so it never asks for something it was just told. Best-effort: a failure may cost the note, never the call.
    known_facts = KnownFacts()
    # One per call: if the agent asked for the name AND the address in one question and the lead gave only the name, ask for the address.
    missing_piece = MissingPiece()

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        llm=openai.realtime.GPTLiveModel(
            voice="marin",
            delegation="responses",
            responses_options={
                "model": "gpt-5.6-luna",
                "instructions": backend_instructions,
                # same settings as the inbound agent (see the comments there): low reasoning effort (the generic "minimal" is rejected by this
                # model), low verbosity, and the priority service tier
                "reasoning": {"effort": "low"},
                "text": {"verbosity": "low"},
                "service_tier": "priority",
            },
        ),
    )
    agent = OutboundAgent(
        ctx, room_name, _call_age, instructions=voice_instructions, lead_phone_number=lead_phone_number, hours_mode=hours_mode, consent=consent,
        recording=rec,
    )
    agent.goodbye_owed = lambda: goodbye_guard.owed(consent.active)
    agent.on_recording_declined = lambda: asyncio.create_task(_ack_backstop())
    closed = asyncio.Event()
    session.on("close", lambda *_: closed.set())

    # Machine checks that need no transcript: how long the callee has talked without a pause (a voicemail greeting is one long burst
    # from the answer, a person's "hello?" is short) and the beep. Any machine signal makes the agent silent at once; the hang-up itself
    # still goes through agent.end_call(), which waits for the minimum call age.
    tracker, tone = BurstTracker(), ToneDetector()
    machine, human_confirmed, callee_sound = asyncio.Event(), asyncio.Event(), asyncio.Event()
    words_heard = [False]
    silent_line = [False]  # set when the opener got no answer from a line that did pick up: the report's silent_pickup
    # What the end-of-call report needs, read when the call is over (see _on_session_end)
    _CALL_STATE[room_name] = CallState(
        lead_phone_number=lead_phone_number, attempt=lead["attempt"], texted=lead["texted"], hours_mode=hours_mode, first_name=lead["first_name"],
        last_name=lead["last_name"], address_on_file=lead["address"], recording=rec, is_answered=lambda: answered_at[0] is not None,
        is_machine=machine.is_set, is_silent_line=lambda: silent_line[0], optout_active=lambda: consent.active,
        optout_restored=lambda: bool(consent.restored and not consent.active), known_facts=known_facts.captured,
        contact_id=lead["contact_id"],
    )
    # True once our opener has finished playing (plus a short settle): only audio heard after that counts as the callee answering, because
    # our own voice bleeds back onto the callee's track while it plays (seen on the first silent-line test: a 2500-level blip during the opener)
    listening_after_opener = [False]
    completed_before_opener = [0.0]
    tap_started = [False]

    def _go_silent() -> None:
        # stop feeding the callee's audio to the model (it would answer a greeting) and cut off anything it is saying
        for step in (lambda: session.input.set_audio_enabled(False), session.interrupt):
            try:
                step()
            except Exception:
                logger.exception("Could not silence the agent (room=%s)", room_name)

    def _check_machine() -> None:
        if machine.is_set():
            return
        reason = machine_reason(tracker.is_long(), tone.fired, words_heard[0], human_confirmed.is_set())
        if reason:
            machine.set()
            logger.warning("MACHINE SIGNAL (%s): agent silent, hanging up once the call is old enough (age=%.1fs, room=%s)", reason, _call_age(), room_name)
            _go_silent()
            asyncio.create_task(agent.end_call(wait_for_speech=False))

    async def _tap_callee_audio(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                frame = event.frame
                samples = np.frombuffer(frame.data, dtype=np.int16)
                if frame.num_channels > 1:
                    samples = samples[:: frame.num_channels]
                tracker.feed(samples, frame.sample_rate)
                tone.feed(samples, frame.sample_rate)
                if listening_after_opener[0]:
                    if tracker.burst_seconds > 0:
                        callee_sound.set()
                    # a short burst that ended after our opener is a person answering: stop treating a long burst as a greeting
                    if tracker.last_completed != completed_before_opener[0] and not tracker.is_long() and tracker.burst_seconds == 0:
                        human_confirmed.set()
                _check_machine()
        except Exception:
            logger.exception("Audio tap stopped early (room=%s)", room_name)
        finally:
            await stream.aclose()

    def _start_tap(track: rtc.Track) -> None:
        if not tap_started[0]:
            tap_started[0] = True
            asyncio.create_task(_tap_callee_audio(track))

    @ctx.room.on("track_subscribed")
    def _on_callee_track_subscribed(track, publication, remote_participant) -> None:
        if remote_participant.identity == participant.identity and track.kind == rtc.TrackKind.KIND_AUDIO:
            _start_tap(track)

    for publication in participant.track_publications.values():
        if publication.track is not None and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
            _start_tap(publication.track)

    # Turn text and timing go to the log (the first pickup test had none, so a "laggy" call could not be measured), and the
    # hang-up backstop watches both sides for a goodbye in case the model says one without calling hangup.
    watcher = ClosingWatcher(consent)
    caller_state_at: list[float | None] = [None]
    last_user_listening_at: list[float | None] = [None]
    opening_asked_at: list[float | None] = [None]

    def _caller_quiet_seconds() -> float:
        if str(getattr(session, "user_state", "")) == "speaking":
            return 0.0
        return time.time() - caller_state_at[0] if caller_state_at[0] is not None else 1e9

    async def _hangup_backstop(trigger_item_id: str) -> None:
        await asyncio.sleep(HANGUP_BACKSTOP_GRACE_SECONDS)
        quiet = _caller_quiet_seconds()
        if not watcher.should_end(trigger_item_id, quiet):
            return  # someone spoke again since, or the caller is mid-sentence: a newer finished turn re-arms or stands it down
        logger.info("HANGUP BACKSTOP: ending call %.0fs after the last turn with no further speech (room=%s)", HANGUP_BACKSTOP_GRACE_SECONDS, room_name)
        await agent.end_call()

    def _log_text(line: str) -> None:
        # a log line that quotes the conversation: held until the lead has answered the recording question, written on a yes, thrown away
        # on a no or if no clear yes ever came (recording_consent.py). Counts and timings are logged directly, never through here.
        for out in rec.log(line):
            logger.info("%s (room=%s)", out, room_name)

    def _send_known_fact_note(note: str) -> None:
        try:
            # append_thinking = "something to know without saying it" (OpenAI GPT-Live); delegation_id None = session-wide
            agent.duplex_session.append_thinking(note)
            _log_text(f"KNOWN FACT sent to the model: {note[:160]}")
        except Exception:
            logger.exception("Could not send a KNOWN FACT note (room=%s)", room_name)

    async def _ack_backstop() -> None:
        # The lead declined (their words, or the model's tool). Alice must tell them she is not recording. If the model did not say it, say it.
        await asyncio.sleep(ACK_BACKSTOP_GRACE_SECONDS)
        for _ in range(ACK_BACKSTOP_TRIES):
            if not rec.ack_owed:
                return
            if str(getattr(session, "agent_state", "")) in ("speaking", "thinking") or str(getattr(session, "user_state", "")) == "speaking":
                await asyncio.sleep(ACK_BACKSTOP_RETRY_SECONDS)
                continue
            rec.ack_owed = False
            logger.warning("RECORDING ACK BACKSTOP: the lead declined recording but the agent never said it was not recording - saying it now (room=%s)", room_name)
            try:
                session.generate_reply(instructions=ACK_BACKSTOP_INSTRUCTIONS)
            except Exception:
                logger.exception("Could not speak the recording acknowledgement backstop (room=%s)", room_name)
            return

    def _recording_action(action: str) -> None:
        if action == "grant":
            logger.info("RECORDING CONSENT: the lead said yes (%s, room=%s)", rec.summary(), room_name)
            for out in rec.take_granted_lines():
                logger.info("%s (room=%s)", out, room_name)
        elif action == "decline":
            logger.warning("RECORDING DECLINED via the lead's words (%s, room=%s)", rec.summary(), room_name)
            asyncio.create_task(_ack_backstop())

    async def _missing_piece_nudge(trigger_item_id: str) -> None:
        await asyncio.sleep(MISSING_PIECE_DELAY_SECONDS)
        try:
            if watcher.last_item_id != trigger_item_id:
                return  # somebody spoke since, so the agent is not stuck
            if str(getattr(session, "agent_state", "")) in ("speaking", "thinking") or str(getattr(session, "user_state", "")) == "speaking":
                return
            instructions = missing_piece.take_nudge()
            if not instructions:
                return
            logger.info("MISSING PIECE NUDGE: the lead gave the name but not the address and the agent stayed quiet, asking for the address (room=%s)", room_name)
            session.generate_reply(instructions=instructions)
        except Exception:
            logger.exception("Could not send the missing-address nudge (room=%s)", room_name)

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = event.item
        role = getattr(item, "role", None)
        text = (getattr(item, "text_content", None) or "").strip()
        if role == "user":
            _log_text(f"USER TURN TEXT: {text!r}")
            _recording_action(rec.on_user_turn(text))
            if text:
                callee_sound.set()  # words the model heard from the callee (even mid-opener) mean someone is there
            if is_voicemail_text(text):
                # safety net behind AMD: the callee's own words are a voicemail greeting (ignored once a person is confirmed)
                logger.warning("VOICEMAIL WORDS HEARD (room=%s)", room_name)
                words_heard[0] = True
                _check_machine()
                if machine.is_set():
                    return
            for note in known_facts.on_user_turn(text):
                _send_known_fact_note(note)
            missing_piece.on_user_turn(text)
            if missing_piece.wants_nudge():
                asyncio.create_task(_missing_piece_nudge(item.id))
            consent_action = consent_turn(consent, "user", text)
            if consent_action == "opt_out_backstop":
                # the backstop logs the lead's words; with no clear yes to recording they must not reach the log
                asyncio.create_task(run_opt_out_backstop(consent, lead_phone_number, room_name, text if rec.transcript_allowed() else "(not recorded)"))
            elif consent_action == "opt_in_backstop":
                asyncio.create_task(run_opt_in_backstop(consent, lead_phone_number, room_name))
            if watcher.user_turn(item.id, text):
                asyncio.create_task(_hangup_backstop(item.id))
        elif role == "assistant":
            _log_text(f"ASSISTANT TURN TEXT: {text!r}")
            rec.on_assistant_turn(text)
            known_facts.on_assistant_turn(text)
            missing_piece.on_assistant_turn(text)
            if consent_turn(consent, "assistant", text) == "opt_in_backstop":
                asyncio.create_task(run_opt_in_backstop(consent, lead_phone_number, room_name))
            if watcher.assistant_turn(item.id, text):
                logger.info("CLOSING SIGNAL WATCHED after the agent's turn (room=%s)", room_name)
                asyncio.create_task(_hangup_backstop(item.id))

    @session.on("user_state_changed")
    def _on_user_state_changed(event) -> None:
        caller_state_at[0] = event.created_at
        if event.new_state == "speaking":
            goodbye_guard.caller_started(consent.active)
        if event.old_state == "speaking" and event.new_state == "listening":
            last_user_listening_at[0] = event.created_at

    @session.on("agent_state_changed")
    def _on_agent_state_changed(event) -> None:
        if event.new_state != "speaking":
            return
        goodbye_guard.agent_started()
        if opening_asked_at[0] is not None:
            logger.info("OPENING LAG: first audio %.2fs after the opening line was requested (room=%s)", event.created_at - opening_asked_at[0], room_name)
            opening_asked_at[0] = None
        elif last_user_listening_at[0] is not None:
            logger.info("TURN LATENCY (state-based): gap=%.3fs (room=%s)", event.created_at - last_user_listening_at[0], room_name)
            last_user_listening_at[0] = None

    await session.start(agent=agent, room=ctx.room)

    # AMD holds the agent's speech while it listens to the first thing the callee says, so nothing is said before the verdict. It races
    # the audio checks above: whichever sees a machine first wins and AMD is dropped.
    started = time.monotonic()

    async def _detect():
        async with AMD(
            session,
            llm=openai.LLM.with_openrouter(model="google/gemini-3.1-flash-lite"),
            stt=deepgram.STT(model="nova-3"),
            participant_identity=participant.identity,
            ivr_detection=False,
            detection_options={"no_speech_threshold": SPEAK_FIRST_SILENCE_SECONDS},
        ) as detector:
            return await detector.execute()

    async def _until_over() -> None:
        # the call is over when the session closes or our own hang-up has deleted the room, whichever comes first
        waiters = [asyncio.create_task(closed.wait()), asyncio.create_task(agent.finished.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()

    amd_task = asyncio.create_task(_detect())
    machine_wait = asyncio.create_task(machine.wait())
    await asyncio.wait({amd_task, machine_wait}, return_when=asyncio.FIRST_COMPLETED)
    machine_wait.cancel()
    if machine.is_set():
        amd_task.cancel()
        await asyncio.gather(amd_task, return_exceptions=True)
        await _until_over()  # the hang-up was already scheduled by _check_machine
        return

    result = amd_task.result()
    category = result.category.value
    action = action_after_amd(category, result.reason, machine.is_set())
    logger.info(
        "OUTBOUND AMD: category=%s action=%s reason=%s speech=%.1fs detect_delay=%.2fs total_wait=%.1fs (room=%s)",
        category, action, result.reason, result.speech_duration or 0.0, result.delay or 0.0, time.monotonic() - started, room_name,
    )
    _log_text(f"OUTBOUND AMD heard: {result.transcript!r}")  # the callee's own words: held like any other quote of the conversation

    if action == "hangup":
        _go_silent()
        await agent.end_call(wait_for_speech=False)
        return

    if action == "talk":
        human_confirmed.set()
    # a person (or a silent line, "speak_first"): say the one-line opening, then stay on the line until they hang up, the model's hangup
    # tool fires, or the time cap hits
    opening_asked_at[0] = time.time()
    handle = session.generate_reply(instructions=f"Say exactly: '{opening_line}'")
    if action == "speak_first":
        # nobody said anything for SPEAK_FIRST_SILENCE_SECONDS. After the opener, a silent line gets OPENER_SILENCE_SECONDS to answer;
        # a voicemail that starts late shows up through the audio checks, which stay on until a person is confirmed.
        await handle.wait_for_playout()
        await asyncio.sleep(OPENER_ECHO_SETTLE_SECONDS)
        callee_sound.clear()  # whatever the callee line carried while we were speaking was our own echo, not an answer
        completed_before_opener[0] = tracker.last_completed
        listening_after_opener[0] = True
        try:
            await asyncio.wait_for(callee_sound.wait(), timeout=OPENER_SILENCE_SECONDS)
        except asyncio.TimeoutError:
            logger.info("SILENT LINE: nothing heard %.0fs after the opener, ending the call (room=%s)", OPENER_SILENCE_SECONDS, room_name)
            silent_line[0] = True
            await agent.end_call()
            return
    await _until_over()


async def _dial_lead(ctx: JobContext, room_name: str, phone: str) -> tuple[str, str, int | None] | None:
    """Rings the lead and waits for the answer. None = answered; otherwise (outcome, reason, SIP code) for the short report."""
    if not phone:
        return "dial_failed", "no lead phone number in the room name or metadata", None
    if not OUTBOUND_SIP_TRUNK_ID:
        return "dial_failed", "OUTBOUND_SIP_TRUNK_ID is not set on the server", None
    try:
        await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            sip_trunk_id=OUTBOUND_SIP_TRUNK_ID, sip_number=OUTBOUND_FROM_NUMBER, sip_call_to=phone, room_name=room_name,
            participant_identity="sip_lead", wait_until_answered=True, ringing_timeout=duration_pb2.Duration(seconds=RINGING_TIMEOUT_SECONDS)))
        return None
    except Exception as err:  # the SDK's SIP error carries the SIP code in metadata (older versions: TwirpError, same field)
        meta = getattr(err, "metadata", None) or {}
        try:
            code = int(meta.get("sip_status_code"))
        except (TypeError, ValueError):
            code = None
        outcome = oo.dial_outcome(code, str(getattr(err, "code", "") or ""))
        logger.info("OUTBOUND DIAL: not connected, outcome=%s sip=%s (room=%s)", outcome, code, room_name)
        return outcome, str(meta.get("sip_status") or err)[:200], code


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=_on_session_end)
async def entrypoint(ctx: JobContext) -> None:
    room_name = ctx.job.room.name
    ctx.log_context_fields = {"room": room_name}
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
    except ValueError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    logger.info("OUTBOUND JOB: room=%s metadata=%s", room_name, metadata)
    lead_phone = normalize_phone_number(phone_from_outbound_room(room_name) or str(metadata.get("phone", "")))
    lead_meta = op.lead_from(metadata, {})
    outbox.reset_crash_ctx(room_name, ctx.job.id, lead_phone, lead_meta["attempt"], lead_meta["contact_id"])

    await ctx.connect()

    # one per call: whether the lead agreed to be recorded (recording_consent.py)
    rec = RecordingConsent()

    if RECORD_DEBUG_AUDIO:
        RECORDINGS_DIR.mkdir(exist_ok=True)

        @ctx.room.on("track_subscribed")
        def _on_track_subscribed(track, publication, remote_participant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_record_track_to_wav(track, RECORDINGS_DIR / f"{room_name}_caller_in.wav", rec))

        @ctx.room.on("local_track_published")
        def _on_local_track_published(publication, track) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_record_track_to_wav(track, RECORDINGS_DIR / f"{room_name}_agent_out.wav", rec))

    try:
        # the call setup waits for the lead's line while the dial rings it; a dial that never connects stops the setup and sends the short report
        call = asyncio.create_task(_run_call(ctx, room_name, metadata, rec))
        not_connected = await _dial_lead(ctx, room_name, lead_phone)
        if not_connected is not None:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            _CALL_STATE.pop(room_name, None)  # exactly one report per call: the session-end report must not also run
            outcome, reason, sip_code = not_connected
            payload = dial_payload(room=room_name, job_id=ctx.job.id, phone=lead_phone, contact_id=lead_meta["contact_id"], attempt=lead_meta["attempt"],
                                   outcome=outcome, reason=reason, sip_status_code=sip_code)
            await outbox.deliver_report(payload, outbox.spool_report(payload))
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
            return
        await asyncio.wait_for(call, timeout=MAX_CALL_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("OUTBOUND: call exceeded %ss, ending it (room=%s)", MAX_CALL_SECONDS, room_name)
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            logger.exception("Failed to delete room after the time cap (room=%s)", room_name)
    except Exception:
        logger.exception("OUTBOUND: call failed (room=%s)", room_name)
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            logger.exception("Failed to delete room after an error (room=%s)", room_name)
    finally:
        logger.info("RECORDING CONSENT at the end of the call: %s (room=%s)", rec.summary(), room_name)  # build step 2 puts this in the report


if __name__ == "__main__":
    # re-sends any report n8n did not confirm (the heartbeat that the inbound copy also sends from this loop needs its own n8n branch: build step 4/5)
    threading.Thread(target=outbox.outbox_replay_loop, name="outbound-outbox-replay", daemon=True).start()
    cli.run_app(server)
