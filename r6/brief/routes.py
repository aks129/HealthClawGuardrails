"""FHIR $appointment-brief — Flask handler.

Registered on r6_blueprint (under /r6/fhir) so it shares tenant enforcement.
Read-shaped: tenant-read-authenticated + AuditEvent.

Returns a FHIR Basic resource whose extension carries the structured brief.
Consuming clients (CareAgents) parse the extension rather than rendering the
raw FHIR shape.
"""

import json
import logging
from datetime import datetime, timezone

from flask import request, jsonify

from r6.access import TenantSource, tenant_from_request
from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.redaction import apply_redaction
from r6.brief.engine import (
    generate_brief,
    BriefResult,
    BriefField,
    CARE_GAPS_UNAVAILABLE,
    CARE_GAPS_REASON_ENGINE_ERROR,
)

logger = logging.getLogger(__name__)


def _resources_for(tenant_id: str, resource_type: str) -> list[dict]:
    rows = (
        R6Resource.query
        .filter(
            R6Resource.tenant_id == tenant_id,
            R6Resource.resource_type == resource_type,
            R6Resource.is_deleted.is_(False),
        )
        .all()
    )
    # Two defects, one line, and they MUST be fixed together (#391 + #382).
    #
    # `r.resource` is not an attribute of R6Resource, so this raised
    # AttributeError and the brief 500'd for any tenant holding data (#391).
    # The crash is currently the only thing preventing #382: there is no
    # `apply_redaction` anywhere in r6/brief/, and `_code_text` reads
    # `code.text` then `coding[].display` — the two fields CLAUDE.md names
    # because real feeds put patient names in them. Repairing the attribute
    # alone turns a 500 into a PHI leak into a document the patient and their
    # clinic receive.
    #
    # apply_redaction strips the upstream free text and then re-labels from
    # r6/terminology.py keyed by code (r6/redaction.py calls label_codings),
    # so the brief stays readable without any of it coming from the feed —
    # the same pair r6/routes.py and r6/labs/routes.py use.
    return [apply_redaction(r.to_fhir_json()) for r in rows]


def _field_to_dict(f: BriefField) -> dict:
    return {
        "label": f.label,
        "value": f.value,
        "sourceType": f.source_type,
        "sourceId": f.source_id,
    }


def _brief_to_extension(result: BriefResult) -> list[dict]:
    def _section(name: str, fields: list[BriefField],
                 extra: list[dict] | None = None) -> dict:
        return {
            "url": f"https://healthclaw.io/fhir/StructureDefinition/brief-section-{name}",
            "extension": [
                {"url": "field", "valueString": json.dumps(_field_to_dict(f))}
                for f in fields
            ] + (extra or []),
        }

    # Care gaps ship their state alongside their fields. Without it a client
    # sees an empty list and has no way to tell "nothing due" from "the
    # screening review never ran" (#381).
    care_gap_state = [{"url": "status", "valueString": result.care_gaps_status}]
    if result.care_gaps_reason:
        care_gap_state.append(
            {"url": "reason", "valueString": result.care_gaps_reason})

    return [
        _section("problems", result.problems),
        _section("medications", result.medications),
        _section("labs", result.labs),
        _section("lab-trends", result.lab_trends),
        _section("care-gaps", result.care_gaps, care_gap_state),
        _section("visits", result.visits),
    ]


def _care_gap_result(tenant_id: str) -> dict:
    """Run care-gaps evaluation; on failure say so rather than returning {}.

    The brief must not 500 when the screening rules break, but the old empty
    dict was worse than the crash: it reached the patient's page as an empty
    "preventive care due" section, which reads as a clean bill of health from
    a component that never ran (#381). The marker keeps the failure named all
    the way through. The reason is a fixed string — exception text can carry
    record content, and audit/log detail stays PHI-free.
    """
    try:
        from r6.caregaps.evaluate import evaluate_care_gaps
        from r6.caregaps.report import build_consumer_summary
        from r6.caregaps.routes import (
            patient_for, resolve_subject, subject_resources)

        # The subject, the demographics and the evidence come from the same
        # three functions Patient/$care-gaps uses, so the brief and the
        # operation cannot disagree about one person. The brief used to pass
        # patient=None, and every brief said the review had nothing to read
        # while $care-gaps found screenings due on the same record (#435).
        subject, state = resolve_subject(None, tenant_id)
        if state in ("no-patient", "ambiguous-patient"):
            # Not guessed and not evaluated: with no one identified, the rules
            # would report on nobody (#542). The caller reason says which.
            return {"consumer": build_consumer_summary(
                [], not_evaluated=state)}

        # Unredacted, as in $care-gaps: the rules read birthDate and gender.
        # It goes to the evaluator and nowhere else — the lines it produces
        # are the rules' own titles and messages, never record text.
        patient = patient_for(subject, tenant_id)
        if patient is None:
            return {"consumer": build_consumer_summary(
                [], not_evaluated="check-incomplete")}

        results = evaluate_care_gaps(
            patient=patient,
            conditions=subject_resources("Condition", subject, tenant_id),
            observations=subject_resources("Observation", subject, tenant_id),
            immunizations=subject_resources("Immunization", subject, tenant_id),
            procedures=subject_resources("Procedure", subject, tenant_id),
            as_of=datetime.now(timezone.utc).date().isoformat(),
        )
        consumer = build_consumer_summary(results)
        return {"consumer": consumer}
    except Exception as exc:
        logger.warning("appointment brief: care-gaps evaluation failed (%s)",
                       type(exc).__name__)
        return {"status": CARE_GAPS_UNAVAILABLE,
                "reason": CARE_GAPS_REASON_ENGINE_ERROR}


def _lab_trend_lines(tenant_id: str) -> list[dict]:
    """The creatinine trend sentence for the tenant's one Patient, or [].

    Trends need one person's history, so the subject is the one the
    care-gaps section resolves, and only that Patient's Observations are
    compared: the same rule `Observation/$interpret?subject=` follows
    (r6/labs/routes.py). No Patient or more than one is [], not a guess.

    Read unredacted, as $interpret reads them: the check needs values and
    times. What leaves is `kdigo_consumer_line`'s own sentence, labelled by
    code from LOINC_RANGES, and the latest result's id, never the record's
    display or text.
    """
    try:
        from r6.caregaps.routes import resolve_subject, subject_resources
        from r6.labs.trend import evaluate_creatinine_aki, kdigo_consumer_line

        subject, state = resolve_subject(None, tenant_id)
        if state != "tenant-default":
            return []
        from r6.labs.routes import STORED_OBSERVATION_CAP
        # Capped as Observation/$interpret?subject= is, so the brief and
        # the chat compare the same rows.
        result = evaluate_creatinine_aki(subject_resources(
            "Observation", subject, tenant_id,
            limit=STORED_OBSERVATION_CAP))
        line = kdigo_consumer_line(result)
        if not line:
            return []
        return [{"analyte": line["analyte"], "message": line["message"],
                 "source_id": (result.get("latest") or {}).get("id") or ""}]
    except Exception as exc:
        # The brief must not 500 over one section. No section is no claim.
        logger.warning("appointment brief: lab trend check failed (%s)",
                       type(exc).__name__)
        return []


def register_brief_routes(blueprint, deps):
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    # NOT "/fhir/AppointmentBrief": the blueprint is already mounted at
    # /r6/fhir, and the extra segment registered the route at
    # /r6/fhir/fhir/AppointmentBrief while every client asked for
    # /r6/fhir/AppointmentBrief (#386). The brief page had therefore
    # never populated for anyone.
    @blueprint.get("/AppointmentBrief")
    def appointment_brief():
        # enforce_tenant_id already refused an absent or malformed id.
        tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id

        auth = authenticate_tenant_read(tenant_id)
        if auth is not None:
            return auth

        record_audit_event(
            "read",
            resource_type="AppointmentBrief",
            resource_id="singleton",
            agent_id=request.headers.get("X-Agent-Id"),
            tenant_id=tenant_id,
        )

        conditions = _resources_for(tenant_id, "Condition")
        medication_requests = _resources_for(tenant_id, "MedicationRequest")
        observations = _resources_for(tenant_id, "Observation")
        encounters = _resources_for(tenant_id, "Encounter")

        care_gap = _care_gap_result(tenant_id)

        result = generate_brief(
            conditions=conditions,
            medication_requests=medication_requests,
            observations=observations,
            encounters=encounters,
            care_gap_result=care_gap,
            lab_trends=_lab_trend_lines(tenant_id),
        )

        resource = {
            "resourceType": "Basic",
            "id": f"appointment-brief-{tenant_id}",
            "meta": {
                "profile": [
                    "https://healthclaw.io/fhir/StructureDefinition/AppointmentBrief"
                ]
            },
            "code": {
                "coding": [{
                    "system": "https://healthclaw.io/fhir/CodeSystem/resource-types",
                    "code": "appointment-brief",
                    "display": "Appointment Brief",
                }]
            },
            "extension": _brief_to_extension(result),
        }

        return jsonify(resource)
