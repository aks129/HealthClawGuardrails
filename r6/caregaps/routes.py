# r6/caregaps/routes.py
"""FHIR Patient/$care-gaps — Flask handler.

Registered on r6_blueprint (under /r6/fhir). Read-shaped: tenant-read-
authenticated + AuditEvent (PHI-free detail). Evaluates preventive-care gaps
for ?subject=Patient/<id>, or for the tenant's own Patient when no subject is
supplied, against the tenant's stored Conditions, Observations, Immunizations,
and Procedures. The `subjectResolution` parameter reports which of those
happened, and names the failure when neither could.
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

    def _resolve_subject(supplied, tenant_id):
        """Return (subject_reference, state).

        Both production callers — CareAgents' get_care_gaps and the care-gaps
        MCP App page — post an empty body with no subject, so `supplied` was
        None and `_resources_for` compared every stored subject.reference
        against None. Nothing matched, the evaluator saw an empty record, and
        the patient was told nothing was due (#389). The tenant already scopes
        the read, so the tenant's own Patient is the default here as it is
        elsewhere (r6/actions/review.py `_load_patient`).

        A fallback that cannot land is its OWN outcome and never an empty
        list. No Patient row and more than one Patient row each return a
        state, which travels to the caller in the consumer summary — the
        engine itself cannot tell the difference afterwards, because an
        unidentifiable patient produces exactly the rule results a healthy
        one does.
        """
        if supplied:
            return supplied, "supplied"
        # Two rows is all it takes to know the match is ambiguous.
        rows = R6Resource.query.filter_by(
            resource_type="Patient", tenant_id=tenant_id).limit(2).all()
        if not rows:
            return None, "no-patient"
        if len(rows) > 1:
            return None, "ambiguous-patient"
        return f"Patient/{rows[0].id}", "tenant-default"

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

        supplied = _subject_from_request()
        subject, state = _resolve_subject(supplied, tenant_id)
        not_evaluated = (state if state in ("no-patient", "ambiguous-patient")
                         else None)

        # `subject`, NOT `supplied` — the Patient the fallback resolved is the
        # Patient we evaluate. #389 half two.
        #
        # An earlier version of this comment said the change was "released by
        # the clinical advisor's ruling on the cadence table". No such ruling exists in
        # docs/, on #389, or on #423, and #389 asked for one in as many words:
        # "it wants CTO sign-off and clinical review, not an engineering
        # judgement call". What actually released it was the owner's approval
        # plus #428, which stopped the one rule that was demonstrably unsafe
        # (colorectal, blind to FIT and Cologuard) from claiming anything.
        #
        # That is a narrower thing, and it leaves #389's other named rule
        # unreviewed: A1c monitoring is patient-visible today and no clinician
        # has passed on its cadence. Tracked on #389; do not let this comment
        # be read as clearance.
        patient = _patient_for(subject, tenant_id)

        # `check-incomplete` (#417) covered the window in which a resolved
        # Patient was held back from the evaluator: every rule reported the
        # date of birth as unknown because the engine was never given one, and
        # no reason about this person's demographics could be true. The
        # evaluator now sees the record, so the engine's own causes ARE about
        # the record and say what is missing from it. A subject naming a row
        # we do not hold still reads nothing, and still says so.
        if not_evaluated is None and patient is None:
            not_evaluated = "check-incomplete"

        # No subject means nothing to compare against, so we do not pretend to
        # have read anything.
        def _for(resource_type):
            return _resources_for(resource_type, subject, tenant_id) if subject else []

        as_of = date.today().isoformat()
        results = evaluate_care_gaps(
            patient, conditions=_for("Condition"),
            observations=_for("Observation"),
            immunizations=_for("Immunization"), procedures=_for("Procedure"),
            as_of=as_of)

        summary = build_caregaps_summary(results)
        consumer = build_consumer_summary(results, not_evaluated=not_evaluated)

        record_audit_event(
            "read", resource_type="Patient", resource_id=None,
            agent_id=request.headers.get("X-Agent-Id"), tenant_id=tenant_id,
            detail=(f"care-gaps; subject={state} evaluated={summary['total']} "
                    f"due={summary['due']}"))

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [
                {"name": "summary", "valueString": json.dumps(summary)},
                {"name": "consumerSummary", "valueString": json.dumps(consumer)},
                {"name": "subjectResolution",
                 "valueString": json.dumps({"state": state, "subject": subject})},
                {"name": "detail", "valueString": json.dumps(results)},
                {"name": "disclaimer", "valueString": _DISCLAIMER},
            ],
        }), 200
