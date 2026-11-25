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
     Patient (r6/sdc/expressions.py). Over HTTP that subject has its
     identity removed first (_identity_free below), because this route's
     answer reaches a model (spec 2026-11-24 §7 PR 3).
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

from r6.access import (Scope, TenantSource, decide_grant, tenant_from_request,
                       unredacted_response)
from models import db
from r6.models import R6Resource
from r6.audit import add_audit_event
from r6.redaction import apply_redaction
from r6.sdc.populate import NOT_POPULATED, populate_questionnaire
from r6.sdc.extract import COMMIT_WITHOUT_CONFIRMATION, extract_resources

logger = logging.getLogger(__name__)


def register_sdc_routes(blueprint, deps):
    """Register SDC routes on `blueprint`.

    deps: dict providing helpers from r6/routes.py —
      'operation_outcome', 'authenticate_tenant_read', 'validator'.

    Note: `operation_outcome` already returns a Flask Response (it calls
    jsonify internally), and `authenticate_tenant_read` returns
    (Response, status). Handlers below return those directly — do NOT
    re-wrap them in jsonify.
    """
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]
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

        qr, issues = populate_questionnaire(
            questionnaire, _identity_free(subject), content)

        add_audit_event("read", "Questionnaire",
                        questionnaire.get("id"),
                        agent_id=request.headers.get("X-Agent-Id"),
                        tenant_id=tenant_id,
                        detail=f"populate; issues={len(issues)}")
        db.session.commit()

        response_params = {
            "resourceType": "Parameters",
            "parameter": [{"name": "response", "resource": qr}],
        }
        if issues:
            response_params["parameter"].append(
                {"name": "issues", "resource": _issues_outcome(issues)})
        # Exit through the kernel so this operation is COUNTED, not a bare
        # jsonify (spec §1.4). Until #282 this was fhir_response with
        # Profile.INTAKE, which then popped top-level fields only and changed
        # nothing here. Intake now runs apply_redaction, which would delete
        # every populated answer, so the exit says what bounds the payload:
        # _identity_free on the subject and apply_redaction in
        # _gather_content, both BEFORE population. A strip afterwards cannot
        # tell a record answer from one the caller typed.
        return unredacted_response(
            response_params, endpoint=request.endpoint,
            reason="subject and content redacted before population")

    @blueprint.route("/QuestionnaireResponse/$extract", methods=["POST"])
    @blueprint.route("/QuestionnaireResponse/<qr_id>/$extract",
                     methods=["POST"])
    def sdc_extract(qr_id=None):
        tenant = tenant_from_request(sources=(TenantSource.HEADER,))
        tenant_id = tenant.id
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]
        dry_run = request.args.get("dryRun", "false").lower() == "true"

        params = request.get_json(silent=True) or {}
        qr = _param_resource(params, "questionnaire-response")
        from_store = False
        if qr is None and qr_id:
            # A STORED response is record content, so extraction reads what
            # GET /QuestionnaireResponse/{id} returns and no more. Without
            # this a read-tier dry run echoed answers a read hides: a
            # subject-less response's name and birth date came back in full
            # (spec 2026-11-24 §2.2). The input is redacted, not only the
            # extracted rows: a caller may send its own Questionnaire, which
            # can map a free-text answer into `gender` or `address.state`,
            # elements the walker keeps. The rows are redacted as well, at
            # the exit below. An inline response is the caller's own data
            # and is extracted as sent.
            stored = _load_stored("QuestionnaireResponse", qr_id, tenant_id)
            qr = apply_redaction(stored) if stored is not None else None
            from_store = qr is not None
        if qr is None:
            return operation_outcome(
                "error", "invalid",
                "questionnaire-response parameter is required"), 400

        # Step-up gate (writes) fires before any resolution/extraction work
        # so a commit-mode caller without a token is rejected up front.
        # dry_run is a read-shaped preview and skips the gate.
        #
        # The missing-header test reads the RAW header on purpose. The kernel
        # strips the token, so it calls a whitespace-only one absent; this
        # site has always answered that "Invalid step-up token", and only a
        # missing or empty header gets the sentence naming dryRun=true.
        # Branching on `.absent` would move the whitespace case across
        # (tests/test_sdc_extract_step_up_pins.py).
        if not dry_run:
            if not request.headers.get("X-Step-Up-Token"):
                return operation_outcome(
                    "error", "security",
                    "$extract requires X-Step-Up-Token (use dryRun=true to "
                    "preview without committing)"), 401
            if not decide_grant(scope=Scope.WRITE, tenant=tenant).granted:
                return operation_outcome(
                    "error", "security", "Invalid step-up token"), 401

        questionnaire = (_param_resource(params, "questionnaire")
                         or _resolve_referenced_questionnaire(qr, tenant_id))
        if questionnaire is None:
            return operation_outcome(
                "error", "not-found",
                "Questionnaire for the response could not be resolved"), 404

        bundle = extract_resources(qr, questionnaire)

        if not dry_run and any(
                e["resource"]["resourceType"] not in COMMIT_WITHOUT_CONFIRMATION
                for e in bundle["entry"]):
            # #572, #679. Nothing on the human-gated path calls $extract, so
            # commit mode here runs on a step-up token alone. The target type
            # is caller-chosen (definitionExtract's valueCode), so this is an
            # allowlist, not a denylist: a bundle carrying any type not
            # cleared to be written without a human is refused rather than
            # written, and rather than dropped silently after dryRun showed
            # it. The form-fill rail commits those rows after the person
            # confirms them. Measured on main before this: AllergyIntolerance
            # (#668), then Observation and Consent (#679), each stored on the
            # step-up token alone.
            return operation_outcome(
                "error", "business-rule",
                "This bundle carries rows $extract does not commit on a "
                "step-up token alone. The form-fill rail commits them after "
                "human confirmation; $extract does not. Use dryRun=true to "
                "preview them."), 422

        if not dry_run:
            # Only rows in COMMIT_WITHOUT_CONFIRMATION reach here (none
            # today). Step-up + $validate gate the write; the audit row
            # below records the commit even when there was nothing to write.
            for entry in bundle["entry"]:
                result = validator.validate_resource(entry["resource"])
                if not result["valid"]:
                    return jsonify(result["operation_outcome"]), 422
        # The rows and their audit commit together or not at all.
        try:
            if not dry_run:
                _add_bundle(bundle, tenant_id)
            add_audit_event("create" if not dry_run else "read",
                            "QuestionnaireResponse", qr.get("id"),
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id,
                            detail=f"extract; dryRun={dry_run}; "
                                   f"resources={len(bundle['entry'])}")
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("SDC extract commit failed: %s",
                         type(exc).__name__)
            return operation_outcome(
                "error", "exception",
                "Failed to commit extracted resources"), 500

        if from_store:
            # The rows also take from the Questionnaire: observation
            # extraction copies `item.code` verbatim, and a stored
            # Questionnaire is read unredacted, so its upstream `display`
            # would reach the caller. Built after the commit block, so what
            # is written is never the redacted copy.
            bundle = dict(bundle, entry=[
                dict(entry, resource=apply_redaction(entry["resource"]))
                for entry in bundle["entry"]])

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
        # populate_questionnaire's `subject` argument, after _identity_free
        # removes the identity an intake form would otherwise carry to the
        # agent. The form-fill rail fills the real name in process for the
        # human (r6/actions/review.py), not through this route. Reference
        # matching reads only the id, which is why this list is gathered
        # from the stored subject.
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


#: The subject elements $populate over HTTP never fills, whatever the
#: %patient projection allows: the person's name, phone, email and street
#: address. Removed outright, not left as the walker's initial or
#: "[Redacted]" stub, so the leaf reads "not populated" rather than holding
#: a placeholder an agent could mistake for an answer.
_IDENTITY_KEYS = ("name", "telecom", "address")


def _identity_free(subject):
    """The subject this route lets the engine see (spec 2026-11-24 §7 PR 3).

    The MCP `questionnaire_populate` tool hands this route's answer to the
    model, and the stock intake form asks for name, birth date, phone, email
    and street address. So the subject goes through apply_redaction (birth
    date cut to the year, and whatever else the standard profile does to a
    Patient as it changes) and loses the identity elements above. `id` and
    `gender` stay. An inline subject gets the same rule: the route has one.

    The form-fill rail does NOT come through here. It calls
    populate_questionnaire in process with the stored Patient, because the
    person reviews their own name on the page and in the PDF, and the model
    gets only status and a link.
    """
    if not isinstance(subject, dict):
        return subject
    bounded = apply_redaction(subject)
    for key in _IDENTITY_KEYS:
        bounded.pop(key, None)
    return bounded


def _redacted_for_populate(resources):
    """Auto-loaded clinical content, redacted before the engine sees it.

    apply_redaction strips every upstream `display` and `CodeableConcept.text`
    — the two fields real feeds put patient names in — and then re-applies
    labels from r6/terminology.py keyed by code (r6/redaction.py:22-38). That
    is the profile the ruling's own words name ("apply_redaction, then
    terminology labels by code"). Profile.INTAKE was NOT usable here when
    this was written: _intake_strip then popped note/text/SSN identifiers at
    the top level only and left `code.text` untouched. Since #282 intake runs
    apply_redaction too, but it also keeps a Patient's identity fields.

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


def _add_bundle(bundle, tenant_id):
    for entry in bundle["entry"]:
        resource = entry["resource"]
        row = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            tenant_id=tenant_id,
        )
        db.session.add(row)


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
