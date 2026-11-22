# Voicemail / silent-line detection for the outbound agent - research and design (2026-10-27)

Why this exists: dial 4 (2026-10-26) left a ~50 s message on Admin's voicemail because LiveKit's detector (AMD) gave up with `uncertain` / `no_speech_timeout` and our old rule treated that as a person. Admin's requirement: the agent must never talk into a voicemail, and the remaining limits (silent human hung up on; a greeting the detector calls "human" with no tell-tale words; unknown cause of the miss) must be designed out, not accepted.

## Real data we already have (read-only analysis of `recordings/*_caller_in.wav`, no API keys used)
| Call | What the callee line did | Source |
|---|---|---|
| proof dial 2 (voicemail pickup) | silence while ringing, then ONE continuous speech burst of **6.2 s** (11.8 s to 18.0 s into the recording), level about 3700, starting almost at the moment of answer | carrier voicemail on Admin's VoIP line |
| proof dial 1 (person) | "Hello?" **1.2 s** burst (18.2 to 19.4 s), a pause, later a 1.6 s burst | Admin |
Reading: a voicemail greeting is a long unbroken burst; a person's first utterance is a short burst followed by a pause (they wait for us). Two samples only: a signature, not yet a measured rule (see "What we must still measure").

## Facts from the docs and vendors
- A voicemail answers with 200 OK, so SIP signalling cannot tell it from a person (LiveKit docs, community thread).
- A voicemail records only after the beep. Anything said before the beep is not saved. Dial 4's message was left because Alice started speaking about 10 s after the answer, i.e. after the greeting and the beep. Hanging up before the beep leaves nothing.
- LiveKit AMD (Python, livekit-agents 1.5.9+; ours is 1.8.3): LLM classifier on the first utterance; categories human / machine-vm / machine-unavailable / machine-ivr / uncertain; median 840 ms; own benchmark voicemail F1 97.3%, human F1 95.7%; runs once at the start; default models are LiveKit Cloud ones, ours is a self-hosted custom pairing that is NOT on LiveKit's evaluated list. Its `speech=0.0s` and empty transcript in dial 4 mean it never received usable text from its speech-to-text.
- Telnyx **premium AMD** (Call Control `POST /v2/calls` with `answering_machine_detection: premium`): results human_residence / human_business / machine / silence / fax_detected / not_sure; separate beep webhook (`beep_detected`); default 30 s analysis timeout; no stated latency, accuracy numbers or price in the docs. It is a Call Control feature: it does not apply to calls that LiveKit places over the SIP trunk, so using it means changing the dialing architecture (Telnyx places the call, then hands a confirmed person to LiveKit).
- Vapi / Retell guidance: combine audio analysis (beep) with transcript analysis; start checking early (1 to 2 s) and keep re-checking (every 2.5 s, up to 8 to 10 times) to avoid false negatives; false positives (hanging up on a person) are the costlier error for compliance and lost leads; most greetings run 10 to 20 s; vendors' accuracy claims should be measured on your own traffic.

## Why the old design failed (and the limits Admin wants removed)
1. One detector, one signal (text from one speech-to-text). If that path returns nothing, nothing else vetoes. -> Fix: independent signals, any one of which can end the call.
2. "Uncertain means person." -> Already reversed: only a positive human verdict speaks.
3. Silent real person hung up on after 5 s. -> Fix: a one-word probe (see layer 5).
4. A greeting the classifier calls "human" with none of our phrases. -> Fix: burst-length and beep layers do not depend on words.
5. Unknown cause of the miss. -> Fix: replay the recorded voicemail audio through the real speech-to-text path offline (needs Admin's OK to use the OpenAI key for a few cents), and keep every call's audio as a regression corpus.

## Proposed design: five independent layers, fail closed
Principle: Alice never speaks until a short burst and a pause have been heard AND the classifier says human; ANY machine signal ends the call at once, skipping speech.
1. **Burst-length (audio only, free, no network).** Measure the callee's first speech burst straight from the incoming audio frames (energy with a short hold so pauses under 0.5 s do not split it). A burst of 3.5 s or more with no real pause = machine, hang up. A burst of 2.5 s or less followed by a pause of 0.6 s or more = person-like. Works even when transcription fails.
2. **Voicemail words (text).** Already built and deployed (`is_voicemail_text`), fed from every transcript source (AMD's text and the model's own input transcription).
3. **Classifier (LiveKit AMD, text).** Must say `human` for Alice to speak. Its speech-to-text gets replay-tested first and swapped if it is the weak link (LiveKit's evaluated options: Deepgram nova-3, AssemblyAI, Cartesia; a new plugin needs Admin's security review and approval first).
4. **Beep detector (audio only, free).** A steady tone near 1 kHz for 150 ms or more at any moment before or during Alice's speech ends the call at once. Covers the short-greeting voicemail ("leave a message" then beep) that layers 1 to 3 could miss.
5. **Silent-line probe.** If nothing is heard for 4 s after the answer, Alice says one word, "Hello?", and waits 4 s more. Any person-like reply moves on to the checks above; silence or a machine signal hangs up. Because a voicemail records only after the beep and the probe is one word spoken early, nothing usable is left; a person who waited for us to speak is no longer lost.
Every hang-up on a machine or silence writes a reason to the log and an outcome the n8n side can use (voicemail / no_answer) later (stage 6/7), so it never counts as a conversation.

## What we must still measure (honest limits)
- Layer thresholds (3.5 s, 2.5 s, 0.6 s, tone length) are set from two recordings. They must be tuned on a real corpus: Admin's carrier voicemail, a few other voicemail styles, and several person pickups. The test is a replay harness over saved audio (offline, repeatable), run before every change.
- No detector reaches 100%. The design goal is that failure costs a retry (the 5-minute retry exists in the real flow), never a message left on a voicemail.
- Carrier voicemails that record from the start of the call (not from the beep) exist; layers 1 and 4 still end the call in about 3.5 s, but a short fragment could be saved. This cannot be removed by software on our side.
- Telnyx premium AMD as an extra outer gate is possible but changes the architecture and adds latency before the person hears Alice; not recommended unless the layers above fail the corpus test.

## Build order (each step tested offline first)
1. Turn the two recordings into fixtures (energy envelopes, no raw audio kept in the repo) and write the replay harness. 2. Pure burst tracker + beep detector + decision function with tests. 3. Wire into the outbound agent (any machine signal ends the call through the single `end_call`). 4. Replay the voicemail audio through the real speech-to-text (needs OK to use the OpenAI key). 5. Real calls: decline (voicemail), pickup, silent pickup, plus a second voicemail style if available. 6. Add every new recording to the corpus.

## Step 4 result - speech-to-text replay (2026-10-27, Admin's "go"; OpenAI key used on the VPS only, a few cents, never printed)
Tool: `replay_stt.py <caller_in.wav>` (run on the VPS; replays a saved callee-side recording through the plain API and through the streaming path the detector uses, `openai.STT(model="gpt-4o-mini-transcribe", use_realtime=True)`, at real-time pace).
- Voicemail (proof2): the streaming path returned the FULL greeting, "The person you are calling is unable to take your call. Please leave a message after the tone." So the speech-to-text is NOT blind to this greeting; "STT returned nothing" is disproven for this recording.
- But the text only arrives AFTER the burst ends: speech started at 12.4 s, ended 18.4 s, final text 19.0 s (about 6.6 s after speech began). A words-based layer cannot beat the greeting length; the audio burst-length layer (3.5 s) is faster than any words layer.
- Person (proof1): "Hello, how are you doing?" arrives about 0.7 s after he stops speaking.
- Still unexplained: why the live detector gave `uncertain / no_speech_timeout` on dial 4 (old build: no recordings, no 5 s wait). The replay does not run the detector itself (its own VAD and timers), only the speech-to-text. Next real run has recordings and the new timer.
- Telnyx check (same day): no AMD setting exists on the SIP (FQDN) connection or the outbound voice profile; Telnyx AMD is documented only for Voice API (Call Control) dial and TeXML Dial. LiveKit SIP participant attributes carry no machine flag (sip.callStatus: dialing / ringing / automation = outbound DTMF still being sent / active / hangup). Possible Telnyx route: connection setting "park outbound calls" + Call Control dial with AMD + bridge; unproven, adds a call leg and delay.

## Telnyx short-duration-call (SDC) penalty - changes the design (2026-10-27, Admin supplied the article)
Source: support.telnyx.com/en/articles/1130707-what-are-short-duration-calls
- An SDC is an OUTBOUND call of 6 seconds or less. Telnyx allows 15% of traffic to be SDCs (connected SDCs / all connected calls, UTC month). Over 15% at month end = "additional charges" on ALL of that month's SDCs, not only the excess. Email alert when over 15% at any point. The page gives no domestic amount (international SDCs: $0.01 per call). No exemptions listed. Telnyx "does not support use cases that require SDCs".
- A voicemail answers with 200 OK, so it is a CONNECTED call. Hanging up on a voicemail in under 6 s makes it an SDC. The earlier "cut at 3.5 s" layer, the 5 s silence hang-up, and any instant voicemail-words hang-up would each create SDCs. At volume, voicemails are a large share of outbound dials.
- Corrected rule (replaces "end the call at once"): a machine signal makes the agent SILENT at once (nothing it says can be recorded), and any hang-up we start on a non-conversation (machine, silence) waits until the call is at least 7.5 s old (6 s + margin; measured from answer). One constant, enforced in the single end_call path. A greeting is still unfinished at 7.5 s in most cases, so nothing is recorded (a message starts only after the beep).
- Cost: ~7.5 s of line time per voicemail, a fraction of a cent. Not "immediate", but silent, and the penalty is avoided.
- A real person who hangs up within 6 s of answering is also an SDC; cannot be controlled.
- Tests we dial (decline tests, short proof calls) count too; irrelevant at test volume, matters in production.

## LiveKit AMD blog (livekit.com/blog/answering-machine-detection) - what it adds
- Design: fast short-circuit rule, then an LLM classifies the first transcript; transcript is "the strongest signal"; median time to verdict 840 ms from session start; categories human / machine-ivr / machine-vm / machine-unavailable / uncertain; benchmark F1: human 95.7, IVR 98.2, voicemail 97.3 (internal dataset).
- It treats a short utterance with no transcript (one-word hello, a beep, silence) as HUMAN and says wrongly calling a human a machine is the worst failure. Our policy is the opposite for the `uncertain` / no-transcript case (only a positive human verdict speaks) because the cost of talking into a voicemail is a recorded message and a lost lead; the replay shows the verdict for a real greeting arrives when the greeting's text arrives.
- Evaluated model pairs: Cloud default google/gemini-3.1-flash-lite + cartesia/ink-whisper; also openai/gpt-4.1 + assemblyai/universal-streaming-multilingual. Ours (gpt-4.1-mini + gpt-4o-mini-transcribe) is not on the evaluated list (the log warns of this). A natural, no-new-vendor change: gpt-4.1 as the detector LLM.
- Voicemail handling in the article: leave a message after the beep / prompt end, or ctx.shutdown() for an unavailable mailbox. It says nothing about SDCs or hang-up timing. Because the verdict for a ~6 s greeting arrives when the text does (~6.6 s after the greeting starts), a verdict-then-hang-up flow lands near or after the 6 s line by itself, but short greetings and early hang-ups would not.

## Final result 2026-10-27 (dials 5-11, all placed by Admin)
The five-layer design was built, deployed and live-tested. Voicemail dials (5, 9): nothing landed in the voicemail, the agent stayed silent, the hang-up came at 7.5 s or later. Pickups (6, 10): normal conversation. Silent pickups (8, 11): opener, 8 s of silence, call ends. Two things changed the design on the way: Telnyx's short-duration-call rule (no hang-up we start before 7.5 s) and the finding that LiveKit AMD hears speech only through the session VAD, which a speech-to-speech session lacks (fixed by adding Silero VAD). Chosen: speak-first after 1.5 s of callee silence (option 1), AMD LLM Gemini 3.1 Flash Lite via OpenRouter, STT Deepgram nova-3. Still open: the thresholds rest on one voicemail and one person recording; a greeting under 3.5 s could let a few words of the opener reach the recording; logging every `sip.*` attribute change was proposed and not approved.
