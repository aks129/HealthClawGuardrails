"""Reading HealthClaw's AppointmentBrief (a FHIR Basic resource).

Shared by the /brief page (careagents/app.py) and the agent's
`appointment_brief` tool (careagents/agent.py), so the two cannot read the
same brief differently. Nothing here fetches or stores: callers pass the
resource the engine returned.
"""

from __future__ import annotations

import json

SECTION_PREFIX = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"

# Mirrors r6.brief.engine.CARE_GAPS_OK. CareAgents talks to HealthClaw over
# HTTP and imports nothing from it, so the string is repeated rather than
# shared.
CARE_GAPS_OK = "ok"


def parse_sections(resource: dict) -> dict[str, list[dict]]:
    """Deserialize a FHIR Basic AppointmentBrief into section→field lists.

    Each section is a list of dicts with keys: label, value, sourceType, sourceId.
    Returns {} on any parse error so the template always gets a plain dict —
    empty sections render as 'not available from connected records'.
    """
    out: dict[str, list[dict]] = {}
    try:
        for ext in resource.get("extension", []):
            url = ext.get("url", "")
            if not url.startswith(SECTION_PREFIX):
                continue
            name = url[len(SECTION_PREFIX):]
            fields = []
            for fe in ext.get("extension", []):
                raw = fe.get("valueString")
                if raw:
                    try:
                        fields.append(json.loads(raw))
                    except (ValueError, TypeError):
                        pass
            out[name] = fields
    except (AttributeError, TypeError):
        pass
    return out


def care_gaps_marker(resource: dict | None, key: str) -> str:
    """One `status`/`reason` sub-extension of the brief's care-gaps section,
    or "" when the brief, the section or the marker is missing or unreadable."""
    care_gaps_url = SECTION_PREFIX + "care-gaps"
    try:
        for ext in (resource or {}).get("extension", []):
            if ext.get("url") != care_gaps_url:
                continue
            for sub in ext.get("extension", []):
                if sub.get("url") == key:
                    return sub.get("valueString") or ""
    except (AttributeError, TypeError):
        pass
    return ""
