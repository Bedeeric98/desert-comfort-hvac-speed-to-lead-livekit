"""Loads and renders the Desert Comfort OUTBOUND (speed to lead) prompt from speed-to-lead-prompt.md. Pure: no LiveKit
imports. The outbound agent renders the lead's details into the prompt before the session
starts, exactly as the inbound agent does with receptionist-prompt.md.

The name helpers are copied from desert_comfort_agent.py on purpose: importing that module would start the inbound agent's setup.
"""

import re
from pathlib import Path

_DRAFT_PATH = Path(__file__).parent / "speed-to-lead-prompt.md"
# Synced from the Google Doc by sync_faq_doc.py on a timer (the same file the inbound agent reads); never fetched during a call.
_FAQ_CONTENT_PATH = Path(__file__).parent / "desert-comfort-faq-objections.txt"

NO_NAME_FOR_PROMPT = "(no name on file - do not guess or use a name)"
NOT_ON_FILE = "(not on file)"

_PLACEHOLDER_NAME_RE = re.compile(
    r"^(not captured|n/?a|none|null|nil|undefined|unknown|anonymous|no name|noname|name|first ?name|last ?name|customer|caller|client|guest)$", re.I)
_NAME_SHAPE_RE = re.compile(r"^[^\W\d_]+(?:[ .'’-]+[^\W\d_]+)*\.?$")  # letters in words, joined by space . ' or -; no digits or symbols


def clean_person_name(value) -> str:
    """The name if it is safe to speak, otherwise ''. A name field is data and may hold anything a lead typed into a form."""
    s = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    if len(s) < 2 or len(s) > 50 or len(s.split()) > 4:
        return ""
    if _PLACEHOLDER_NAME_RE.match(s) or not _NAME_SHAPE_RE.match(s):
        return ""
    return s


def load_faq_content() -> str:
    """The FAQ / objection answers, with the company acronym written the way it sounds (the model spelled out "HVAC" letter by letter on a
    real call, 2026-10-02). An empty string if the sync has never run: the prompt still works without it."""
    if not _FAQ_CONTENT_PATH.exists():
        return ""
    return re.sub(r"HVAC", "Aitch-vak", _FAQ_CONTENT_PATH.read_text(encoding="utf-8"))


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def lead_from(metadata, lookup) -> dict:
    """What the call knows about the lead: {first_name, last_name, address, attempt, texted}. The dispatch metadata (set by whatever starts the
    call) wins; the GHL lookup fills what the metadata does not carry. Names are cleaned (a form field can hold anything); the attempt number
    is clamped to 1 to 3 and is 1 when missing or unreadable, so a call never claims an earlier attempt it cannot prove."""
    metadata = metadata if isinstance(metadata, dict) else {}
    lookup = lookup if isinstance(lookup, dict) else {}
    try:
        attempt = min(3, max(1, int(metadata.get("attempt", 1))))
    except (TypeError, ValueError):
        attempt = 1
    # the lead's GHL id, set by the dial step and sent back in the report so n8n updates exactly this contact (blank if missing or not an id)
    contact_id = str(metadata.get("contact_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{10,40}", contact_id):
        contact_id = ""
    return {
        "first_name": clean_person_name(metadata.get("first_name")) or clean_person_name(lookup.get("first_name")),
        "last_name": clean_person_name(metadata.get("last_name")) or clean_person_name(lookup.get("last_name")),
        "address": str(metadata.get("address") or lookup.get("service_address") or "").strip(),
        "attempt": attempt,
        "texted": _truthy(metadata.get("texted", False)),
        "contact_id": contact_id,
    }


def reason_clause(attempt_number: int = 1, texted: bool = False) -> str:
    """Why we are calling, worded for the attempt. Built by code, never by the model: a first call must not mention an earlier one."""
    if attempt_number <= 1:
        return "calling about the form you filled out regarding a service request"
    if attempt_number == 2:
        return "following up on the call I tried a few minutes ago about the form you filled out regarding a service request"
    return "following up, since I tried you earlier" + (" and sent a text" if texted else "") + ", about the form you filled out regarding a service request"


def _text() -> str:
    return _DRAFT_PATH.read_text(encoding="utf-8")


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end].strip()


def load_voice_layer() -> str:
    return _slice(_text(), "## Voice Layer", "\n---")


def load_backend_layer() -> str:
    return _slice(_text(), "## Backend Layer", "\n---")


def load_opening_templates() -> tuple[str, str]:
    """(template with the lead's name, template without a usable name): the two blockquoted lines of the Opening Line section."""
    section = _slice(_text(), "## Opening Line", "## Voice Layer")
    quoted = [ln.strip() for ln in section.splitlines() if ln.strip().startswith(">")]
    lines = [q[q.index('"') + 1 : q.rindex('"')] for q in quoted]
    if len(lines) != 2:
        raise ValueError(f"Expected 2 blockquoted opening lines (named, unnamed), found {len(lines)}")
    return lines[0], lines[1]


def _render(template: str, **values: str) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value or "")
    return rendered


def render_opening(first_name, attempt_number: int = 1, texted: bool = False) -> str:
    """The ONE line Alice says first: greeting, digital-assistant disclosure, recording notice and the consent question together."""
    named, unnamed = load_opening_templates()
    name = clean_person_name(first_name)
    return _render(named if name else unnamed, first_name=name, reason_clause=reason_clause(attempt_number, texted))


def render_voice_layer(*, first_name, last_name, current_time_formatted: str, lead_phone_number: str, on_file_address: str,
                       faq_objection_content: str) -> str:
    return _render(
        load_voice_layer(),
        first_name=clean_person_name(first_name) or NO_NAME_FOR_PROMPT,
        last_name=clean_person_name(last_name) or NOT_ON_FILE,
        current_time_formatted=current_time_formatted,
        lead_phone_number=lead_phone_number,
        on_file_address=(on_file_address or "").strip() or NOT_ON_FILE,
        faq_objection_content=faq_objection_content,
    )


def render_backend_layer(*, current_time_formatted: str, lead_phone_number: str) -> str:
    return _render(load_backend_layer(), current_time_formatted=current_time_formatted, lead_phone_number=lead_phone_number)
