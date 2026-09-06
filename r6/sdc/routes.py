"""SDC $populate / $extract Flask handlers.

Attached to the existing r6_blueprint so the tenant-enforcement before_request
hook applies. Owns all store I/O, audit, and step-up; the transform
logic lives in the pure engines (populate.py, extract.py).

What $populate is allowed to read (council ruling D10). Three seats called
the old behaviour an unbounded PHI read, and it was: any expression a caller
put in a Questionnaire evaluated against the whole stored Patient, and every
Observation / MedicationRequest / AllergyIntolerance / Condition the tenant
held for that subject was loaded verbatim into the answer set. Three bounds
now apply, each in the place that owns it:

  1. Expressions see a BOUNDED PROJECTION of the subject, never the stored
     Patient (r6/sdc/expressions.py).
  2. Auto-loaded clinical content goes through apply_redaction before the
     engine sees it (_redacted_for_populate below), so an upstream `display`
     or `CodeableConcept.text` carrying a patient name cannot transit. The
     inline `content` Bundle a caller supplies is the caller's own data and
     is passed through unchanged.
  3. Tombstoned rows are not read at all (is_deleted=False, #422).
"""

import json
import logging

from flask import request, jsonify

from r6.access import Profile, TenantSource, fhir_response, tenant_from_request
from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.redaction import apply_redaction
from r6.sdc.populate import NOT_POPULATED, populate_questionnaire
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
        tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
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
        # Exit through the kernel so this operation is COUNTED as a shaped
        # FHIR exit rather than a bare jsonify (spec §1.4). Say plainly what
        # that buys and what it does not: Profile.INTAKE runs _intake_profile,
        # which pops top-level note/text/SSN-identifiers and does not recurse,
        # so on this Parameters wrapper it changes nothing. What actually
        # bounds the payload is upstream — the %patient projection in
        # r6/sdc/expressions.py and apply_redaction in _gather_content below.
        return fhir_response(response_params, profile=Profile.INTAKE)

    @blueprint.route("/QuestionnaireResponse/$extract", methods=["POST"])
    @blueprint.route("/QuestionnaireResponse/<qr_id>/$extract",
                     methods=["POST"])
    def sdc_extract(qr_id=None):
        tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
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
        # THE SUBJECT IS NOT IN THIS LIST. It reaches the engine as
        # populate_questionnaire's `subject` argument, and it is NOT redacted
        # there: an intake form exists to carry the patient's own name, DOB
        # and address, and apply_redaction would truncate all three. What
        # bounds it is the %patient projection in r6/sdc/expressions.py — an
        # allowlist of exactly those elements — plus reference matching,
        # which reads only the id.
        #
        # It used to be appended here as well. Nothing read it: the engine
        # filters this list to Observations and to the three list-resource
        # types, so a Patient passed no filter. But it was the second door to
        # the identifier oracle QA walked an SSN through (PR #562 review) —
        # `%resources.where(resourceType='Patient').identifier.value` — and
        # it stayed reachable by anything that walks content. Pinned by
        # tests/test_sdc_populate_bounded.py::
        # test_the_content_list_handed_to_the_engine_carries_no_subject.
        content = []
        if subject and subject.get("id"):
            for resource_type, subject_field in _AUTO_LOADED_RESOURCE_TYPES:
                content.extend(_redacted_for_populate(
                    _load_resources_for_patient(resource_type, subject_field,
                                                subject["id"], tenant_id)))
        # The inline `content` Bundle is the CALLER'S OWN data — they just
        # sent it in the request body — so it is passed through as it always
        # has been. Redacting it would strip what the caller supplied and
        # hand it back unusable, and it reveals nothing the caller did not
        # already hold. Only the resources this route loaded from the tenant
        # store on the caller's behalf go through apply_redaction.
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
    # is_deleted=False is #422: a tombstoned row must not be read back into a
    # form. `tenant_id` is the kernel's (tenant_from_request), so the scope
    # this filters by is the one the gate authorized. The kernel has no
    # resource selector yet — that is playbook F5, unlanded — so the query
    # itself stays here.
    row = R6Resource.query.filter_by(
        resource_type=resource_type, id=resource_id,
        tenant_id=tenant_id, is_deleted=False).first()
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
        resource_type=resource_type, tenant_id=tenant_id,
        is_deleted=False).all()
    out = []
    ref = f"Patient/{patient_id}"
    for row in rows:
        resource = row.to_fhir_json()
        if resource.get(subject_field, {}).get("reference") == ref:
            out.append(resource)
    return out


def _redacted_for_populate(resources):
    """Auto-loaded clinical content, redacted before the engine sees it.

    apply_redaction strips every upstream `display` and `CodeableConcept.text`
    — the two fields real feeds put patient names in — and then re-applies
    labels from r6/terminology.py keyed by code (r6/redaction.py:22-38). That
    is the profile the ruling's own words name ("apply_redaction, then
    terminology labels by code"). Profile.INTAKE is NOT usable here: its
    _intake_strip pops note/text/SSN identifiers at the top level only and
    leaves `code.text` untouched, which is the leak rather than the fix.

    Redacting BEFORE population, not after, is what makes this bound hold for
    every mechanism at once. The engine copies free text into answers from
    four different resolvers and from Observation.valueCodeableConcept; a
    redaction pass over the response afterwards would have to know which
    answers came from a record and which the caller typed.

    What this costs, deliberately: an upstream free-text name survives only
    if the server recognises the code beside it. `dosageInstruction[].text`
    and any label for a code r6/terminology.py has no entry for (SNOMED
    allergens, today) do not come back. See the PR body — that is the ruled
    trade, not an oversight.
    """
    return [apply_redaction(resource) for resource in resources]


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
    # THE EXPLANATION IS SAID ONCE. It used to ride on every issue, and it
    # was a 230-character constant identical for every leaf, so the
    # response grew with the number of unanswered leaves times a fixed
    # paragraph: 29.3KB of request came back 3519.6KB over HTTP, and a
    # 293.0KB request produced 352.5MB in process. The CTO ruling on the QA
    # review of #576 is to stop repeating it rather than to cap the list —
    # a cap is a second control to reason about and would silently truncate
    # a legitimate long form, while the amplification was the repetition and
    # nothing else.
    #
    # So: one leading `informational` issue carrying the sentence, then one
    # `incomplete` issue per unanswered leaf carrying ONLY its linkId. A
    # caller still learns exactly which leaves were unanswered and exactly
    # why, each per-leaf entry is still greppable by linkId — which is what
    # a caller branches on — and the two kinds are told apart by `code`
    # rather than by position.
    #
    # severity=information on both, NOT warning. Under unconditional
    # emission (r6/sdc/populate.py:_report_unpopulated) an ordinary form
    # with one empty optional field carries an issue, and a conformant
    # client reading `warning` treats that as a failed operation. Nothing
    # here is a failure — every issue says "this leaf resolved no value",
    # which is a restatement of the response's own missing `answer`.
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "information", "code": "informational",
                   "diagnostics": NOT_POPULATED}]
        + [{"severity": "information", "code": "incomplete",
            "diagnostics": i["linkId"]} for i in issues],
    }
