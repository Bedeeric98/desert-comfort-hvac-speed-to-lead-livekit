// Normalize Event - cleans and validates what the caller passed. Never throws: bad input becomes `invalid`.
const src = $input.first().json || {};
const clean = (v) => String(v === undefined || v === null ? '' : v).trim();

const event = clean(src.event).toLowerCase();
const source = clean(src.source) || 'unknown';
const contact_id = clean(src.contact_id);
const phone = clean(src.phone);
const occurred_at = clean(src.occurred_at) || new Date().toISOString();
const event_id = clean(src.event_id) || `${source}:${contact_id}:${event}:${Date.now()}`;

let detail = src.detail;
if (typeof detail === 'string') { try { detail = JSON.parse(detail); } catch (e) { detail = {}; } }
if (!detail || typeof detail !== 'object') detail = {};

let invalid = '';
if (!event) invalid = 'missing event';
else if (!contact_id) invalid = 'missing contact_id';
else if (!/^[A-Za-z0-9]{10,40}$/.test(contact_id)) invalid = 'contact_id is not a valid GHL id';
else if (Number.isNaN(Date.parse(occurred_at))) invalid = 'occurred_at is not a valid date';

const base = `dc-statewriter:${source}:${event_id}:${event}`;
return [{ json: { event, source, contact_id, phone, event_id, occurred_at, detail, invalid, done_key: `${base}:done`, lease_key: `${base}:lease` } }];
