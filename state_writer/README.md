# Desert Comfort State Writer (sub-workflow)

Built 2026-10-02, step 5 of the build plan.
n8n workflow **"Desert Comfort State Writer"**, published, called by other workflows through an *Execute Sub-workflow* node.
No secrets in this folder: it holds code only.

## What it does
Takes ONE event about ONE contact, reads the contact from GHL (never from search, which lags), decides, writes, and records it.

| Input field | Meaning |
|---|---|
| `event` | canonical event: `booked_standard`, `booked_emergency`, `qualified_not_booked`, `out_of_area`, `no_meaningful_conversation`, `dead_air`, `transferred`, `do_not_call`, `appt_showed`, `appt_completed`, `appt_cancelled`, `appt_noshow`, `appt_rescheduled` |
| `source` | `end_of_call` or `appointment_status` |
| `contact_id` | GHL contact id (required, validated) |
| `event_id` | unique per event (call job id, or appointment id + status); makes retries safe |
| `phone`, `occurred_at`, `detail` | optional; for appointment events `detail.appointment = {id, calendar_id, start_time, status}` |

Decisions it can return: `applied`, `no_change`, `dnd_set`, `skipped_not_allowed`, `skipped_has_future_appointment`, `ignored_other_calendar`, `duplicate_ignored`, and the alert cases `unknown_event`, `blocked_unmapped_stage`, `invalid_input`, `contact_not_found`, `config_missing`, `appointments_unreadable`.

## Rules baked in (all configured in n8n Data Tables, not in code)
- **Unknown means stop:** an unknown event, a stage value nobody defined, bad input or a missing config key means NO write and a Slack alert.
- **Forward-only stages:** call outcomes never demote `booked` or `served`; only appointment events move a contact back (cancel or no-show returns booked to qualified, and only if no other live future appointment exists).
- **Do-Not-Disturb is only ever set here, never cleared:** `do_not_call` sets Call and SMS and leaves the stage alone; a later booking changes the stage and leaves Do-Not-Disturb intact.
- **Idempotent:** Redis lease (120 s) plus a 7-day done mark per event; a retryable failure (contact or appointments unreadable) releases the lease so a prompt retry is processed.
- **Fail-open claims:** a Redis outage lets the event through rather than dropping it.
- **Legacy contacts:** a blank stage is derived from the old `lead_status_desertcomfort` field. When the old status says something about the journey (booked, qualified, out of area) the derived stage is SAVED on first touch whatever the decision is (skipped, DND only, no change), because the call's own status update overwrites the old status right afterwards (found by the step 6 live test). A brand-new contact with no old status is left untouched.

## Data Tables (all prefixed `desertcomfort_`)
`desertcomfort_contact_events` (the ledger), `desertcomfort_config` (field ids, calendars, timezone, DND channels), `desertcomfort_stage_transitions` (the allowed moves; edit a row to change behavior), `desertcomfort_journey_map` (the client-facing map, default). Column schemas cannot change after creation.

## Files
- `decide.js`, `normalize_event.js`, `normalize_claim.js`, `aggregate_config.js`, `aggregate_transitions.js`: the exact code that runs in the workflow's Code nodes.

## Verified (2026-10-02, real systems)
A live integration test (not included in this repo) passed 20 of 20 cases, covering duplicates, opt-out surviving a later booking, booked to served with the last service date, repeat service, cancel with and without another live appointment (including a real one on the calendar), another build's calendar ignored, unknown event, bad stage value, unreadable contact and its retry, invalid input, and two simultaneous identical events (exactly one applied).

## Wired in (Phase 1 step 6, 2026-10-02)
Called by the live call-end chain of `Desert Comfort LiveKit` through `Call State Writer Desert Comfort LiveKit` (Execute Sub-workflow, `continueErrorOutput`): the call's outcome becomes the event, `job_id` (or the room name when missing) becomes the event id, the contact id comes from the upsert. Runs before the outcome chain. If it fails, the report still completes and the admin gets a "State Writer failed" Slack alert. Dead air and calls with no usable caller number never reach it. The appointment-status receiver calls the same workflow with `source = appointment_status`.

A sub-workflow must be PUBLISHED to run: n8n refuses to activate a parent that points at an unpublished sub-workflow, and a deactivated one fails at run time with "Workflow is not active and cannot be executed".
