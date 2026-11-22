# Stage 2 research - outbound calling on LiveKit (speed to lead, Desert Comfort)

Written 2026-10-26. Sources: LiveKit docs (outbound calls, telephony overview, answering machine detection), LiveKit AMD blog post, a read-only check of the server's Python environment. Nothing was installed or changed.

## What the platform gives us
1. **Outbound dial.** The agent or n8n calls `CreateSIPParticipant` (`ctx.api.sip.create_sip_participant`) with a stored outbound trunk id (or an inline trunk), `sip_call_to`, `room_name`, `participant_identity`, `sip_number` (caller ID), `wait_until_answered`, optional `dtmf`, `play_dialtone`, `display_name`. Outbound trunk fields: `hostname` (docs example `sip.telnyx.com`), `auth_username`, `auth_password`, `destination_country`. Telnyx-specific steps are not in LiveKit's docs; they come from the Telnyx side (we already have an outbound voice profile and FQDN credentials from the inbound setup).
2. **Call outcome codes.** Answered = 200 OK, `sip.callStatus = active`. Rejected (486/603) = `USER_REJECTED`; no answer (408/480) = `USER_UNAVAILABLE`; carrier failure (5xx) = `SIP_TRUNK_FAILURE`. `AgentSession` closes itself only on `USER_REJECTED`, so we shut down explicitly for the other two. So "no answer" and "rejected" are known from signalling; only voicemail needs more.
3. **Voicemail = 200 OK.** SIP signalling cannot tell a person from a voicemail. LiveKit ships **AMD** (answering machine detection) in the core framework: `from livekit.agents import AMD`. Needs `livekit-agents` 1.5.9 or newer. **The VPS venv has 1.8.3 and `from livekit.agents import AMD` imports there (checked 2026-10-26, Python 3.10.12).**
4. **AMD behaviour.** Runs once, on the first thing the callee says; agent speech is paused while it runs. Categories: `human`, `machine-vm` (can leave a message), `machine-unavailable` (mailbox full/unset), `machine-ivr` (menu), `uncertain` (treat as human). Its own benchmark: median 840 ms, voicemail F1 97.3%, human F1 95.7%. It lists "calling a human a machine" as the worst failure and defaults to human on a lone "hello?". Settings: `human_speech_threshold` 2.5 s, `human_silence_threshold` 0.5 s, `machine_silence_threshold` 1.5 s, `no_speech_threshold` 10 s (clock starts at answer), `timeout` 20 s (effective ceiling about 2x).
5. **Outbound etiquette from the docs.** Let the callee speak first; call `session.start()` only after the callee answers, so nothing plays while the phone rings.

## Limits and unknowns (the honest part)
- **AMD with a speech-to-speech model is not proven.** The docs say AMD runs its own speech-to-text pipeline "so it can listen even when the session uses a realtime model", and tell you to verify. Our session uses GPT-Live-1 (audio only). Needs a real proof in Stage 4.
- **AMD's default models run on LiveKit Inference (LiveKit Cloud).** We are self-hosted. We would pass our own `llm` and `stt` instances. `livekit-plugins-openai` 1.8.3 is installed (it has LLM and STT); no Deepgram plugin on the VPS. OpenAI STT is not in LiveKit's evaluated list, so AMD logs a "not evaluated" warning and accuracy is not benchmarked for our pairing. Evaluated LLMs include `openai/gpt-4.1-mini`; evaluated STT are `cartesia/ink-whisper`, `assemblyai/universal-streaming-multilingual`, `deepgram/nova-3`. Adding any of those is a new third-party dependency and needs Admin's security review first.
- **AMD runs once, at the start only.** A voicemail that picks up late or a person who answers then hands the phone over is not caught. No cost figure in the docs; it adds one LLM and one speech-to-text call per outbound answer.
- **AMD can return `machine-ivr`** (auto IVR navigation is on by default in Python); for a homeowner's phone we should set `ivr_detection=False` and treat it as voicemail/no answer.
- **Carrier concurrency:** default 2 outbound channels on Telnyx (10 after verification); speed-to-lead bursts could exceed 2 simultaneous leads. Ask Telnyx or verify the account before real traffic (template appendix).
- **OpenAI's own SIP path cannot do outbound.** LiveKit is OpenAI's listed partner for GPT-Live, which is why this runs through LiveKit.
- **Caller ID and consent are not technical problems:** the outbound number must be one we own (`+12125550103`), and the calling-hours exemption plus AI-call consent wording go to an attorney for review.

## What this means for the build
- Voicemail detection is a solved shape (AMD), not a custom build, but needs a proof on GPT-Live and a choice of its LLM and STT (Stage 2 decision for Admin).
- The no-answer / voicemail / human split can mostly be decided in the agent right after pickup, then reported to n8n at call end exactly like inbound.
- Voicemail must not corrupt status (SunPath 2026-07-19 bug): treat `machine-*` and `USER_UNAVAILABLE` as the same "no conversation" class and write the flag before the call-end chain runs.

## Still to read before Stage 5 (prompt)
SunPath outbound `prompt.md` and its `attempt_context` mechanism, the Desert Comfort prompt variants in full, GHL automations on the Desert Comfort account.
