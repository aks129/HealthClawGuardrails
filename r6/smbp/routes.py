"""SMBP Flask blueprint — enroll, log reading, clinician report.

Read-shaped endpoints are tenant-authenticated; the reading write requires a
step-up token (it creates an Observation). All FHIR writes emit an AuditEvent.
Outbound patient contact (reminders, calls) is NOT issued here — it goes through
the r6/actions propose -> human-confirm -> commit loop.
"""

import json
import logging

from flask import Blueprint, request, jsonify, Response

from r6.models import R6Resource, db
from r6.audit import record_audit_event
from r6.stepup import validate_step_up_token
from r6.smbp.models import SMBPSession
from r6.smbp.monitoring import build_bp_observation
from r6.smbp.triage import classify
from r6.smbp.report import build_report, render_html, render_pdf

logger = logging.getLogger(__name__)

smbp_blueprint = Blueprint("smbp", __name__, url_prefix="/r6/smbp")


def _tenant():
    return (request.headers.get("X-Tenant-Id") or "").strip() or None


def _oo(severity, code, diagnostics):
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": severity, "code": code, "diagnostics": diagnostics}]}


@smbp_blueprint.route("/enroll", methods=["POST"])
def enroll():
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400
    body = request.get_json(silent=True) or {}
    patient_ref = body.get("patient_ref")
    if not patient_ref:
        return jsonify(_oo("error", "invalid", "patient_ref required")), 400
    try:
        days = int(body.get("days", 14))
    except (ValueError, TypeError):
        return jsonify(_oo("error", "invalid", "days must be an integer")), 400
    session = SMBPSession(
        tenant_id=tenant_id,
        patient_ref=patient_ref,
        language=body.get("language", "en"),
        days=days,
        consent_captured=bool(body.get("consent_captured", False)),
    )
    db.session.add(session)
    db.session.commit()
    record_audit_event("create", "SMBPSession", session.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id, detail="smbp enroll")
    return jsonify(session.to_dict()), 201


@smbp_blueprint.route("/reading", methods=["POST"])
def reading():
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400

    step_up = request.headers.get("X-Step-Up-Token")
    if not step_up:
        return jsonify(_oo("error", "security",
                           "reading requires X-Step-Up-Token")), 401
    valid, _err = validate_step_up_token(step_up, tenant_id)
    if not valid:
        return jsonify(_oo("error", "security", "Invalid step-up token")), 401

    body = request.get_json(silent=True) or {}
    try:
        systolic = int(body["systolic"])
        diastolic = int(body["diastolic"])
        patient_ref = body["patient_ref"]
        effective = body["effective"]
    except (KeyError, ValueError, TypeError):
        return jsonify(_oo("error", "invalid",
                           "patient_ref, systolic, diastolic, effective required")), 400

    triage = classify(systolic, diastolic, body.get("symptoms"))
    obs = build_bp_observation(patient_ref, systolic, diastolic, effective)
    row = R6Resource(resource_type="Observation",
                     resource_json=json.dumps(obs), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    record_audit_event("create", "Observation", row.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id,
                       detail="smbp reading band=%s" % triage["band"])
    return jsonify({"observation_id": row.id, "triage": triage}), 201


@smbp_blueprint.route("/report/<session_id>", methods=["GET"])
def report(session_id):
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400
    from r6.routes import authenticate_tenant_read
    auth_err = authenticate_tenant_read(tenant_id)
    if auth_err is not None:
        return auth_err[0], auth_err[1]
    session = SMBPSession.query.filter_by(id=session_id, tenant_id=tenant_id).first()
    if session is None:
        return jsonify(_oo("error", "not-found", "session not found")), 404

    rows = R6Resource.query.filter_by(resource_type="Observation",
                                      tenant_id=tenant_id).all()
    observations = []
    for r in rows:
        obs = r.to_fhir_json()
        if obs.get("subject", {}).get("reference") == session.patient_ref:
            observations.append(obs)

    label = session.patient_ref.split("/")[-1]
    rep = build_report(session.patient_ref, label, session.days, observations)

    record_audit_event("read", "SMBPSession", session.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id,
                       detail="smbp report readings=%d" % len(observations))

    if request.args.get("format") == "pdf":
        pdf = render_pdf(rep)
        _persist_document_reference(tenant_id, session, len(pdf))
        return Response(pdf, mimetype="application/pdf")
    return Response(render_html(rep), mimetype="text/html")


def _persist_document_reference(tenant_id, session, size):
    doc = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "57075-4",
                             "display": "SMBP report"}]},
        "subject": {"reference": session.patient_ref},
        "content": [{"attachment": {"contentType": "application/pdf",
                                    "title": "SMBP report",
                                    "size": size}}],
    }
    row = R6Resource(resource_type="DocumentReference",
                     resource_json=json.dumps(doc), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    record_audit_event("create", "DocumentReference", row.id,
                       tenant_id=tenant_id, detail="smbp report pdf")
