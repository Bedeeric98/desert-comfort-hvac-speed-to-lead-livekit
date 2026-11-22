// Decide State Change - Desert Comfort State Writer.
// Pure decision logic: reads everything through explicit node references, writes NOTHING itself.
// Rules: unknown means stop (alert, no write); forward-only stages; Do-Not-Disturb is only ever SET here, never
// cleared; booked/served are never demoted by call outcomes; a cancelled appointment demotes booked only when no
// other live future appointment exists.
const get = (name) => { try { return $(name).first().json; } catch (e) { return null; } };

const CANON = ['contact', 'lead', 'qualified', 'booked', 'served', 'dormant', 'disqualified'];
const ev = get('Normalize Event') || {};
const cfgRows = ((get('Aggregate Config') || {}).rows) || [];
const trans = ((get('Aggregate Transitions') || {}).rows) || [];
const cfg = {};
for (const r of cfgRows) { if (r && r.key) cfg[String(r.key).trim()] = r.value; }

const csv = (s) => String(s === undefined || s === null ? '' : s).split(',').map((x) => x.trim()).filter(Boolean);
const tz = cfg.timezone || 'UTC';
const calIds = csv(cfg.appointment_calendar_ids);
const dndChannels = csv(cfg.dnd_channels_on_do_not_call);
const legacyBooked = csv(cfg.legacy_booked_statuses);

const result = {
  decision: '', reason: '', from_stage: '', to_stage: '', write_stage: false, set_dnd: false,
  stage_body: null, dnd_body: null, last_service_date: '', alert: false, alert_text: '', retryable: false,
  event: ev.event || '', event_id: ev.event_id || '', source: ev.source || '', phone: ev.phone || '', contact_id: ev.contact_id || '',
};
// A contact whose stage was only DERIVED from a meaningful old lead status (booked / qualified / out of area) must have that
// stage saved on first touch whatever the decision is: the call's own update overwrites the old status right after, and the
// evidence (for example "this is a booked customer") would be lost for good. Brand-new contacts with no old status are left alone.
let legacyStage = '';
const PERSIST_LEGACY_ON = ['no_change', 'dnd_set', 'skipped_not_allowed', 'skipped_has_future_appointment'];
const done = (decision, reason, extra) => {
  Object.assign(result, { decision, reason }, extra || {});
  if (legacyStage && !result.write_stage && !result.alert && PERSIST_LEGACY_ON.includes(decision)) {
    result.write_stage = true;
    result.stage_body = { customFields: [{ key: cfg.stage_field_key, fieldValue: legacyStage }] };
    result.reason += ` (legacy stage '${legacyStage}' saved)`;
  }
  return [{ json: result }];
};
const alertWith = (decision, reason, text, retryable) => done(decision, reason, { alert: true, alert_text: text, retryable: Boolean(retryable) });

if (ev.invalid) return alertWith('invalid_input', ev.invalid, `State Writer got an invalid event: ${ev.invalid}.`);

const missingCfg = ['location_id', 'stage_field_key', 'stage_field_id', 'last_service_date_field_key'].filter((k) => !cfg[k]);
if (missingCfg.length) {
  return alertWith('config_missing', `config keys missing: ${missingCfg.join(', ')}`,
    `State Writer config is missing ${missingCfg.join(', ')}. Event ${ev.event} for contact ${ev.contact_id} NOT applied (it will be retried).`, true);
}

const cr = get('Get Contact') || {};
if (cr.statusCode !== 200 || !cr.body || !cr.body.contact) {
  return alertWith('contact_not_found', `GET contact returned ${cr.statusCode}`,
    `State Writer could not read contact ${ev.contact_id} (HTTP ${cr.statusCode}). Event ${ev.event} NOT applied (it will be retried).`, true);
}
const contact = cr.body.contact;
const fields = contact.customFields || [];
const fieldVal = (id) => { const f = fields.find((x) => x.id === id); return f ? (f.value !== undefined ? f.value : f.fieldValue) : null; };
let stage = String(fieldVal(cfg.stage_field_id) || '').trim().toLowerCase();
const leadStatus = String(fieldVal(cfg.lead_status_field_id) || '').trim().toLowerCase();

// Contacts created before the stage field existed: derive a starting stage from the old status field.
let derived = false;
if (!stage) {
  derived = true;
  if (legacyBooked.includes(leadStatus)) stage = 'booked';
  else if (leadStatus === 'qualified_not_booked') stage = 'qualified';
  else if (leadStatus === 'out_of_area') stage = 'disqualified';
  else stage = 'contact';
  if (stage !== 'contact') legacyStage = stage;
}
result.from_stage = stage;

if (!CANON.includes(stage)) {
  return alertWith('blocked_unmapped_stage', `stage value '${stage}' is not a known stage`,
    `Contact ${ev.contact_id} has stage '${stage}', which is not a known stage. Event ${ev.event} NOT applied. Someone may have edited the stage field.`);
}

const row = trans.find((t) => t && String(t.event).trim() === ev.event);
if (!row) {
  return alertWith('unknown_event', `no rule for event '${ev.event}'`, `State Writer has no rule for event '${ev.event}' (contact ${ev.contact_id}). Nothing applied.`);
}

// Appointment events: only the configured calendars drive state (other builds share the sub-account).
const appt = (ev.detail && ev.detail.appointment) || null;
if (ev.source === 'appointment_status' && appt && appt.calendar_id && calIds.length && !calIds.includes(appt.calendar_id)) {
  return done('ignored_other_calendar', 'appointment is on a calendar this client does not use');
}

const action = String(row.action || '').trim();
if (action === 'none') return done('no_change', 'known event, no stage change');

if (action === 'dnd') {
  const dnd = contact.dndSettings || {};
  const need = dndChannels.filter((c) => !(dnd[c] && dnd[c].status === 'active'));
  if (!need.length) return done('no_change', 'Do-Not-Disturb already active on all required channels');
  const body = { dndSettings: {} };
  for (const c of dndChannels) body.dndSettings[c] = { status: 'active', message: 'ConvertEcho: do-not-call outcome', code: 'CONVERTECHO_DNC' };
  return done('dnd_set', `Do-Not-Disturb set on ${dndChannels.join(' and ')}`, { set_dnd: true, dnd_body: body });
}

if (action !== 'stage') {
  return alertWith('unknown_event', `rule for '${ev.event}' has unknown action '${action}'`, `Rule for '${ev.event}' has unknown action '${action}'. Nothing applied.`);
}

const toStage = String(row.to_stage || '').trim();
if (!CANON.includes(toStage)) {
  return alertWith('unknown_event', `rule for '${ev.event}' targets unknown stage '${toStage}'`, `Rule for '${ev.event}' targets unknown stage '${toStage}'. Nothing applied.`);
}
const from = csv(row.from_stages);
if (!(from.includes('*') || from.includes(stage))) {
  return done('skipped_not_allowed', `'${ev.event}' is not allowed from stage '${stage}' (would have gone to '${toStage}')`, { to_stage: stage });
}

// Conditional demotion: only when no other live future appointment exists (booked means a live future appointment).
if (String(row.condition || '').trim() === 'no_future_appointment') {
  const ar = get('Get Contact Appointments') || {};
  if (ar.statusCode !== 200) {
    return alertWith('appointments_unreadable', `GET appointments returned ${ar.statusCode}`,
      `State Writer could not read appointments for contact ${ev.contact_id} (HTTP ${ar.statusCode}). Not demoting on '${ev.event}' (it will be retried).`, true);
  }
  const events = (ar.body && ar.body.events) || [];
  const nowMs = Date.parse(ev.occurred_at) || Date.now();
  const dead = ['cancelled', 'canceled', 'noshow', 'invalid'];
  const live = events.filter((a) =>
    (!calIds.length || calIds.includes(a.calendarId)) &&
    Date.parse(a.startTime) > nowMs &&
    !dead.includes(String(a.appointmentStatus || a.appoinmentStatus || a.status || '').toLowerCase().replace(/[^a-z]/g, '')) &&
    !(appt && appt.id && a.id === appt.id));
  if (live.length) return done('skipped_has_future_appointment', `contact still has ${live.length} live future appointment(s), stays ${stage}`, { to_stage: stage });
}

// Last service date: written whenever a contact becomes (or already is) served from a visit event.
let lastDate = '';
if (toStage === 'served') {
  const when = (appt && appt.start_time) || ev.occurred_at || new Date().toISOString();
  const d = new Date(when);
  lastDate = isNaN(d.getTime()) ? '' : d.toLocaleDateString('en-CA', { timeZone: tz });
}

const needStage = stage !== toStage || derived;
if (!needStage && !lastDate) return done('no_change', `already ${toStage}`, { to_stage: toStage });

const cf = [];
if (needStage) cf.push({ key: cfg.stage_field_key, fieldValue: toStage });
if (lastDate) cf.push({ key: cfg.last_service_date_field_key, fieldValue: lastDate });
return done('applied', needStage ? `${stage} -> ${toStage} on '${ev.event}'` : `stays ${toStage}, last service date updated on '${ev.event}'`,
  { to_stage: toStage, write_stage: true, stage_body: { customFields: cf }, last_service_date: lastDate });
