"""FHIR $evaluate-measure for NQF 0018 — Flask handler.

Registered on r6_blueprint (under /r6/fhir) so it shares tenant enforcement.
Read-shaped: tenant-read-authenticated + AuditEvent. Computes the measure from
the tenant's stored Patient / Condition / Observation resources.
"""

import datetime as dt
import logging

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.quality.measures import evaluate_nqf0018, evaluate_population
from r6.quality.report import (
    build_individual_report, build_summary_report, build_measure_resource,
)

logger = logging.getLogger(__name__)

MEASURE_ID = "nqf0018-controlling-high-bp"


def _default_measurement_period(today=None):
    today = today or dt.date.today()
    return f"{today.year}-01-01", f"{today.year}-12-31"


def register_quality_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    def _tenant():
        return (request.headers.get("X-Tenant-Id") or "").strip() or None

    def _param(params, name):
        for p in params.get("parameter", []):
            if p.get("name") == name:
                return (p.get("valueString") or p.get("valueDate")
                        or p.get("valueReference", {}).get("reference"))
        return None

    def _load(resource_type, tenant_id):
        return [r.to_fhir_json() for r in R6Resource.query.filter_by(
            resource_type=resource_type, tenant_id=tenant_id).all()]

    def _for_subject(resources, subject_ref):
        return [r for r in resources
                if r.get("subject", {}).get("reference") == subject_ref]

    @blueprint.route(f"/Measure/{MEASURE_ID}", methods=["GET"])
    def get_measure():
        return jsonify(build_measure_resource()), 200

    @blueprint.route(f"/Measure/{MEASURE_ID}/$evaluate-measure",
                     methods=["POST", "GET"])
    def evaluate_measure():
        tenant_id = _tenant()
        if not tenant_id:
            return jsonify(operation_outcome(
                "error", "security", "X-Tenant-Id required")), 400
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        params = request.get_json(silent=True) or {}
        default_start, default_end = _default_measurement_period()
        period_start = (_param(params, "periodStart")
                        or request.args.get("periodStart")
                        or default_start)
        period_end = (_param(params, "periodEnd")
                      or request.args.get("periodEnd")
                      or default_end)
        subject = (_param(params, "subject")
                   or request.args.get("subject"))

        patients = _load("Patient", tenant_id)
        conditions = _load("Condition", tenant_id)
        observations = _load("Observation", tenant_id)

        if subject:
            subj_ref = subject if "/" in subject else f"Patient/{subject}"
            patient = next(
                (p for p in patients
                 if f"Patient/{p.get('id')}" == subj_ref), None)
            if patient is None:
                return jsonify(operation_outcome(
                    "error", "not-found", "subject Patient not found")), 404
            result = evaluate_nqf0018(
                patient, _for_subject(conditions, subj_ref),
                _for_subject(observations, subj_ref), period_start, period_end)
            report = build_individual_report(subj_ref, result,
                                             period_start, period_end)
            detail = f"nqf0018 individual numerator={result['in_numerator']}"
        else:
            cohort = [{
                "patient": p,
                "conditions": _for_subject(conditions, f"Patient/{p.get('id')}"),
                "observations": _for_subject(observations, f"Patient/{p.get('id')}"),
            } for p in patients]
            pop = evaluate_population(cohort, period_start, period_end)
            report = build_summary_report(pop, period_start, period_end)
            detail = (f"nqf0018 summary rate={pop['performance_rate']} "
                      f"n={pop['numerator']}/{pop['denominator']}")

        record_audit_event("read", "Measure", MEASURE_ID,
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id, detail=detail)
        return jsonify(report), 200

    return evaluate_measure
