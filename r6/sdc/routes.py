"""SDC $populate / $extract Flask handlers.

Attached to the existing r6_blueprint so the tenant-enforcement before_request
hook applies. Owns all store I/O, audit, and step-up; the transform
logic lives in the pure engines (populate.py, extract.py).
"""

import json
import logging

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.sdc.populate import populate_questionnaire
from r6.sdc.extract import extract_resources

logger = logging.getLogger(__name__)


def register_sdc_routes(blueprint, deps):
    """Register SDC routes on `blueprint`.

    deps: dict providing helpers from r6/routes.py —
      'operation_outcome', 'authenticate_tenant_read', 'validate_step_up_token',
      'validator'.

    Note: `operation_outcome` already returns a Flask Response (it calls
    jsonify internally), and `authenticate_tenant_read` returns
    (Response, status). Handlers below return those directly — do NOT
    re-wrap them in jsonify.
    """
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]
    validate_step_up_token = deps["validate_step_up_token"]
    validator = deps["validator"]

    @blueprint.route("/Questionnaire/$populate", methods=["POST"])
    @blueprint.route("/Questionnaire/<questionnaire_id>/$populate",
                     methods=["POST"])
    def sdc_populate(questionnaire_id=None):
        tenant_id = request.headers.get("X-Tenant-Id")
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        params = request.get_json(silent=True) or {}
        questionnaire = _resolve_questionnaire(params, questionnaire_id,
                                               tenant_id)
        if questionnaire is None:
            return operation_outcome(
                "error", "not-found",
                "Questionnaire could not be resolved"), 404

        subject = _resolve_subject(params, tenant_id)
        content = _gather_content(params, subject, tenant_id)

        qr, issues = populate_questionnaire(questionnaire, subject, content)

        record_audit_event("read", "Questionnaire",
                            questionnaire.get("id"),
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id,
                            detail=f"populate; issues={len(issues)}")

        response_params = {
            "resourceType": "Parameters",
            "parameter": [{"name": "response", "resource": qr}],
        }
        if issues:
            response_params["parameter"].append(
                {"name": "issues", "resource": _issues_outcome(issues)})
        return jsonify(response_params), 200

    @blueprint.route("/QuestionnaireResponse/$extract", methods=["POST"])
    @blueprint.route("/QuestionnaireResponse/<qr_id>/$extract",
                     methods=["POST"])
    def sdc_extract(qr_id=None):
        tenant_id = request.headers.get("X-Tenant-Id")
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]
        dry_run = request.args.get("dryRun", "false").lower() == "true"

        params = request.get_json(silent=True) or {}
        qr = _param_resource(params, "questionnaire-response")
        if qr is None and qr_id:
            qr = _load_stored("QuestionnaireResponse", qr_id, tenant_id)
        if qr is None:
            return operation_outcome(
                "error", "invalid",
                "questionnaire-response parameter is required"), 400

        # Step-up gate (writes) fires before any resolution/extraction work
        # so a commit-mode caller without a token is rejected up front.
        # dry_run is a read-shaped preview and skips the gate.
        if not dry_run:
            step_up = request.headers.get("X-Step-Up-Token")
            if not step_up:
                return operation_outcome(
                    "error", "security",
                    "$extract requires X-Step-Up-Token (use dryRun=true to "
                    "preview without committing)"), 401
            valid, _err = validate_step_up_token(step_up, tenant_id)
            if not valid:
                return operation_outcome(
                    "error", "security", "Invalid step-up token"), 401

        questionnaire = (_param_resource(params, "questionnaire")
                         or _resolve_referenced_questionnaire(qr, tenant_id))
        if questionnaire is None:
            return operation_outcome(
                "error", "not-found",
                "Questionnaire for the response could not be resolved"), 404

        bundle = extract_resources(qr, questionnaire)

        if not dry_run:
            # H4 posture (deliberate): $extract commits clinical resources as a
            # structured bundle import, like Bundle/$ingest-context — both are
            # exempt from the per-resource X-Human-Confirmed gate that direct
            # writes (e.g. POST /Observation) require. Step-up + $validate gate
            # the write here; the form-fill review IS the human-in-the-loop step.
            for entry in bundle["entry"]:
                result = validator.validate_resource(entry["resource"])
                if not result["valid"]:
                    return jsonify(result["operation_outcome"]), 422
            try:
                _commit_bundle(bundle, tenant_id)
            except Exception as exc:
                from r6.models import db
                db.session.rollback()
                logger.error("SDC extract commit failed: %s",
                             type(exc).__name__)
                return operation_outcome(
                    "error", "exception",
                    "Failed to commit extracted resources"), 500

        record_audit_event("create" if not dry_run else "read",
                            "QuestionnaireResponse", qr.get("id"),
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id,
                            detail=f"extract; dryRun={dry_run}; "
                                   f"resources={len(bundle['entry'])}")

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [{"name": "return", "resource": bundle}],
        }), 200

    def _resolve_questionnaire(params, questionnaire_id, tenant_id):
        inline = _param_resource(params, "questionnaire")
        if inline:
            return inline
        if questionnaire_id:
            return _load_stored("Questionnaire", questionnaire_id, tenant_id)
        ref = _param_value(params, "questionnaireRef", "valueString")
        if ref and "/" in ref:
            return _load_stored("Questionnaire", ref.split("/")[-1], tenant_id)
        return None

    def _resolve_subject(params, tenant_id):
        inline = _param_resource(params, "subject")
        if inline:
            return inline
        ref = _param_value(params, "subject", "valueReference")
        if isinstance(ref, dict) and ref.get("reference"):
            ident = ref["reference"].split("/")[-1]
            return _load_stored("Patient", ident, tenant_id)
        return None

    def _gather_content(params, subject, tenant_id):
        content = []
        if subject:
            content.append(subject)
        if subject and subject.get("id"):
            for resource_type, subject_field in _AUTO_LOADED_RESOURCE_TYPES:
                content.extend(_load_resources_for_patient(
                    resource_type, subject_field, subject["id"], tenant_id))
        bundle = _param_resource(params, "content")
        if bundle and bundle.get("resourceType") == "Bundle":
            content.extend(e["resource"] for e in bundle.get("entry", [])
                           if "resource" in e)
        return content

    def _resolve_referenced_questionnaire(qr, tenant_id):
        canonical = qr.get("questionnaire")
        if not canonical:
            return None
        ident = canonical.split("|")[0].split("/")[-1]
        return _load_stored("Questionnaire", ident, tenant_id)

    return sdc_populate, sdc_extract


def _param_resource(params, name):
    for p in params.get("parameter", []):
        if p.get("name") == name and "resource" in p:
            return p["resource"]
    return None


def _param_value(params, name, value_key):
    for p in params.get("parameter", []):
        if p.get("name") == name and value_key in p:
            return p[value_key]
    return None


def _load_stored(resource_type, resource_id, tenant_id):
    row = R6Resource.query.filter_by(
        resource_type=resource_type, id=resource_id,
        tenant_id=tenant_id).first()
    return row.to_fhir_json() if row else None


# Resource types $populate auto-loads for the subject, alongside the field
# each one uses to reference its patient (R4 is inconsistent here —
# AllergyIntolerance uses `patient`, everything else here uses `subject`).
# MedicationRequest/AllergyIntolerance/Condition feed r6/sdc/populate.py's
# list-resource population (medications/allergies/conditions repeating
# groups on the intake Questionnaire); Observation feeds item.code matching.
_AUTO_LOADED_RESOURCE_TYPES = [
    ("Observation", "subject"),
    ("MedicationRequest", "subject"),
    ("AllergyIntolerance", "patient"),
    ("Condition", "subject"),
]


def _load_resources_for_patient(resource_type, subject_field, patient_id, tenant_id):
    rows = R6Resource.query.filter_by(
        resource_type=resource_type, tenant_id=tenant_id).all()
    out = []
    ref = f"Patient/{patient_id}"
    for row in rows:
        resource = row.to_fhir_json()
        if resource.get(subject_field, {}).get("reference") == ref:
            out.append(resource)
    return out


def _commit_bundle(bundle, tenant_id):
    from r6.models import db
    for entry in bundle["entry"]:
        resource = entry["resource"]
        row = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            tenant_id=tenant_id,
        )
        db.session.add(row)
    db.session.commit()


def _issues_outcome(issues):
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "warning", "code": "incomplete",
                   "diagnostics": f"{i['linkId']}: {i['detail']}"}
                  for i in issues],
    }
