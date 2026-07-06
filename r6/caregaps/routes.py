# r6/caregaps/routes.py
"""FHIR Patient/$care-gaps — Flask handler.

Registered on r6_blueprint (under /r6/fhir). Read-shaped: tenant-read-
authenticated + AuditEvent (PHI-free detail). Evaluates preventive-care gaps
for ?subject=Patient/<id> against the tenant's stored Conditions,
Observations, Immunizations, and Procedures.
"""
import json
import logging
from datetime import date

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_caregaps_summary, build_consumer_summary

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Preventive-care decision support based on published guidelines "
              "(USPSTF/ACIP/ADA). Not a diagnosis or a directive; population-level "
              "adult defaults that individual risk factors can change. Confirm "
              "with your clinician. This is a lightweight consumer-facing check, "
              "not the Da Vinci DEQM $care-gaps operation and not a certified "
              "eCQM; per-rule related_ecqm ids are provided for reconciling with "
              "certified measure engines.")


def register_caregaps_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    def _tenant():
        return (request.headers.get("X-Tenant-Id") or "").strip() or None

    def _subject_from_request():
        subject = request.args.get("subject")
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict) and body.get("resourceType") == "Parameters":
            for p in body.get("parameter", []):
                if isinstance(p, dict) and p.get("name") == "subject":
                    ref = p.get("valueReference")
                    if isinstance(ref, dict):
                        subject = ref.get("reference") or subject
        return subject

    def _patient_for(subject, tenant_id):
        if not subject or not subject.startswith("Patient/"):
            return None
        row = R6Resource.query.filter_by(
            resource_type="Patient", id=subject.split("/", 1)[1],
            tenant_id=tenant_id).first()
        return row.to_fhir_json() if row else None

    def _resources_for(resource_type, subject, tenant_id):
        rows = R6Resource.query.filter_by(
            resource_type=resource_type, tenant_id=tenant_id).all()
        out = []
        for row in rows:
            res = row.to_fhir_json()
            if res.get("subject", {}).get("reference") == subject:
                out.append(res)
        return out

    @blueprint.route("/Patient/$care-gaps", methods=["GET", "POST"])
    def care_gaps():
        tenant_id = _tenant()
        if not tenant_id:
            return jsonify(operation_outcome(
                "error", "security", "X-Tenant-Id required")), 400
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        subject = _subject_from_request()
        patient = _patient_for(subject, tenant_id)
        conditions = _resources_for("Condition", subject, tenant_id)
        observations = _resources_for("Observation", subject, tenant_id)
        immunizations = _resources_for("Immunization", subject, tenant_id)
        procedures = _resources_for("Procedure", subject, tenant_id)
        as_of = date.today().isoformat()

        results = evaluate_care_gaps(
            patient, conditions=conditions, observations=observations,
            immunizations=immunizations, procedures=procedures, as_of=as_of)

        summary = build_caregaps_summary(results)
        consumer = build_consumer_summary(results)

        record_audit_event(
            "read", resource_type="Patient", resource_id=None,
            agent_id=request.headers.get("X-Agent-Id"), tenant_id=tenant_id,
            detail=(f"care-gaps; evaluated={summary['total']} "
                    f"due={summary['due']}"))

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [
                {"name": "summary", "valueString": json.dumps(summary)},
                {"name": "consumerSummary", "valueString": json.dumps(consumer)},
                {"name": "detail", "valueString": json.dumps(results)},
                {"name": "disclaimer", "valueString": _DISCLAIMER},
            ],
        }), 200
