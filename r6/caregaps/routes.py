# r6/caregaps/routes.py
"""FHIR Patient/$care-gaps — Flask handler.

Registered on r6_blueprint (under /r6/fhir). Read-shaped: tenant-read-
authenticated + AuditEvent (PHI-free detail). Evaluates preventive-care gaps
for ?subject=Patient/<id>, or for the tenant's own Patient when no subject is
supplied, against the tenant's stored Conditions, Observations, Immunizations,
and Procedures. The `subjectResolution` parameter reports which of those
happened, and names the failure when neither could.
"""
import logging
import re
from datetime import datetime, timezone

from flask import request, jsonify

from r6.access import TenantSource, tenant_from_request
from models import db
from r6.models import R6Resource
from r6.audit import add_audit_event
from r6.caregaps.evaluate import evaluate_care_gaps
from r6.caregaps.report import build_caregaps_summary, build_consumer_summary
from r6.safe_read import strict_dumps

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Preventive-care decision support based on published guidelines "
              "(USPSTF/ACIP/ADA). Not a diagnosis or a directive; population-level "
              "adult defaults that individual risk factors can change. Confirm "
              "with your clinician. This is a lightweight consumer-facing check, "
              "not the Da Vinci DEQM $care-gaps operation and not a certified "
              "eCQM; per-rule related_ecqm ids are provided for reconciling with "
              "certified measure engines.")


def resolve_subject(supplied, tenant_id):
    """Return (subject_reference, state).

    Both production callers — CareAgents' get_care_gaps and the care-gaps
    MCP App page — post an empty body with no subject, so `supplied` was
    None and `subject_resources` compared every stored subject.reference
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

    `is_deleted=False` is #422, and the honest version of it: this count
    decides whether the operation may pick a subject at all, and a count
    that includes tombstones decides on a set nobody can see. A tenant
    that deleted its duplicate Patient would still read as ambiguous, the
    symptom would not move, and the only apparent next move would be a
    hard delete against production.

    WOULD, not did. Verified 2026-08-16: `is_deleted = True` is written on
    one line in this repository (r6/routes.py:2824, the demo walkthrough,
    on Permission rows) and no route accepts DELETE, so no Patient row can
    carry a tombstone today. This is a reader fixed ahead of its writer —
    which is the only order in which it can be fixed quietly. Every read
    path in r6/routes.py has filtered since the column existed; the
    feature modules added later did not.
    """
    if supplied:
        return supplied, "supplied"
    # Two rows is all it takes to know the match is ambiguous.
    rows = R6Resource.query.filter_by(
        resource_type="Patient", tenant_id=tenant_id,
        is_deleted=False).limit(2).all()
    if not rows:
        return None, "no-patient"
    if len(rows) > 1:
        return None, "ambiguous-patient"
    return f"Patient/{rows[0].id}", "tenant-default"

def patient_for(subject, tenant_id):
    """The demographics the rules read age and sex from.

    Filtered for the same reason, and it is the half `resolve_subject`
    cannot cover: a caller may SUPPLY `?subject=Patient/<deleted-id>`,
    which never passes through the resolver above. Without this, a deleted
    patient's date of birth still selects which preventive rules fire.
    """
    if not subject or not subject.startswith("Patient/"):
        return None
    row = R6Resource.query.filter_by(
        resource_type="Patient", id=subject.split("/", 1)[1],
        tenant_id=tenant_id, is_deleted=False).first()
    return row.to_fhir_json() if row else None

#: A resource id: the FHIR charset, up to 128 characters. Live Epic Patient
#: ids reach 109 (#878), past FHIR's nominal 64.
_ID = r"[A-Za-z0-9\-.]{1,128}"
_VERSION = rf"(?:/_history/{_ID})?"
#: Each is matched in full (`fullmatch`): nothing before, nothing after, not
#: even a trailing newline. A relative reference is exactly Patient/<id>; an
#: absolute one is a URL whose path ENDS in /Patient/<id>.
_RELATIVE = re.compile(rf"Patient/({_ID}){_VERSION}")
_ABSOLUTE = re.compile(rf"https?://[^/\s?#]+(?:/[^/\s?#]+)*/Patient/({_ID}){_VERSION}")
_URN_UUID = re.compile(rf"urn:uuid:({_ID})")


def referenced_patient_id(ref):
    """The Patient id a reference names, or None.

    Three forms, because real feeds use all three and an exact string test
    against `Patient/<id>` silently dropped the other two (#867):

      Patient/<id>                     relative, optionally /_history/<v>
      https://host/.../Patient/<id>    absolute; the id decides, not the host
      urn:uuid:<id>                    a bundle-local id. The Fasten ingester
                                       and the upload path keep the upstream
                                       id as the row id and store no fullUrl,
                                       so it resolves against that id.

    A `urn:uuid:` names no type, so it is a Patient only when the caller
    compares it with a Patient's id. Every tenant's rows are already scoped
    by tenant_id; the host of an absolute URL widens nothing.
    """
    if not isinstance(ref, str):
        return None
    # A dot-segment can walk a path anywhere ("Patient/../Patient/x",
    # "https://h/a/../Patient/x"), and "." or ".." is no one's id.
    if "." in ref and any(seg in (".", "..") for seg in ref.split("/")):
        return None
    m = (_RELATIVE.fullmatch(ref) or _ABSOLUTE.fullmatch(ref)
         or _URN_UUID.fullmatch(ref))
    # The split above cannot see a `urn:uuid:` form, which has no "/", so
    # "urn:uuid:.." resolved to the id "..". Refuse it on the captured id,
    # which covers every form.
    pid = m.group(1) if m else None
    return None if pid in (".", "..") else pid


def sole_patient_id(tenant_id):
    """The id of the tenant's only live Patient, or None for none or two+."""
    rows = R6Resource.query.filter_by(
        resource_type="Patient", tenant_id=tenant_id,
        is_deleted=False).limit(2).all()
    return rows[0].id if len(rows) == 1 else None


def subject_match(res, patient_id, sole_id):
    """True when `res` is this patient's, False when it is not, and None when
    its subject cannot be read at all (malformed, #869).

    A resource with no subject belongs to the tenant's one Patient when the
    tenant has exactly one and it is the one asked about: a single-patient
    record set is that person's. With two or more it belongs to nobody we
    can name, and is left out rather than guessed.
    """
    subject = res.get("subject")
    if subject is None:
        return sole_id is not None and sole_id == patient_id
    if not isinstance(subject, dict):
        return None
    ref = subject.get("reference")
    if ref is None:
        # An identifier-only or display-only subject names no row we hold.
        return False if "reference" not in subject else None
    if not isinstance(ref, str):
        return None
    return referenced_patient_id(ref) == patient_id


def subject_rows(resource_type, subject, tenant_id, limit=None):
    """(resources, unreadable_count) for the patient `subject` names.

    The one reader labs, care gaps and the brief share, so the three agree
    about whose result a row is (#867). With `limit`, rows are read newest
    first by last update, the order the labs fallback uses, and reading
    stops after `limit` of the patient's own: another person's newer rows
    do not take this patient's slots.

    A `subject` that is not a Patient reference falls back to the old
    exact-string comparison rather than to nothing.
    """
    patient_id = referenced_patient_id(subject)
    sole_id = sole_patient_id(tenant_id) if patient_id else None
    query = R6Resource.query.filter_by(
        resource_type=resource_type, tenant_id=tenant_id, is_deleted=False)
    if limit is not None:
        query = query.order_by(R6Resource.last_updated.desc(),
                               R6Resource.id.desc())
    out, unreadable = [], 0
    for row in query.yield_per(500):
        res = row.to_fhir_json()
        if patient_id is None:
            ref = res.get("subject")
            match = isinstance(ref, dict) and ref.get("reference") == subject
        else:
            match = subject_match(res, patient_id, sole_id)
        if match is None:
            unreadable += 1
        elif match:
            out.append(res)
            if limit is not None and len(out) >= limit:
                break
    return out, unreadable


def subject_resources(resource_type, subject, tenant_id, limit=None):
    """The clinical evidence a gap is evaluated against.

    The most consequential of the three. These rows are what CLOSES a
    gap — a Procedure closes a screening, an Immunization closes a
    vaccine, an Observation closes A1c monitoring. Counting a
    soft-deleted row here tells a patient they are covered by a record
    the system considers deleted, which is the failure direction that
    matters: it withholds a due item rather than repeating one.

    Matched through `subject_rows`, so an absolute-URL or `urn:uuid:`
    reference, or no subject on a one-Patient tenant, counts as the
    patient's (#867), and a malformed subject is skipped (#869).
    """
    return subject_rows(resource_type, subject, tenant_id, limit)[0]


def register_caregaps_routes(blueprint, deps):
    authenticate_tenant_read = deps["authenticate_tenant_read"]

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

    @blueprint.route("/Patient/$care-gaps", methods=["GET", "POST"])
    def care_gaps():
        # enforce_tenant_id already refused an absent or malformed id.
        tenant_id = tenant_from_request(sources=(TenantSource.HEADER,)).id
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        supplied = _subject_from_request()
        subject, state = resolve_subject(supplied, tenant_id)
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
        patient = patient_for(subject, tenant_id)

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
            return subject_resources(resource_type, subject, tenant_id) if subject else []

        as_of = datetime.now(timezone.utc).date().isoformat()
        # A subject we could not resolve is not evaluated, full stop (#542).
        # The route used to set `not_evaluated` and then run the rules anyway,
        # against `patient=None`, which produced two false statements about a
        # person nobody had identified:
        #
        #   - The condition gate in evaluate.py fires BEFORE the age gate, so
        #     a rule with an empty condition list fell through to
        #     `not_applicable` rather than `indeterminate`. A1c is the one
        #     that showed it: `not_applicable: 1` for an unresolved subject.
        #   - The audit detail counted those seven rules as `evaluated=7`.
        #
        # The consumer summary suppressed the rows (#389, #417), so the page
        # looked right while the payload and the audit did not — which is the
        # retro's shape exactly: a check that examined nothing printing the
        # verdict of a check that examined everything
        # (docs/2026-08-02-retro.md). Every other caller of this operation —
        # the MCP tool, the Telegram summary, curl — read the rows.
        if not_evaluated is not None:
            results = []
        else:
            results = evaluate_care_gaps(
                patient, conditions=_for("Condition"),
                observations=_for("Observation"),
                immunizations=_for("Immunization"),
                procedures=_for("Procedure"), as_of=as_of)

        summary = build_caregaps_summary(results)
        # An explicit flag a client can branch on without knowing the reason
        # family. The MCP App page matches the reason list today (#538/#541)
        # and can switch to this; both ship for one release (#542).
        summary["evaluated"] = not_evaluated is None
        consumer = build_consumer_summary(results, not_evaluated=not_evaluated)

        add_audit_event(
            "read", resource_type="Patient", resource_id=None,
            agent_id=request.headers.get("X-Agent-Id"), tenant_id=tenant_id,
            detail=(f"care-gaps; subject={state} evaluated={summary['total']} "
                    f"due={summary['due']}"))
        db.session.commit()

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [
                {"name": "summary", "valueString": strict_dumps(summary)},
                {"name": "consumerSummary", "valueString": strict_dumps(consumer)},
                {"name": "subjectResolution",
                 "valueString": strict_dumps({"state": state, "subject": subject})},
                {"name": "detail", "valueString": strict_dumps(results)},
                {"name": "disclaimer", "valueString": _DISCLAIMER},
            ],
        }), 200
