"""Appointment Brief engine — pure function, no I/O.

Converts FHIR R4 resource lists into a structured BriefResult. Each field
carries a source citation (resourceType + id) so the UI can link back to the
exact record that produced the claim.

Design rules (enforced by tests):
1. Unknown is never absent. Empty input lists produce empty section lists,
   not "none" strings. The template decides how to render an empty section.
2. No inference. Every output field projects a literal value from the record.
   The engine never derives a clinical conclusion the record doesn't state.
3. Read-only. The engine has no write path. It never modifies the input dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BriefField:
    label: str        # patient-facing label ("Hypertension")
    value: str        # patient-facing value ("Active since 2021-03")
    source_type: str  # FHIR resourceType ("Condition")
    source_id: str    # FHIR resource.id — used to build the "View source" link


@dataclass
class BriefResult:
    problems: list[BriefField] = field(default_factory=list)
    medications: list[BriefField] = field(default_factory=list)
    labs: list[BriefField] = field(default_factory=list)
    care_gaps: list[BriefField] = field(default_factory=list)
    visits: list[BriefField] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _code_text(resource: dict) -> str:
    """Best human-readable label from a FHIR code element."""
    code = resource.get("code") or resource.get("medicationCodeableConcept") or {}
    text = code.get("text", "")
    if text:
        return text
    for coding in code.get("coding", []):
        if coding.get("display"):
            return coding["display"]
    return "Unknown"


def _onset_display(resource: dict) -> str:
    """Best onset date string from a Condition resource."""
    onset = (
        resource.get("onsetDateTime")
        or resource.get("onsetPeriod", {}).get("start")
        or resource.get("recordedDate")
        or ""
    )
    if onset and len(onset) >= 7:
        return onset[:7]  # YYYY-MM
    return ""


def _effective_display(resource: dict) -> str:
    """Best effective date string from an Observation resource."""
    dt = (
        resource.get("effectiveDateTime")
        or resource.get("effectivePeriod", {}).get("start")
        or resource.get("issued")
        or ""
    )
    if dt and len(dt) >= 10:
        return dt[:10]  # YYYY-MM-DD
    return ""


def _obs_value(obs: dict) -> str:
    """Human-readable value + unit from an Observation."""
    if "valueQuantity" in obs:
        vq = obs["valueQuantity"]
        value = vq.get("value", "")
        unit = vq.get("unit") or vq.get("code", "")
        return f"{value} {unit}".strip() if value != "" else ""
    if "valueString" in obs:
        return obs["valueString"]
    if "valueCodeableConcept" in obs:
        cc = obs["valueCodeableConcept"]
        return cc.get("text") or next(
            (c.get("display", "") for c in cc.get("coding", [])), ""
        )
    return ""


def _medication_display(resource: dict) -> str:
    """Human-readable medication name from a MedicationRequest."""
    # Try medicationCodeableConcept first, then medicationReference display
    cc = resource.get("medicationCodeableConcept")
    if cc:
        text = cc.get("text", "")
        if text:
            return text
        for coding in cc.get("coding", []):
            if coding.get("display"):
                return coding["display"]
    ref = resource.get("medicationReference", {})
    return ref.get("display", "Unknown medication")


def _encounter_display(enc: dict) -> str:
    """Human-readable label for an Encounter."""
    type_text = ""
    for t in enc.get("type", []):
        type_text = t.get("text", "")
        if not type_text:
            for coding in t.get("coding", []):
                type_text = coding.get("display", "")
        if type_text:
            break
    date = ""
    period = enc.get("period", {})
    date = period.get("start") or enc.get("meta", {}).get("lastUpdated", "")
    if date and len(date) >= 10:
        date = date[:10]
    return (type_text or "Visit") + (f" ({date})" if date else "")


# ---------------------------------------------------------------------------
# Section builders (pure, exported for unit testing)
# ---------------------------------------------------------------------------

_ACTIVE_CONDITION_STATUSES = {"active", "recurrence", "relapse"}


def build_problems(conditions: list[dict]) -> list[BriefField]:
    """Active problem list from Condition resources."""
    out = []
    for c in conditions:
        status = (c.get("clinicalStatus") or {}).get("coding", [{}])[0].get(
            "code", ""
        )
        if status not in _ACTIVE_CONDITION_STATUSES:
            continue
        label = _code_text(c)
        onset = _onset_display(c)
        value = f"Active{f' since {onset}' if onset else ''}"
        out.append(BriefField(
            label=label,
            value=value,
            source_type="Condition",
            source_id=c.get("id", ""),
        ))
    return out


_ACTIVE_MED_STATUSES = {"active", "intended", "unknown"}


def build_medications(medication_requests: list[dict]) -> list[BriefField]:
    """Current medication list from MedicationRequest resources."""
    out = []
    for m in medication_requests:
        status = m.get("status", "")
        if status not in _ACTIVE_MED_STATUSES:
            continue
        label = _medication_display(m)
        dosage = ""
        for d in m.get("dosageInstruction", []):
            dosage = d.get("text", "")
            if dosage:
                break
        out.append(BriefField(
            label=label,
            value=dosage or "See record for dosage",
            source_type="MedicationRequest",
            source_id=m.get("id", ""),
        ))
    return out


_MAX_LABS = 10


def build_labs(observations: list[dict]) -> list[BriefField]:
    """Most recent lab results from Observation resources (capped at 10)."""

    def _sort_key(obs: dict) -> str:
        return (
            obs.get("effectiveDateTime")
            or obs.get("effectivePeriod", {}).get("start")
            or obs.get("issued")
            or ""
        )

    sorted_obs = sorted(observations, key=_sort_key, reverse=True)
    out = []
    for obs in sorted_obs[:_MAX_LABS]:
        label = _code_text(obs)
        value = _obs_value(obs)
        date = _effective_display(obs)
        display_value = value + (f" ({date})" if date else "") if value else (date or "See record")
        out.append(BriefField(
            label=label,
            value=display_value,
            source_type="Observation",
            source_id=obs.get("id", ""),
        ))
    return out


def build_care_gaps(care_gap_result: dict) -> list[BriefField]:
    """Open preventive-care gaps from the $care-gaps output."""
    consumer = care_gap_result.get("consumer") or {}
    due_items = consumer.get("due") or []
    out = []
    for item in due_items:
        label = item.get("measure") or item.get("name") or "Screening"
        value = item.get("reason") or item.get("status") or "Due"
        out.append(BriefField(
            label=label,
            value=value,
            source_type="MeasureReport",
            source_id=item.get("id") or item.get("measure_id", ""),
        ))
    return out


def build_visits(encounters: list[dict]) -> list[BriefField]:
    """Recent and upcoming encounters from Encounter resources."""

    def _sort_key(enc: dict) -> str:
        period = enc.get("period", {})
        return period.get("start") or enc.get("meta", {}).get("lastUpdated", "")

    sorted_encs = sorted(encounters, key=_sort_key, reverse=True)
    out = []
    for enc in sorted_encs[:5]:
        label = _encounter_display(enc)
        status = enc.get("status", "")
        out.append(BriefField(
            label=label,
            value=status.capitalize() if status else "Scheduled",
            source_type="Encounter",
            source_id=enc.get("id", ""),
        ))
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_brief(
    conditions: list[dict],
    medication_requests: list[dict],
    observations: list[dict],
    encounters: list[dict],
    care_gap_result: dict,
) -> BriefResult:
    """Generate a structured appointment brief from FHIR resource lists.

    All inputs may be empty lists / empty dicts. Empty inputs produce empty
    section lists — never error strings. The unknown-never-absent rule is
    enforced: this function never returns a string like 'none' or 'no records'.
    """
    return BriefResult(
        problems=build_problems(conditions),
        medications=build_medications(medication_requests),
        labs=build_labs(observations),
        care_gaps=build_care_gaps(care_gap_result),
        visits=build_visits(encounters),
    )
