"""FHIR $appointment-brief — Flask handler.

Registered on r6_blueprint (under /r6/fhir) so it shares tenant enforcement.
Read-shaped: tenant-read-authenticated + AuditEvent.

Returns a FHIR Basic resource whose extension carries the structured brief.
Consuming clients (CareAgents) parse the extension rather than rendering the
raw FHIR shape.
"""

import json
import logging
from datetime import date

from flask import request, jsonify

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


def _tenant() -> str | None:
    return (request.headers.get("X-Tenant-Id") or "").strip() or None


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
        _section("care-gaps", result.care_gaps, care_gap_state),
        _section("visits", result.visits),
    ]


def _care_gap_result(conditions: list[dict], observations: list[dict]) -> dict:
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
        results = evaluate_care_gaps(
            patient=None,
            conditions=conditions,
            observations=observations,
            as_of=date.today().isoformat(),
        )
        consumer = build_consumer_summary(results)
        return {"consumer": consumer}
    except Exception as exc:
        logger.warning("appointment brief: care-gaps evaluation failed (%s)",
                       type(exc).__name__)
        return {"status": CARE_GAPS_UNAVAILABLE,
                "reason": CARE_GAPS_REASON_ENGINE_ERROR}


def register_brief_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    # NOT "/fhir/AppointmentBrief": the blueprint is already mounted at
    # /r6/fhir, and the extra segment registered the route at
    # /r6/fhir/fhir/AppointmentBrief while every client asked for
    # /r6/fhir/AppointmentBrief (#386). The brief page had therefore
    # never populated for anyone.
    @blueprint.get("/AppointmentBrief")
    def appointment_brief():
        tenant_id = _tenant()
        if not tenant_id:
            return operation_outcome("error", "required", "X-Tenant-Id header missing"), 400

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

        care_gap = _care_gap_result(conditions, observations)

        result = generate_brief(
            conditions=conditions,
            medication_requests=medication_requests,
            observations=observations,
            encounters=encounters,
            care_gap_result=care_gap,
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
