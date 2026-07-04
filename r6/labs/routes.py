# r6/labs/routes.py
"""FHIR Observation/$interpret — Flask handler.

Registered on r6_blueprint (under /r6/fhir). Read-shaped: tenant-read-
authenticated + AuditEvent (PHI-free detail). Interprets a single Observation,
a Bundle, or the tenant's stored Observations for ?subject=Patient/<id>.
"""
import json
import logging

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.labs.interpret import interpret_observation
from r6.labs.report import (
    annotate_observation, build_interpretation_summary, build_consumer_summary,
)

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Advisory decision support, not a diagnosis. Reference ranges are "
               "adult population defaults and vary by lab, age, sex, and clinical "
               "context. The performing lab's own reference range takes precedence.")


def register_labs_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    def _tenant():
        return (request.headers.get("X-Tenant-Id") or "").strip() or None

    def _observations_from_request(tenant_id):
        """Return (observations, ignored_count). Tolerates malformed input."""
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        subject = request.args.get("subject")
        if body.get("resourceType") == "Parameters":
            for p in body.get("parameter", []):
                if isinstance(p, dict) and p.get("name") == "subject":
                    ref = p.get("valueReference")
                    if isinstance(ref, dict):
                        subject = ref.get("reference") or subject
        observations, ignored = [], 0
        if subject:
            rows = R6Resource.query.filter_by(
                resource_type="Observation", tenant_id=tenant_id).all()
            for row in rows:
                obs = row.to_fhir_json()
                if obs.get("subject", {}).get("reference") == subject:
                    observations.append(obs)
        elif body.get("resourceType") == "Bundle":
            for e in body.get("entry", []):
                res = e.get("resource", {}) if isinstance(e, dict) else {}
                if isinstance(res, dict) and res.get("resourceType") == "Observation":
                    observations.append(res)
                else:
                    ignored += 1
        elif body.get("resourceType") == "Observation":
            observations.append(body)
        elif body:
            ignored += 1
        return observations, ignored

    def _patient_for(obs, tenant_id, cache):
        ref = obs.get("subject", {}).get("reference")
        if not ref or not ref.startswith("Patient/"):
            return None
        if ref in cache:
            return cache[ref]
        row = R6Resource.query.filter_by(
            resource_type="Patient", id=ref.split("/", 1)[1],
            tenant_id=tenant_id).first()
        cache[ref] = row.to_fhir_json() if row else None
        return cache[ref]

    @blueprint.route("/Observation/$interpret", methods=["POST"])
    def interpret_labs():
        tenant_id = _tenant()
        if not tenant_id:
            return jsonify(operation_outcome(
                "error", "security", "X-Tenant-Id required")), 400
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        observations, ignored = _observations_from_request(tenant_id)
        cache, results, annotated = {}, [], []
        for obs in observations:
            patient = _patient_for(obs, tenant_id, cache)
            res = interpret_observation(obs, patient)
            results.append(res)
            annotated.append({"resource": annotate_observation(obs, res)})

        summary = build_interpretation_summary(results)
        summary["ignored"] = ignored
        consumer = build_consumer_summary(results)

        record_audit_event(
            "read", resource_type="Observation", resource_id=None,
            agent_id=request.headers.get("X-Agent-Id"), tenant_id=tenant_id,
            detail=(f"labs $interpret; interpreted={summary['total']} "
                    f"flagged={len(summary['flagged'])} critical={summary['critical']}"))

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [
                {"name": "return",
                 "resource": {"resourceType": "Bundle", "type": "collection",
                              "entry": annotated}},
                {"name": "summary", "valueString": json.dumps(summary)},
                {"name": "consumerSummary", "valueString": json.dumps(consumer)},
                {"name": "disclaimer", "valueString": _DISCLAIMER},
            ],
        }), 200
