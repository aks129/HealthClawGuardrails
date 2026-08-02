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
from r6.brief.engine import generate_brief, BriefResult, BriefField

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
    return [r.resource for r in rows]


def _field_to_dict(f: BriefField) -> dict:
    return {
        "label": f.label,
        "value": f.value,
        "sourceType": f.source_type,
        "sourceId": f.source_id,
    }


def _brief_to_extension(result: BriefResult) -> list[dict]:
    def _section(name: str, fields: list[BriefField]) -> dict:
        return {
            "url": f"https://healthclaw.io/fhir/StructureDefinition/brief-section-{name}",
            "extension": [
                {"url": "field", "valueString": json.dumps(_field_to_dict(f))}
                for f in fields
            ],
        }

    return [
        _section("problems", result.problems),
        _section("medications", result.medications),
        _section("labs", result.labs),
        _section("care-gaps", result.care_gaps),
        _section("visits", result.visits),
    ]


def _care_gap_result(conditions: list[dict], observations: list[dict]) -> dict:
    """Run care-gaps evaluation; return empty result on any failure."""
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
    except Exception:
        return {}


def register_brief_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    @blueprint.get("/fhir/AppointmentBrief")
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
