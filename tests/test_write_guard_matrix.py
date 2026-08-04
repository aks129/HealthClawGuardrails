"""The write-guard matrix: which controls guard which mutating route.

Rebuilt from the code (not copied from the architecture audit) so that
"which controls guard this write" is one reviewable table instead of tribal
knowledge spread across nine blueprints. Every row was verified by reading
the handler and then by probing it through the test client; disagreements
with ``docs/2026-08-02-architecture-audit-and-refactor-plan.md`` are recorded
in the row's ``note``.

Why this file exists (``docs/2026-08-02-retro.md``): the recurring defect
shape here is *a control that looks like one thing and quietly does another*.
A per-route guard set that lives only in reviewers' heads cannot be diffed,
so a route that drops a guard reads exactly like a route that never had one.
The table is the diff.

Assertions go through the Flask test client wherever the guard is observable
at the HTTP boundary, so the access-kernel refactor the plan proposes can
move every one of these call sites without editing this file. Guards only
observable inside the process (an audit row's absence, a log line, a source
idiom) say so at the assertion.

Constitution rule 20: every test carries a ``MUTATION:`` line naming the exact
edit that must turn it red. Constitution rule 19: each assertion names one
property. Known defects are ``xfail(strict=True)`` with an issue number, so
they turn red the moment someone fixes them without closing the issue.
Defects found while building this table that have **no** issue number yet are
pinned as current behavior with a loud docstring instead — an unticketed
defect must not be silently encoded as if it were the design.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import pathlib
import re
import uuid
from dataclasses import dataclass, field

import pytest

TENANT = "test-tenant"                     # public in the test env (conftest)
PRIVATE_TENANT = "guard-matrix-private"    # never in PUBLIC_TENANTS
FOREIGN_TENANT = "guard-matrix-other"
MINT_SECRET = "guard-matrix-internal-secret"


# ---------------------------------------------------------------------------
# Guard vocabulary
# ---------------------------------------------------------------------------

TENANT_HEADER = "tenant-header"      # X-Tenant-Id required, 400 without it
TENANT_FORMAT = "tenant-format"      # ...and its format is validated
READ_AUTH = "read-auth"              # authorize_tenant_read / authenticate_tenant_read
STEP_UP = "step-up"                  # tenant-bound HMAC step-up token
HITL = "human-in-the-loop"           # a human approval artifact
INTERNAL_SECRET = "internal-secret"  # X-Internal-Secret / shared webhook secret
TENANT_FILTER = "tenant-filter"      # the rows it mutates are scoped to the tenant
AUDIT = "audit"                      # emits an AuditEvent


@dataclass(frozen=True)
class Row:
    """One mutating route and the guards that actually fire on it."""

    id: str
    method: str
    path: str
    endpoint: str
    guards: frozenset
    #: Status codes a fully anonymous call may return.
    anon_refusal: tuple = ()
    #: Status when the tenant header is present but no step-up token is. 401
    #: and 403 both appear today; the plan proposes normalizing them (open
    #: question 1), so each is pinned per route rather than assumed uniform.
    step_up_missing_status: int | None = None
    #: Status when the shared secret is configured but not presented.
    internal_secret_status: int | None = None
    #: Open issue number when a guard this row should have is missing.
    defect_issue: str | None = None
    body: dict | None = None
    #: Callable(app) -> dict of path substitutions, for rows whose guard is
    #: only reachable once some state exists.
    setup: object = None
    #: True when the tenant is selected by the request BODY rather than the
    #: X-Tenant-Id header. Such a route cannot decide authorization before
    #: parsing, because the parse is what tells it whom to authorize.
    tenant_from_body: bool = False
    #: True when the handler parses the body BEFORE reaching its step-up
    #: gate. See test_the_step_up_gate_runs_before_the_body_is_parsed — this
    #: is the #267 shape, and it is recorded, not endorsed.
    parses_body_before_step_up: bool = False
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def has(self):
        return self.guards.__contains__


# --- setup hooks -----------------------------------------------------------

def _seed_resource(app, tenant, resource_type, resource_id, body=None):
    from models import db
    from r6.models import R6Resource
    payload = body or {"resourceType": resource_type, "id": resource_id}
    with app.app_context():
        db.session.add(R6Resource(
            tenant_id=tenant, resource_type=resource_type,
            resource_id=resource_id, resource_json=json.dumps(payload)))
        db.session.commit()


def _setup_condition(app):
    """$curatr-* load the row before evaluating anything, so it must exist."""
    for tenant in (PRIVATE_TENANT, TENANT):
        _seed_resource(app, tenant, "Condition", "guard-matrix-cond", {
            "resourceType": "Condition", "id": "guard-matrix-cond",
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "subject": {"reference": "Patient/guard-matrix-pt"}})
    return {}


def _setup_transferable_medication(app):
    """rx-transfer/propose becomes a write only once a draft exists.

    Deliberate design: a read-scoped caller may preview the "nothing to
    transfer" refusal (422) without a write token, so the step-up gate sits
    BELOW the draft build. Without this seed the probe would measure the 422
    and conclude, wrongly, that the route has no step-up gate.
    """
    for tenant in (PRIVATE_TENANT, TENANT):
        _seed_resource(app, tenant, "MedicationRequest", f"gm-med-{tenant}", {
            "resourceType": "MedicationRequest", "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {"text": "Metformin 500 MG"},
            "subject": {"reference": "Patient/guard-matrix-pt"}})
    return {}


def _setup_fasten_connection(app):
    """A unique org_connection_id per run: re-registering an id answers 200/409
    from the idempotency branch, which would mask the guard under test."""
    return {"org_connection_id": f"gm-conn-{uuid.uuid4().hex[:8]}"}


# ---------------------------------------------------------------------------
# THE MATRIX
#
# One row per route that mutates persistent state and carries clinical or
# access-control weight. Routes mutating only infrastructure state (agent-run
# queue, command-center activity log, OAuth client registry) are classified in
# NON_CLINICAL_MUTATORS below and covered by the census test, not probed here.
#
# ROUTE                                   |tenant|fmt|read|step|HITL|secret|filter|audit
# POST /r6/fhir/<type>                     |  x  | x |    | x  | x  |      |  x   |  x
# PUT  /r6/fhir/<type>/<id>                |  x  | x |    | x  | x  |      |  x   |  x
# POST /r6/fhir/Bundle/$ingest-context     |  x  | x |    |flag|    |      |  x   |  x
# POST .../$curatr-apply-fix               |  x  | x |    | x  | x* |      |  x   |  x
# GET  .../$curatr-evaluate                |  x  | x | x  |    |    |      |  x   |  x
# POST /r6/fhir/internal/seed              |     |   |    |    |    |  x*  |  x   |  x
# POST /r6/fhir/internal/ingest-bundle     |  x  | x |    |    |    |  x   |  x   |  x
# POST /r6/fhir/internal/purge-tenant      |     |   |    |    |    |  x*  |  x   |  x
# POST /r6/fhir/internal/bind-telegram     |     | x |    | x  |    |      |  x   |  x
# POST /r6/fhir/demo/agent-loop            |     |   |    |    |    |  x*  |  x   |  x
# POST /r6/fhir/$share-bundle              |  x  | x |    | x  |    |      |  x   |  x
# POST .../QuestionnaireResponse/$extract  |  x  | x | x  | x  |    |      |  x   |  x
# POST /r6/actions/propose                 |  x  | x |    |    |    |      |  x   |  x
# POST /r6/actions/rx-transfer/propose     |  x  | x | x  | x  |    |      |  x   |  x
# POST /r6/actions/<id>/commit             |  x  | x |    | x  |    |      |  x   |  x
# POST /r6/actions/<id>/confirm            |  x  | x |    | x  | x  |      |  x   |  x
# POST /r6/actions/<id>/approval-token     |  x  | x |    |    |    |  x   |  x   |  x
# POST /r6/actions/<id>/review             |  x  | x |    | x  | x  |      |  x   |  x
# POST /r6/actions/callback/<provider>     |     |   |    |    |    |  x   |  x   |  x
# POST /fasten/webhook                     |     |   |    |    |    |  x   |  x   |  x
# POST /fasten/connections                 |  x  | x |    |    |    |      |  x   |  x
# POST /fasten/jobs/<id>/retry             |  x  | x |    |    |    |      |  x   |     <- S-14
# POST /fasten/demo                        |     |   |    |    |    |      |      |  x  <- #305
# POST /r6/smbp/enroll                     |  x  | x |    |    |    |      |  x   |  x
# POST /r6/smbp/reading                    |  x  | x |    | x  |    |      |  x   |  x
# GET  /r6/smbp/report/<id>?format=pdf     |  x  | x | x  |    |    |      |  x   |  x
# POST /shc/ingest                         |  x  | x |    |    |    |  x   |  x   |  x
# POST /r6/ops/reap                        |  x  | x |    | x  |    |      |      |  x  <- #304
# POST /wearables/sync-now                 |  x  | x |    | x  |    |      |      |  x  <- #304
#
# x* = the guard exempts public/synthetic tenants by design.
# Every TENANT_HEADER row now also carries TENANT_FORMAT. Access-kernel slice
# 9 closed the five that did not — see
# test_tenant_row_validates_the_tenant_id_format.
# ---------------------------------------------------------------------------

MATRIX: tuple = (
    Row(
        id="fhir-create",
        method="POST", path="/r6/fhir/Patient", endpoint="r6.create_resource",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, HITL,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"resourceType": "Patient", "id": "guard-matrix-pt"},
        parses_body_before_step_up=True,
        note="Probed with Patient, a NON-clinical type, because for a "
             "clinical type the X-Human-Confirmed hook answers 428 before "
             "the handler's step-up check ever runs — see "
             "test_the_human_confirmation_hook_answers_before_step_up. HITL "
             "here is that header, a known-weak gate (#214): pinned as it "
             "exists, not endorsed.",
    ),
    Row(
        id="fhir-update",
        method="PUT", path="/r6/fhir/Patient/{oid}",
        endpoint="r6.update_resource",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, HITL,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"resourceType": "Patient", "id": "guard-matrix-pt"},
    ),
    Row(
        id="ingest-context",
        method="POST", path="/r6/fhir/Bundle/$ingest-context",
        endpoint="r6.ingest_context",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, TENANT_FILTER, AUDIT}),
        anon_refusal=(400,),
        body={"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {"resourceType": "Patient", "id": "gm-bundle-pt"}}]},
        note="S-3: the step-up gate here is conditional on READ_AUTH_ENABLED "
             "and is the ONLY write with that property — it fails OPEN when "
             "the flag is off. Both branches are pinned by "
             "test_ingest_context_step_up_is_flag_conditional. STEP_UP is "
             "deliberately absent from this guard set: a guard that only "
             "fires behind a flag is not a guard the table can claim.",
    ),
    Row(
        id="curatr-apply-fix",
        method="POST", path="/r6/fhir/Condition/{cid}/$curatr-apply-fix",
        endpoint="r6.curatr_apply_fix",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, HITL,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=403,
        body={"fixes": [{"field_path": "Condition.code.coding[0].code",
                         "new_value": "E11.9"}]},
        setup=_setup_condition,
        note="403, not 401, for a missing token — one of the sites the "
             "plan's open question 1 proposes to normalize. HITL is an "
             "operation-bound, single-use approval token, enforced in "
             "production only (resolve_app_env() == 'production').",
    ),
    Row(
        id="curatr-evaluate",
        method="GET", path="/r6/fhir/Condition/{cid}/$curatr-evaluate",
        endpoint="r6.curatr_evaluate",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, READ_AUTH,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), setup=_setup_condition,
        note="S-10: a GET that persists curation_state + quality_score. The "
             "AuditEvent it writes says 'read'.",
    ),
    Row(
        id="internal-seed",
        method="POST", path="/r6/fhir/internal/seed", endpoint="r6.seed_tenant",
        guards=frozenset({INTERNAL_SECRET, TENANT_FILTER, AUDIT}),
        anon_refusal=(403,), internal_secret_status=403,
        body={"tenant_id": PRIVATE_TENANT}, tenant_from_body=True,
        note="DELIBERATE DIVERGENCE from ingest-bundle: "
             "_internal_mint_authorized exempts public tenants, because "
             "minting or seeding a public tenant grants nothing a public "
             "tenant does not already have. No tenant-header gate — "
             "/internal/ is exempt from the before_request hook and the "
             "selector is the body, which is why an anonymous call with no "
             "body seeds desktop-demo and answers 201.",
    ),
    Row(
        id="internal-ingest-bundle",
        method="POST", path="/r6/fhir/internal/ingest-bundle",
        endpoint="r6.ingest_bundle",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, INTERNAL_SECRET,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400, 403), internal_secret_status=403,
        body={"bundle": {"resourceType": "Bundle", "entry": []}},
        note="DELIBERATE DIVERGENCE from seed/purge: "
             "_internal_ingest_authorized drops the public-tenant exemption, "
             "because authoring a tenant's content is a different risk from "
             "minting its token (stored prompt injection into an LLM "
             "context). Two helpers, two policies, on purpose — see #267.",
    ),
    Row(
        id="internal-purge-tenant",
        method="POST", path="/r6/fhir/internal/purge-tenant",
        endpoint="r6.purge_tenant_route",
        guards=frozenset({INTERNAL_SECRET, TENANT_FILTER, AUDIT}),
        anon_refusal=(403,), internal_secret_status=403,
        body={"tenant_id": PRIVATE_TENANT}, tenant_from_body=True,
        note="Same public-tenant exemption as seed. The delete is audited "
             "with add_audit_event INSIDE the purge transaction, so an "
             "unauditable purge aborts rather than deleting unrecorded.",
    ),
    Row(
        id="internal-bind-telegram",
        method="POST", path="/r6/fhir/internal/bind-telegram",
        endpoint="r6.bind_telegram_chat",
        guards=frozenset({TENANT_FORMAT, STEP_UP, TENANT_FILTER, AUDIT}),
        anon_refusal=(400, 401), step_up_missing_status=401,
        body={"tenant_id": PRIVATE_TENANT, "chat_id": 4242},
        tenant_from_body=True,
        note="Lives under /internal/ but is step-up gated, not secret gated: "
             "the tenant comes from the BODY and the token must be bound to "
             "it. Not an internal-secret route despite the path prefix — a "
             "reader who trusts the prefix gets this one wrong.",
    ),
    Row(
        id="demo-agent-loop",
        method="POST", path="/r6/fhir/demo/agent-loop",
        endpoint="r6.demo_agent_loop",
        guards=frozenset({INTERNAL_SECRET, TENANT_FILTER, AUDIT}),
        anon_refusal=(403,), internal_secret_status=403,
        note="Writes Patient/Observation/Permission AND soft-deletes every "
             "Permission for the tenant. /demo/ is exempt from both the "
             "tenant hook and the human-confirmation hook, so the mint gate "
             "is the only control standing here.",
    ),
    Row(
        id="share-bundle",
        method="POST", path="/r6/fhir/$share-bundle", endpoint="r6.share_bundle",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        note="Mutates nothing; it EXPORTS identified patient data, so it "
             "carries the write gate. In the matrix because its 401 is one "
             "of the three the plan pins alongside create and update.",
    ),
    Row(
        id="sdc-extract",
        method="POST", path="/r6/fhir/QuestionnaireResponse/$extract",
        endpoint="r6.sdc_extract",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, READ_AUTH, STEP_UP,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"resourceType": "Parameters", "parameter": [
            {"name": "questionnaire-response", "resource": {
                "resourceType": "QuestionnaireResponse",
                "status": "completed"}}]},
        parses_body_before_step_up=True,
        note="?dryRun=true skips the step-up gate by design (read-shaped "
             "preview, commits nothing). Exempt from the per-resource "
             "human-confirmation gate, like $ingest-context.",
    ),
    Row(
        id="actions-propose",
        method="POST", path="/r6/actions/propose",
        endpoint="actions.propose_action",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, TENANT_FILTER, AUDIT}),
        anon_refusal=(400,),
        body={"kind": "sms", "payload": {"phone": "+15555550100",
                                         "body": "appointment reminder"}},
        note="DISAGREEMENT with the plan: propose persists a ProposedAction "
             "on the tenant HEADER alone — no read-auth, no step-up — while "
             "its sibling rx-transfer/propose requires both. Safe only "
             "because nothing executes before confirm; it is still an "
             "unauthenticated write into any named tenant's action list, and "
             "the red-flag screen it runs is the only content control.",
    ),
    Row(
        id="actions-rx-transfer-propose",
        method="POST", path="/r6/actions/rx-transfer/propose",
        endpoint="actions.propose_rx_transfer",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, READ_AUTH, STEP_UP,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"to_pharmacy": {"name": "Corner Pharmacy",
                              "phone": "+15555550111"}},
        setup=_setup_transferable_medication,
        parses_body_before_step_up=True,
        note="Parses the body before the step-up gate BY DESIGN, and this is "
             "the one row where that is defensible: a read-scoped caller is "
             "allowed to preview the 'nothing transferable' refusal, so the "
             "gate cannot run until the draft has been built.",
    ),
    Row(
        id="actions-commit",
        method="POST", path="/r6/actions/{action_id}/commit",
        endpoint="actions.commit_action",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        note="DISAGREEMENT with plan §3, which lists the actions step-up "
             "sites as lines 255 and 468 only. commit's gate (routes.py:354) "
             "is a third site; a line-driven migration would have skipped it.",
    ),
    Row(
        id="actions-confirm",
        method="POST", path="/r6/actions/{action_id}/confirm",
        endpoint="actions.confirm_action",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, HITL,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"approved_via": "dashboard"},
        note="The real human-in-the-loop mechanism: a single-use token bound "
             "to audience=action-approval AND to this action id, mintable "
             "only through the internal-secret approval-token endpoint. This "
             "is the gate CLAUDE.md contrasts with the spoofable "
             "X-Human-Confirmed header.",
    ),
    Row(
        id="actions-approval-token",
        method="POST", path="/r6/actions/{action_id}/approval-token",
        endpoint="actions.issue_action_approval_token",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, INTERNAL_SECRET,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400, 403), internal_secret_status=403,
        note="Even public tenants must present the secret here — otherwise "
             "an agent holding an ordinary tenant write token could mint its "
             "own approval credential and close the human loop on itself.",
    ),
    Row(
        id="actions-review",
        method="POST", path="/r6/actions/{action_id}/review",
        endpoint="actions.review_submit",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, HITL,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        note="HITL is the server-side per-item attestation gate: every "
             "medication and allergy row acted on, and 'no known allergies' "
             "never inferred. The row set is re-populated from FHIR so a "
             "crafted POST cannot shrink it.",
    ),
    Row(
        id="actions-callback",
        method="POST", path="/r6/actions/callback/bland",
        endpoint="actions.action_callback",
        guards=frozenset({INTERNAL_SECRET, TENANT_FILTER, AUDIT}),
        anon_refusal=(403,), internal_secret_status=403,
        note="The shared secret arrives as a QUERY PARAMETER, not a header — "
             "the only guard in the matrix that does, so a header-oriented "
             "kernel must not assume otherwise. Fail-closed when "
             "ACTIONS_WEBHOOK_SECRET is unset. The tenant comes from the "
             "action row, never from the request.",
    ),
    Row(
        id="fasten-webhook",
        method="POST", path="/fasten/webhook", endpoint="fasten.webhook",
        guards=frozenset({INTERNAL_SECRET, TENANT_FILTER, AUDIT}),
        anon_refusal=(401,), internal_secret_status=401,
        body={"type": "webhook.test"},
        note="Standard-Webhooks HMAC over the raw body, fail-closed when the "
             "secret is unset. The tenant is resolved from the registered "
             "connection, never from the payload.",
    ),
    Row(
        id="fasten-register-connection",
        method="POST", path="/fasten/connections",
        endpoint="fasten.register_connection",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), setup=_setup_fasten_connection,
        body={"org_connection_id": "guard-matrix-conn"},
        note="The tenant header alone binds an EHR connection to a tenant: "
             "no read-auth and no step-up. The only protection against "
             "claiming someone else's connection is the 409 on an "
             "already-registered id. TENANT_FORMAT arrived with access-kernel "
             "slice 9; before it this route answered 201 to "
             "'../../etc/passwd' and bound the connection to that string.",
    ),
    Row(
        id="fasten-retry-job",
        method="POST", path="/fasten/jobs/guard-matrix-task/retry",
        endpoint="fasten.retry_job",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, TENANT_FILTER}),
        anon_refusal=(400,),
        note="S-14: re-runs a full ingest and emits NO AuditEvent — the only "
             "ingest trigger in the matrix without one. The absence is "
             "pinned by test_audit_absent_where_the_matrix_says_absent so "
             "restoring it forces this row to be updated in the same PR. "
             "TENANT_FORMAT arrived with access-kernel slice 9; before it a "
             "malformed id reached the job lookup as the partition key.",
    ),
    Row(
        id="fasten-demo",
        method="POST", path="/fasten/demo", endpoint="fasten.run_demo",
        guards=frozenset({AUDIT}),
        anon_refusal=(401, 403), defect_issue="#305",
        note="S-2/#305: zero authentication. Writes a connection, a job and "
             "four R6Resource rows. Mitigation the audit does not mention: "
             "the tenant is HARD-CODED to 'fasten-demo-tenant', so this is "
             "an unauthenticated storage-growth primitive, not a "
             "cross-tenant write primitive.",
    ),
    Row(
        id="smbp-enroll",
        method="POST", path="/r6/smbp/enroll", endpoint="smbp.enroll",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, TENANT_FILTER, AUDIT}),
        anon_refusal=(400,),
        body={"patient_ref": "Patient/guard-matrix-pt"},
        note="DISAGREEMENT with the module docstring, which says "
             "'read-shaped endpoints are tenant-authenticated'. enroll is a "
             "WRITE and takes the tenant header only — no read-auth and no "
             "step-up — while its sibling /reading requires step-up. "
             "TENANT_FORMAT arrived with access-kernel slice 9; before it "
             "this route answered 201 to '../../etc/passwd' and persisted an "
             "SMBPSession partitioned by that string.",
    ),
    Row(
        id="smbp-reading",
        method="POST", path="/r6/smbp/reading", endpoint="smbp.reading",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=401,
        body={"patient_ref": "Patient/guard-matrix-pt", "systolic": 128,
              "diastolic": 78, "effective": "2026-08-02T10:00:00Z"},
        note="Writes a clinical Observation but is NOT subject to the "
             "X-Human-Confirmed hook: that hook is registered on "
             "r6_blueprint only and this is the smbp blueprint. The plan's "
             "'eight other blueprints get no before_request hooks' gap, made "
             "concrete on a clinical write. TENANT_FORMAT arrived with "
             "access-kernel slice 9; the 401 measured here before it came "
             "from the step-up gate below the tenant read, so a caller "
             "holding a token carried a malformed id through.",
    ),
    Row(
        id="smbp-report-pdf",
        method="GET", path="/r6/smbp/report/{session_id}?format=pdf",
        endpoint="smbp.report",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, READ_AUTH,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(400,),
        note="S-10: a GET that inserts a DocumentReference, once per render. "
             "Its tenant-format validation comes from authorize_tenant_read, "
             "which checks the pattern before consulting the flag.",
    ),
    Row(
        id="shc-ingest",
        method="POST", path="/shc/ingest", endpoint="shc.ingest",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, INTERNAL_SECRET,
                          TENANT_FILTER, AUDIT}),
        anon_refusal=(401,), internal_secret_status=401,
        body={"resourceType": "Bundle", "entry": []},
        note="Bearer shared secret, fail-closed when unset. The ingest runs "
             "on a background thread, so the HTTP response proves the gate, "
             "not the write. S-4/#306 (no per-entry rollback, raw exception "
             "logged) is pinned separately below. TENANT_FORMAT arrived with "
             "access-kernel slice 9; before it a malformed id was handed to "
             "the ingest thread as the tenant every resource was written "
             "under. The secret is still checked first, so the format check "
             "cannot answer an unauthenticated caller.",
    ),
    Row(
        id="ops-reap",
        method="POST", path="/r6/ops/reap", endpoint="ops.reap",
        guards=frozenset({INTERNAL_SECRET, AUDIT}),
        anon_refusal=(403,), internal_secret_status=403,
        note="FIXED by #308 (was S-1/#304). Was: tenant header + tenant-bound "
             "step-up, then swept every tenant — and because a public tenant "
             "mints a step-up token with no credential, that chain was "
             "unauthenticated. Now infrastructure auth: X-Internal-Secret, "
             "tenant-blind, NO public-tenant exemption, and X-Tenant-Id is "
             "not read at all. The sweep is still global, which is now "
             "honest rather than a lie the guard set told. TENANT_FILTER is "
             "correctly absent: this endpoint is operator-scoped by design, "
             "not tenant-scoped.",
    ),
    Row(
        id="wearables-sync-now",
        method="POST", path="/wearables/sync-now",
        endpoint="wearables.sync_now",
        guards=frozenset({TENANT_HEADER, TENANT_FORMAT, STEP_UP, AUDIT}),
        anon_refusal=(400,), step_up_missing_status=403,
        defect_issue="#304",
        note="DISAGREEMENT with the plan: S-1 names only r6/ops/routes.py, "
             "but run_once() iterates WearableConnection.query.all() across "
             "every tenant behind one tenant's step-up token and ingests "
             "Observations into those tenants. Same defect class, second "
             "site, unlisted. It also refuses with 403 where ops refuses "
             "with 401 — a fourth status-code dialect for one failure.",
    ),
)

BY_ID = {row.id: row for row in MATRIX}


# ---------------------------------------------------------------------------
# Mutating routes that are NOT clinical/access-control writes. Classified so
# the census can prove the matrix is complete; each names why it is out.
# ---------------------------------------------------------------------------

NON_CLINICAL_MUTATORS = {
    # Durable agent-run queue: execution bookkeeping, not patient data.
    # NOTE: none of these emits an AuditEvent.
    "agent_runs.create_agent_run": "queue: tenant session or step-up",
    "agent_runs.cancel_agent_run": "queue: tenant session or step-up",
    "agent_runs.claim_agent_run": "queue: internal secret",
    "agent_runs.heartbeat_agent_run": "queue: internal secret",
    "agent_runs.transition_agent_run": "queue: internal secret",
    "agent_runs.append_agent_run_event": "queue: internal secret",
    "agent_runs.create_agent_tool_call": "queue: internal secret",
    "agent_runs.transition_agent_tool_call": "queue: internal secret",
    "agent_runs.finalize_agent_run": "queue: internal secret",
    "agent_runs.resume_agent_run": "queue: internal secret",
    "agent_runs.reconcile_agent_tool_call": "queue: reconciliation secret",
    # Command-center activity log: _authz_write = session or step-up.
    "command_center.api_conversations_create": "activity log: session or step-up",
    "command_center.api_tasks_create": "activity log: session or step-up",
    "command_center.api_tasks_update": "activity log: session or step-up",
    "command_center.api_generate_link": "mints a signed link; no store write",
    "command_center.logout": "clears the session cookie only",
    # Credential minting and non-store side effects.
    "r6.issue_step_up_token": "mints a token; no store write",
    "r6.register_client": "OAuth dynamic client registration (token store)",
    "r6.token": "OAuth token grant (token store)",
    "r6.revoke": "OAuth revocation (token store)",
    # POSTs that persist nothing in this system.
    "r6.validate_resource": "$validate persists nothing; audits 'validate'",
    "r6.import_stub": "$import-stub persists nothing; audits only",
    "r6.evaluate_permission": "read-only policy evaluation",
    "r6.interpret_labs": "read-only lab interpretation",
    "r6.care_gaps": "read-only care-gap evaluation",
    "r6.evaluate_measure": "read-only measure evaluation",
    "r6.sdc_populate": "read-only questionnaire population",
    "email_inbound.inbound_email": "forwards mail; no local persistence",
    "api_subscribe": "external mailing-list call; no local persistence",
}

# GET routes that mutate persistent state. Every one breaks the read/write
# split a reviewer relies on; they are listed so a NEW one cannot arrive
# unnoticed. S-10 in the audit names two of these six.
KNOWN_GET_MUTATORS = {
    "r6.curatr_evaluate": "S-10: persists curation_state + quality_score",
    "smbp.report": "S-10: inserts a DocumentReference on ?format=pdf",
    "fasten.agent_access": "mint-once claim on FastenConnection (deliberate)",
    "actions.action_status": "lazy expiry flips a stale proposal to expired",
    "agent_runs.get_agent_worker_health": "expire_overdue_runs() on a probe",
    "wearables.oauth_callback": "upserts WearableConnection (signed state)",
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def secrets(monkeypatch):
    """Production posture: every shared secret configured.

    Without this the internal gates fall back to "open outside production",
    so an anonymous-refusal assertion would pass for the wrong reason — the
    retro's rule: ask what else could make the check pass.
    """
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT_SECRET)
    monkeypatch.setenv("ACTIONS_WEBHOOK_SECRET", MINT_SECRET)
    monkeypatch.setenv("SHC_WEBHOOK_SECRET", MINT_SECRET)
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET", MINT_SECRET)
    monkeypatch.delenv("FASTEN_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    return MINT_SECRET


def token_for(tenant: str, **kwargs) -> str:
    from r6.stepup import generate_step_up_token
    return generate_step_up_token(tenant, **kwargs)


def concrete(row: Row, subs=None) -> str:
    """Fill a row's path template.

    Ids need not exist: every gate in the matrix is evaluated before the row
    is loaded. Where that is not true the row carries a `setup` hook.
    """
    values = {"oid": "guard-matrix-pt", "cid": "guard-matrix-cond",
              "action_id": "guard-matrix-action",
              "session_id": "guard-matrix-session",
              "org_connection_id": "guard-matrix-conn"}
    values.update(subs or {})
    return row.path.format(**values)


def call(client, row: Row, headers=None, path=None, body=..., app=None):
    subs = row.setup(app) if (row.setup and app is not None) else {}
    payload = row.body if body is ... else body
    kwargs = {"headers": headers or {}}
    if row.method != "GET":
        kwargs["json"] = payload if payload is not None else {}
    return client.open(path or concrete(row, subs), method=row.method,
                       **kwargs)


def audit_rows(client, tenant):
    """Read the audit trail back through the API, not the ORM."""
    resp = client.get("/r6/fhir/AuditEvent?_count=200",
                      headers={"X-Tenant-Id": tenant,
                               "X-Step-Up-Token": token_for(tenant)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return [entry["resource"]
            for entry in resp.get_json().get("entry", [])]


# ---------------------------------------------------------------------------
# 1. Anonymous refusal — the property the whole matrix exists for
# ---------------------------------------------------------------------------

def _params(rows, defect_marks=None):
    for row in rows:
        marks = list((defect_marks or {}).get(row.id, ()))
        yield pytest.param(row, id=row.id, marks=marks)


_FASTEN_DEMO_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="#305: /fasten/demo writes a connection, a job and four "
           "R6Resource rows with no credential of any kind. Its siblings "
           "(/demo/agent-loop, /internal/seed, /internal/purge-tenant) all "
           "require the mint secret.")


@pytest.mark.parametrize(
    "row", list(_params(MATRIX, {"fasten-demo": (_FASTEN_DEMO_XFAIL,)})))
def test_a_mutating_route_refuses_an_anonymous_caller(client, app, row,
                                                      secrets):
    """Every mutating route refuses a caller carrying no credential at all.

    MUTATION: delete the auth gate from any one handler in MATRIX — e.g.
    remove the `if not step_up_token` block from r6.create_resource, or the
    `_internal_ingest_authorized(...)` call from r6.ingest_bundle. That row
    goes red here.

    This is the one assertion that must hold for every row no matter which
    control does the refusing, so it survives the access-kernel migration
    that changes *which* control answers.
    """
    resp = call(client, row, headers={}, app=app)
    assert resp.status_code in row.anon_refusal, (
        f"{row.method} {row.path} answered {resp.status_code} to an "
        f"anonymous caller; expected one of {row.anon_refusal}. Guards on "
        f"this row: {sorted(row.guards)}")


@pytest.mark.parametrize(
    "row", list(_params(MATRIX, {"fasten-demo": (_FASTEN_DEMO_XFAIL,)})))
def test_authorization_is_decided_before_the_body_is_parsed(client, app, row,
                                                            secrets):
    """An unparseable body from an anonymous caller still gets the auth refusal.

    MUTATION: move the auth gate in any handler below its
    `request.get_json(...)` call — literally the #267 defect. That row's
    refusal changes from the pinned status to a parse error and this goes red.

    Retro finding #4: `assert status in (400, 403)` passed whether auth ran
    before or after the parse, because the parse error is also a 400. Pinning
    the SAME status for a well-formed and an unparseable body is what closes
    that hole: a handler that parses first cannot produce the identical
    answer for both.

    Anonymous is only half the probe — on most rows the tenant hook answers
    first and would mask a reordering deeper in the handler. The step-up half
    is test_the_step_up_gate_runs_before_the_body_is_parsed below; mutation
    testing found that gap, it was not designed away.
    """
    if row.method == "GET":
        pytest.skip("no body to parse")
    if row.tenant_from_body:
        pytest.skip(
            "the tenant selector for this route IS the body, so it cannot "
            "decide authorization before parsing — the parse is what names "
            "whom to authorize. Its gate is covered by "
            "test_internal_secret_row_refuses_a_wrong_secret and "
            "test_internal_seed_refuses_a_private_tenant_without_the_secret.")
    resp = client.open(concrete(row, row.setup(app) if row.setup else {}),
                       method=row.method, data=b'{"unterminated": ',
                       content_type="application/json")
    assert resp.status_code in row.anon_refusal, (
        f"{row.method} {row.path} answered {resp.status_code} to an "
        f"unparseable anonymous body, but {row.anon_refusal} to a "
        f"well-formed one — the body is being parsed before authorization")


def test_internal_seed_refuses_a_private_tenant_without_the_secret(client,
                                                                   secrets):
    """The seed gate is real for every tenant that is not synthetic.

    MUTATION: delete the `_internal_mint_authorized` call in r6.seed_tenant.
    """
    resp = client.post("/r6/fhir/internal/seed",
                       json={"tenant_id": PRIVATE_TENANT})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. Step-up
# ---------------------------------------------------------------------------

STEP_UP_ROWS = [r for r in MATRIX if r.has(STEP_UP)]


@pytest.mark.parametrize("row", STEP_UP_ROWS, ids=lambda r: r.id)
def test_step_up_row_refuses_a_missing_token(client, app, row, secrets):
    """A step-up row refuses when the tenant header is present but no token is.

    MUTATION: remove the `if not step_up_token: return ...` guard from the
    row's handler. Its answer moves off the pinned 401/403.

    The status is pinned PER ROUTE, not assumed uniform: 401 at nine sites,
    403 at $curatr-apply-fix and /wearables/sync-now. The plan proposes
    normalizing these (open question 1); until an owner decides, this records
    the wire behavior external clients have already learned.
    """
    resp = call(client, row, headers={"X-Tenant-Id": PRIVATE_TENANT}, app=app)
    assert resp.status_code == row.step_up_missing_status, (
        f"{row.method} {row.path} answered {resp.status_code} with no "
        f"step-up token; pinned status is {row.step_up_missing_status}")


@pytest.mark.parametrize("row", STEP_UP_ROWS, ids=lambda r: r.id)
def test_step_up_row_refuses_a_foreign_tenants_token(client, app, row,
                                                     secrets):
    """A valid token for tenant B never authorizes a write to tenant A.

    MUTATION: drop the `tenant_id` argument from the row's
    validate_step_up_token call, so it validates the signature only. The
    foreign token starts being accepted and this goes red.

    This is the assertion that separates "a token was presented" from "a
    token bound to THIS tenant was presented".
    """
    resp = call(client, row,
                headers={"X-Tenant-Id": PRIVATE_TENANT,
                         "X-Step-Up-Token": token_for(FOREIGN_TENANT)},
                app=app)
    assert resp.status_code == row.step_up_missing_status, (
        f"{row.method} {row.path} answered {resp.status_code} to a token "
        f"bound to a different tenant; expected {row.step_up_missing_status}")


@pytest.mark.parametrize("row", STEP_UP_ROWS, ids=lambda r: r.id)
def test_step_up_row_refuses_an_expired_token(client, app, row, secrets):
    """Expiry is enforced on every step-up row, not only in stepup.py's unit test.

    MUTATION: delete the `exp` check in r6/stepup.py validate_step_up_token.
    Every row in this parametrization goes red at once.
    """
    resp = call(client, row,
                headers={"X-Tenant-Id": PRIVATE_TENANT,
                         "X-Step-Up-Token": token_for(PRIVATE_TENANT,
                                                      ttl_seconds=-1)},
                app=app)
    assert resp.status_code == row.step_up_missing_status, (
        f"{row.method} {row.path} accepted an expired token "
        f"({resp.status_code})")


@pytest.mark.parametrize("row", STEP_UP_ROWS, ids=lambda r: r.id)
def test_the_step_up_gate_runs_before_the_body_is_parsed(client, app, row,
                                                         secrets):
    """A credentialless caller WITH a tenant header still gets the step-up
    refusal for an unparseable body.

    MUTATION: move the step-up block in r6/smbp/routes.py reading() (or any
    other row's) below its `request.get_json(...)` call and add a 400 for a
    missing body. That row goes red.

    The anonymous variant of this test cannot see such a reordering: on most
    rows the tenant hook answers 400 first, so the whole handler is never
    entered and both the fixed and the broken orderings look identical. That
    is the retro's "passed whether auth ran before or after the parse" shape,
    and it survived in this file until a mutation run exposed it.

    UNTICKETED DEFECT (the rows flagged parses_body_before_step_up): POST
    /r6/fhir/<type> and $extract parse the body before their step-up gate,
    so a caller holding nothing but a tenant header reaches the JSON parser.
    That is exactly what #267 fixed on /internal/ingest-bundle — see
    test_deep_nesting_is_refused_only_on_the_path_that_was_patched for the
    crash lever it leaves open. PUT /r6/fhir/<type>/<id> gates first, so
    create and update disagree with each other on a security-relevant
    ordering: the plan's "per-route convention" thesis in one diff. Worth an
    issue.
    """
    if row.method == "GET" or row.tenant_from_body:
        pytest.skip("no body, or the body is the tenant selector")
    resp = client.open(concrete(row, row.setup(app) if row.setup else {}),
                       method=row.method, data=b'{"unterminated": ',
                       content_type="application/json",
                       headers={"X-Tenant-Id": PRIVATE_TENANT})
    if row.parses_body_before_step_up:
        assert resp.status_code != row.step_up_missing_status, (
            f"{row.method} {row.path} now gates before parsing. Good — "
            f"remove parses_body_before_step_up from its row and update this "
            f"docstring's defect list in the same PR.")
    else:
        assert resp.status_code == row.step_up_missing_status, (
            f"{row.method} {row.path} answered {resp.status_code} to an "
            f"unparseable body with no credential, but "
            f"{row.step_up_missing_status} to a well-formed one — the body "
            f"is being parsed before the step-up gate")


def test_the_human_confirmation_hook_answers_before_step_up(client):
    """On a clinical write the HITL hook answers 428 before the step-up gate.

    MUTATION: unregister check_human_confirmation from r6_blueprint, or move
    it after the handler's step-up check. The 428 becomes a 401.

    Worth pinning because the ordering is invisible at the call site and
    surprising: an unauthenticated caller learns "this resource type is
    clinical" before being asked for a credential. It also means the
    create/update rows above must be probed with a NON-clinical type to
    observe their 401 at all — the kind of detail that makes a hand-written
    guard test pass for the wrong reason.
    """
    clinical = {"resourceType": "Observation", "id": "guard-matrix-clin",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org",
                                     "code": "2339-0"}]}}
    resp = client.post("/r6/fhir/Observation",
                       headers={"X-Tenant-Id": PRIVATE_TENANT},
                       json=clinical)
    assert resp.status_code == 428, (
        "the human-confirmation hook no longer precedes the step-up gate; "
        "update the fhir-create/fhir-update rows if this was intended")


def test_confirm_requires_an_action_bound_single_use_token(client, app,
                                                           secrets):
    """A generic tenant write token can never execute an action.

    MUTATION: drop `require_audience` / `require_operation` from the
    validate_step_up_token call in actions.confirm_action.

    This is the human-in-the-loop guarantee in one assertion: the credential
    that executes is mintable only by the approval surface (which itself
    needs the internal secret), never by an agent holding an ordinary write
    token.
    """
    from models import db
    from r6.actions.models import ProposedAction

    with app.app_context():
        action = ProposedAction(
            tenant_id=PRIVATE_TENANT, kind="sms",
            payload={"phone": "+15555550100", "body": "hello"})
        action.status = "awaiting_confirmation"
        db.session.add(action)
        db.session.commit()
        action_id = action.id

    resp = client.post(f"/r6/actions/{action_id}/confirm",
                       headers={"X-Tenant-Id": PRIVATE_TENANT,
                                "X-Step-Up-Token": token_for(PRIVATE_TENANT)},
                       json={"approved_via": "dashboard"})
    assert resp.status_code == 401, (
        "a generic tenant write token executed an action; only an "
        "action-bound approval credential may")


# ---------------------------------------------------------------------------
# 3. Internal secret
# ---------------------------------------------------------------------------

SECRET_ROWS = [r for r in MATRIX if r.has(INTERNAL_SECRET)]


@pytest.mark.parametrize("row", SECRET_ROWS, ids=lambda r: r.id)
def test_internal_secret_row_refuses_a_wrong_secret(client, app, row, secrets):
    """A server-to-server route refuses a caller presenting the WRONG secret.

    MUTATION: replace hmac.compare_digest with `provided == expected or not
    provided` in _internal_ingest_authorized / _internal_mint_authorized, or
    delete the verify_webhook call in fasten.webhook.

    A wrong secret is the sharper probe: a gate that only checks presence
    passes the anonymous test above and fails here.
    """
    headers = {"X-Tenant-Id": PRIVATE_TENANT,
               "X-Internal-Secret": "not-the-secret",
               "Authorization": "Bearer not-the-secret"}
    path = concrete(row)
    if row.id == "actions-callback":
        path = f"{path}?secret=not-the-secret&action_id=guard-matrix-action"
    resp = call(client, row, headers=headers, path=path, app=app)
    assert resp.status_code == row.internal_secret_status, (
        f"{row.method} {row.path} answered {resp.status_code} to a wrong "
        f"secret; pinned status is {row.internal_secret_status}")


def test_ingest_bundle_and_seed_diverge_on_the_public_tenant_exemption(
        client, secrets):
    """The two internal gates are deliberately different, and stay different.

    MUTATION: point ingest_bundle at `_internal_mint_authorized` instead of
    `_internal_ingest_authorized` — the "obvious cleanup" a refactor invites.
    The second assertion goes red.

    Not a divergence to normalize: minting a token for a public tenant grants
    nothing extra, but AUTHORING a public tenant's content is stored prompt
    injection into an LLM context (#267). One control, one property — so two
    controls.
    """
    seed = client.post("/r6/fhir/internal/seed", json={"tenant_id": TENANT})
    assert seed.status_code == 201, (
        "seeding a PUBLIC tenant without the internal secret must stay "
        "allowed — the demo dashboard has nowhere to hold a secret")

    ingest = client.post(
        "/r6/fhir/internal/ingest-bundle",
        headers={"X-Tenant-Id": TENANT},
        json={"bundle": {"resourceType": "Bundle", "entry": []}})
    assert ingest.status_code == 403, (
        "ingesting into a PUBLIC tenant without the internal secret must be "
        "refused; the public-tenant exemption does not apply to authoring")


# ---------------------------------------------------------------------------
# 4. Tenant header and tenant format
# ---------------------------------------------------------------------------

TENANT_ROWS = [r for r in MATRIX if r.has(TENANT_HEADER)]


@pytest.mark.parametrize("row", TENANT_ROWS, ids=lambda r: r.id)
def test_tenant_row_refuses_a_missing_tenant_header(client, app, row, secrets):
    """A tenant-scoped route refuses a request with no X-Tenant-Id.

    MUTATION: default the tenant instead of refusing — e.g. `tenant_id =
    request.headers.get('X-Tenant-Id', 'default')` in the row's handler, or
    delete the enforce_tenant_id before_request hook.

    Tenant selection is header-only across this system; a route that accepts
    a body or query selector instead becomes a cross-tenant write oracle.
    """
    resp = call(client, row,
                headers={"X-Internal-Secret": MINT_SECRET,
                         "Authorization": f"Bearer {MINT_SECRET}"},
                app=app)
    assert resp.status_code == 400, (
        f"{row.method} {row.path} answered {resp.status_code} with no tenant "
        f"header; expected 400")


@pytest.mark.parametrize("row", TENANT_ROWS, ids=lambda r: r.id)
def test_tenant_row_validates_the_tenant_id_format(client, app, row, secrets):
    """A tenant id is refused or accepted by format exactly as the matrix says.

    MUTATION: delete `_TENANT_ID_PATTERN.fullmatch` from enforce_tenant_id
    (or `_TENANT_PATTERN.match` from actions/_tenant_or_none, or the
    `tenant_from_request` call from any slice-9 blueprint). Those rows go red.
    Adding validation to a row WITHOUT it also goes red, which is the point:
    the matrix must be updated in the same PR.

    CLOSED by access-kernel slice 9. This docstring used to record an
    unticketed defect: /fasten/connections, /fasten/jobs/<id>/retry,
    /r6/smbp/enroll and /shc/ingest accepted ANY string as a tenant id, so
    '../../etc/passwd' became a partition key and landed in audit detail.
    /r6/smbp/reading was a fifth site the probe could not see, because its
    step-up gate answered 401 below the unvalidated tenant read. All five now
    resolve the header through `tenant_from_request`, and their rows carry
    TENANT_FORMAT. Per-route refusals are pinned in
    tests/test_tenant_format_blueprints.py.
    """
    resp = call(client, row,
                headers={"X-Tenant-Id": "../../etc/passwd",
                         "X-Internal-Secret": MINT_SECRET,
                         "Authorization": f"Bearer {MINT_SECRET}"},
                app=app)
    if row.has(TENANT_FORMAT):
        assert resp.status_code in (400, 401, 403), (
            f"{row.method} {row.path} answered {resp.status_code} to a "
            f"malformed tenant id; the matrix says its format is validated")
    else:
        assert resp.status_code not in (400,), (
            f"{row.method} {row.path} now refuses a malformed tenant id "
            f"({resp.status_code}). Add TENANT_FORMAT to its guard set and "
            f"delete it from the unticketed-defect list in this docstring.")


# ---------------------------------------------------------------------------
# 5. Audit — present where the matrix claims it, absent where it does not
# ---------------------------------------------------------------------------

def test_a_successful_clinical_write_emits_an_audit_event(client, tenant_id,
                                                          auth_headers):
    """A create is visible in the audit trail read back through the API.

    MUTATION: remove the record_audit_event call from r6.create_resource.

    Read back over HTTP rather than from the ORM, so this survives the plan's
    move of audit into the access kernel.
    """
    headers = dict(auth_headers)
    headers["X-Human-Confirmed"] = "true"
    obs = {"resourceType": "Observation", "id": "guard-matrix-audited",
           "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "2339-0"}]}}
    created = client.post("/r6/fhir/Observation", headers=headers, json=obs)
    assert created.status_code == 201, created.get_data(as_text=True)

    events = audit_rows(client, tenant_id)
    assert any("guard-matrix-audited" in json.dumps(event)
               for event in events), "a successful create emitted no AuditEvent"


def test_smbp_reading_emits_an_audit_event(client, tenant_id, auth_headers):
    """The clinical write on a NON-r6 blueprint is audited too.

    MUTATION: remove the record_audit_event call from r6/smbp/routes.py
    reading().

    Blueprint-local audit calls are the plan's weak point: r6_blueprint has
    hooks, the other eight do not. This pins one of the eight.
    """
    resp = client.post("/r6/smbp/reading", headers=auth_headers,
                       json=BY_ID["smbp-reading"].body)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    observation_id = resp.get_json()["observation_id"]

    events = audit_rows(client, tenant_id)
    assert any(observation_id in json.dumps(event) for event in events), (
        "the SMBP reading write emitted no AuditEvent")


def test_a_refused_write_leaves_no_resource_behind(client, tenant_id):
    """A refused write persists nothing — the #267 regression, pinned per route.

    MUTATION: move the step-up check in r6.create_resource below the
    db.session.add / db.session.commit pair.
    """
    patient = {"resourceType": "Patient", "id": "guard-matrix-refused"}
    refused = client.post("/r6/fhir/Patient",
                          headers={"X-Tenant-Id": tenant_id}, json=patient)
    assert refused.status_code == 401

    read = client.get("/r6/fhir/Patient/guard-matrix-refused",
                      headers={"X-Tenant-Id": tenant_id})
    assert read.status_code == 404, (
        "a write refused for want of a step-up token still persisted a row")


def test_audit_absent_where_the_matrix_says_absent(client, app, tenant_id,
                                                   monkeypatch):
    """/fasten/jobs/<id>/retry emits no AuditEvent (S-14). Pinned as absent.

    MUTATION: add a record_audit_event call to r6/fasten/routes.py retry_job.
    This test goes red — deliberately. An absence recorded in the matrix is a
    claim like any other; when S-14 is fixed this is what forces the matrix
    row to be updated in the same PR rather than months later.
    """
    from models import db
    from r6.fasten import routes as fasten_routes
    from r6.fasten.models import FastenJob
    from r6.models import AuditEventRecord

    # The real handler starts a daemon ingest thread that shares this
    # process's SQLite connection; that is a harness hazard, not the guard
    # under test.
    monkeypatch.setattr(fasten_routes, "_launch_ingest",
                        lambda *args, **kwargs: None)

    with app.app_context():
        db.session.add(FastenJob(
            task_id="guard-matrix-task",
            org_connection_id="guard-matrix-conn",
            tenant_id=tenant_id, status="failed",
            download_links_json=json.dumps(
                ["https://example.invalid/export.ndjson"])))
        db.session.commit()

    # Counted through the ORM, not the API: reading the trail over HTTP
    # audits the read, so the count would grow on its own and the assertion
    # would pass or fail for a reason unrelated to retry_job.
    with app.app_context():
        before = AuditEventRecord.query.count()

    resp = client.post("/fasten/jobs/guard-matrix-task/retry",
                       headers={"X-Tenant-Id": tenant_id})
    assert resp.status_code == 202, resp.get_data(as_text=True)

    with app.app_context():
        after = AuditEventRecord.query.count()
    assert after == before, (
        "retry_job now emits an AuditEvent — S-14 is fixed. Add AUDIT to the "
        "fasten-retry-job row's guard set and delete this test in the same PR.")


# ---------------------------------------------------------------------------
# 6. Tenant filtering — the guard the ops sweeps do not have
# ---------------------------------------------------------------------------

def test_a_write_lands_only_in_the_tenant_that_authorized_it(client, tenant_id,
                                                             auth_headers):
    """A create authorized for tenant A is invisible to tenant B.

    MUTATION: drop `tenant_id=tenant_id` from the R6Resource constructor in
    r6.create_resource, or from the read filter in r6.read_resource.
    """
    headers = dict(auth_headers)
    headers["X-Human-Confirmed"] = "true"
    obs = {"resourceType": "Observation", "id": "guard-matrix-isolated",
           "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "2339-0"}]}}
    assert client.post("/r6/fhir/Observation", headers=headers,
                       json=obs).status_code == 201

    foreign = client.get(
        "/r6/fhir/Observation/guard-matrix-isolated",
        headers={"X-Tenant-Id": FOREIGN_TENANT,
                 "X-Step-Up-Token": token_for(FOREIGN_TENANT)})
    assert foreign.status_code == 404, (
        "another tenant can read a resource written under this tenant")


@pytest.mark.xfail(
    strict=True,
    reason="#304: /r6/ops/reap validates a step-up token bound to ONE tenant "
           "and then sweeps ProposedAction.query.filter_by(status=...) across "
           "EVERY tenant, driving state transitions and Telegram pushes on "
           "records the caller has no claim to.")
def test_ops_reap_only_touches_the_authenticated_tenant(client, app, secrets):
    """A reap authenticated for tenant A must not transition tenant B's actions.

    MUTATION (once fixed): delete the tenant predicate from the three sweep
    queries in r6/ops/routes.py reap().
    """
    from datetime import timedelta

    from models import db
    from r6.actions.models import ProposedAction, _utcnow

    with app.app_context():
        victim = ProposedAction(
            tenant_id=FOREIGN_TENANT, kind="sms",
            payload={"phone": "+15555550100", "body": "x"})
        victim.status = "awaiting_confirmation"
        victim.expires_at = _utcnow() - timedelta(hours=1)
        db.session.add(victim)
        db.session.commit()
        victim_id = victim.id

    resp = client.post("/r6/ops/reap",
                       headers={"X-Tenant-Id": PRIVATE_TENANT,
                                "X-Step-Up-Token": token_for(PRIVATE_TENANT)})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with app.app_context():
        after = db.session.get(ProposedAction, victim_id)
        assert after.status == "awaiting_confirmation", (
            f"a reap authenticated for {PRIVATE_TENANT} moved "
            f"{FOREIGN_TENANT}'s action to {after.status}")


def test_wearables_sync_only_touches_the_authenticated_tenant(client, app,
                                                              monkeypatch):
    """A manual sync authenticated for tenant A must not touch tenant B's rows.

    MUTATION (once fixed): remove the tenant argument from run_once() so it
    reverts to WearableConnection.query.all().
    """
    from models import db
    from r6.wearables.models import WearableConnection

    monkeypatch.setenv("OPEN_WEARABLES_URL", "http://wearables.invalid")
    with app.app_context():
        conn = WearableConnection(
            tenant_id=FOREIGN_TENANT, provider="fitbit",
            ow_user_id="hc-other", last_sync_status="never")
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

    resp = client.post("/wearables/sync-now",
                       headers={"X-Tenant-Id": PRIVATE_TENANT,
                                "X-Step-Up-Token": token_for(PRIVATE_TENANT)})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with app.app_context():
        after = db.session.get(WearableConnection, conn_id)
        assert after.last_sync_status == "never", (
            f"a sync authenticated for {PRIVATE_TENANT} touched "
            f"{FOREIGN_TENANT}'s connection (status={after.last_sync_status})")


# ---------------------------------------------------------------------------
# 7. Named defects
# ---------------------------------------------------------------------------

def test_shc_ingest_never_logs_a_raw_exception(app, caplog, monkeypatch):
    """The SHC ingest failure path must not put exception text in the log.

    MUTATION (once fixed): change the logger.warning back to passing `exc`
    instead of `type(exc).__name__`.

    Observed inside the process, deliberately: the ingest runs on a daemon
    thread with no HTTP response to carry this, so caplog is the only honest
    vantage point.
    """
    import r6.fasten.ingester as ingester
    from r6.shc import routes as shc_routes

    marker = "Confidentialgiven Secretsurname 123-45-6789"

    def exploding_ingest(resource, tenant_id, **kwargs):
        raise RuntimeError(f"INSERT INTO r6_resources ... [{marker}]")

    monkeypatch.setattr(ingester, "_ingest_one", exploding_ingest)
    with caplog.at_level(logging.WARNING):
        shc_routes._ingest_bundle(
            app, [{"resourceType": "Patient", "id": "gm-shc"}],
            PRIVATE_TENANT, "flexpa", "guard-matrix-job")

    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in blob, (
        "the SHC ingest failure path logged the raw exception text")


def test_no_write_path_indexes_the_step_up_tuple():
    """validate_step_up_token is always destructured, never indexed.

    Was a strict xfail for #307 and is now enforced: both offenders are
    fixed (`r6/agent_runs/routes.py` indexed `[0]`, `r6/command_center/
    routes.py` destructured and dropped the reason). The pin flips in the
    same PR as the fix — a strict xfail left in place after its defect is
    gone fails as "unexpectedly passing", which reads as a broken test
    rather than a closed finding.

    MUTATION: rewrite any destructured call site as
    `validate_step_up_token(token, tenant)[0]`. This goes red.

    Observed at the source, deliberately: `[0]` returns the correct boolean
    today, so there is NO behavioral difference to assert at the HTTP
    boundary — and a test that cannot observe the property it claims to guard
    is decoration. The hazard is that `[0]` sits one keystroke from the
    bypass (`if validate_step_up_token(...)`) and reads as though it had been
    reviewed. A source assertion is the only honest guard for a latent idiom.
    """
    indexed = re.compile(r"validate_step_up_token\([^)]*\)\s*\[0\]")
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(root))
        for path in sorted((root / "r6").rglob("*.py"))
        if indexed.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "the step-up result is indexed rather than destructured in: "
        + ", ".join(offenders))


def test_ingest_context_step_up_is_flag_conditional(client, monkeypatch,
                                                    tenant_id):
    """$ingest-context is the only write whose step-up gate depends on a flag.

    MUTATION: make the gate unconditional (delete the `if
    _read_auth_enabled():` wrapper around it in r6/routes.py). The first
    assertion goes red.

    S-3 from the audit, pinned rather than xfailed because no issue number
    was opened for it. Recorded so the fail-open branch is a stated property
    with a test attached instead of an accident nobody wrote down. Every
    other write in MATRIX gates unconditionally.
    """
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "gm-flag-pt"}}]}

    monkeypatch.delenv("READ_AUTH_ENABLED", raising=False)
    open_path = client.post("/r6/fhir/Bundle/$ingest-context",
                            headers={"X-Tenant-Id": tenant_id}, json=bundle)
    assert open_path.status_code == 201, (
        "with READ_AUTH_ENABLED off, $ingest-context accepts an unauthorized "
        "write; this is S-3, pinned as current behavior")

    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    gated = client.post("/r6/fhir/Bundle/$ingest-context",
                        headers={"X-Tenant-Id": PRIVATE_TENANT}, json=bundle)
    assert gated.status_code == 401, (
        "with READ_AUTH_ENABLED on, $ingest-context must require a "
        "write-scoped tenant-bound token")


def test_deep_nesting_is_refused_on_every_write_path(client):
    """The #267 depth guard now covers every write path, not just one.

    MUTATION: delete the `except RecursionError` from
    r6.routes.json_body_within_depth, or drop the `_json_depth_within` call
    from it. Both halves of this test go red.

    #267 fixed an unhandled RecursionError on /internal/ingest-bundle, where
    a deeply nested body crashed the handler before any credential was
    checked. #312 recorded that the identical lever was still live on POST
    /r6/fhir/<type>, PUT /r6/fhir/<type>/<id>, /r6/fhir/Bundle/$ingest-context,
    /r6/actions/propose and /r6/smbp/enroll — one guard, applied per route,
    which is the refactor plan's thesis as a reproducible probe. Those five
    are parametrized in test_a_deeply_nested_body_is_refused_not_crashed;
    this test keeps the original ingest-bundle probe so the site #267 patched
    cannot regress while the shared helper is edited.
    """
    deep = b"[" * 60000 + b"]" * 60000

    patched = client.post("/r6/fhir/internal/ingest-bundle", data=deep,
                          content_type="application/json",
                          headers={"X-Tenant-Id": TENANT})
    assert patched.status_code == 400, (
        "the #267 depth guard on /internal/ingest-bundle is gone")

    create = client.post("/r6/fhir/Patient", data=deep,
                         content_type="application/json",
                         headers={"X-Tenant-Id": TENANT})
    assert create.status_code == 400, (
        "POST /r6/fhir/<type> answered %s to a 60,000-deep anonymous body; "
        "#312's crash lever is back" % create.status_code)


#: The five write paths #312 named, each probed at the depth where CPython's
#: JSON scanner blows the stack. `needs_token` records what it takes to REACH
#: the parser: four of the five need nothing but a tenant header, which is
#: what made this an unauthenticated crash lever rather than a nuisance.
DEEP_BODY_PATHS = (
    pytest.param("POST", "/r6/fhir/Patient", False, id="fhir-create"),
    pytest.param("PUT", "/r6/fhir/Patient/guard-matrix-pt", True,
                 id="fhir-update"),
    pytest.param("POST", "/r6/fhir/Bundle/$ingest-context", False,
                 id="ingest-context"),
    pytest.param("POST", "/r6/actions/propose", False, id="actions-propose"),
    pytest.param("POST", "/r6/smbp/enroll", False, id="smbp-enroll"),
)


@pytest.mark.parametrize("method,path,needs_token", DEEP_BODY_PATHS)
def test_a_deeply_nested_body_is_refused_not_crashed(client, method, path,
                                                     needs_token):
    """#312: a ~1500-deep body answers 4xx on every write path, never crashes.

    MUTATION: delete the `except RecursionError` from
    r6.routes.json_body_within_depth. All five rows go red at once — that one
    clause is the whole fix. Per row, the edit that reddens it alone is:
    removing the `json_body_within_depth` call from the handler
    (ingest-context, actions-propose, smbp-enroll), or from
    r6.health_compliance.enforce_human_in_loop (fhir-create, fhir-update).

    That last split is worth stating plainly rather than leaving for the next
    reader to rediscover: on the r6 blueprint the FIRST parse of a POST/PUT
    body happens in the human-in-the-loop before_request hook, ahead of every
    handler. So create's and update's own depth guards are defense in depth —
    real, but not independently observable here, because the hook answers
    first. A mutation naming only the handler would look "covered" while
    proving nothing, which is the shape this file exists to catch.

    The depth is ~1500, just past CPython's default recursion limit, because
    that is the cheapest payload that reaches the defect: `silent=True`
    suppresses decode errors but NOT RecursionError, so before the fix each
    of these raised out of the handler and 500'd the worker. A few kilobytes,
    no credential.

    `needs_token` is the honest part of this probe. Only PUT gates before it
    parses, so only PUT needs a token to reach its parser at all — without
    one it answers 401 and this test would pass for the wrong reason (the
    retro's "what else could make this green?"). The token is not much of a
    barrier either: test-tenant is public, and a public tenant mints a
    step-up token with no credential.
    """
    deep = b"[" * 1500 + b"]" * 1500
    headers = {"X-Tenant-Id": TENANT}
    if needs_token:
        headers["X-Step-Up-Token"] = token_for(TENANT)

    resp = client.open(path, method=method, data=deep,
                       content_type="application/json", headers=headers)

    assert 400 <= resp.status_code < 500, (
        f"{method} {path} answered {resp.status_code} to a 1500-deep body; "
        f"a hostile payload must be refused, not turned into a 5xx")


# ---------------------------------------------------------------------------
# 8. Guard the guard — the matrix cannot go stale silently
# ---------------------------------------------------------------------------

def _mutating_endpoints(app):
    return {
        rule.endpoint: str(rule)
        for rule in app.url_map.iter_rules()
        if (rule.methods - {"HEAD", "OPTIONS"}) & {"POST", "PUT", "PATCH",
                                                   "DELETE"}
    }


def test_every_mutating_route_is_classified(app):
    """A new POST/PUT/PATCH/DELETE route lands in the matrix or is classified.

    MUTATION: add a route with a mutating method to any blueprint, or delete
    an entry from NON_CLINICAL_MUTATORS.

    "A route that writes" is detected from the URL map's methods rather than
    from a naming convention, so it cannot be evaded by calling the handler
    something else. Classification is a judgement a human makes once, in this
    file, where a reviewer will see it.
    """
    known = {row.endpoint for row in MATRIX} | set(NON_CLINICAL_MUTATORS)
    unclassified = {
        endpoint: path
        for endpoint, path in _mutating_endpoints(app).items()
        if endpoint not in known
    }
    assert not unclassified, (
        "these mutating routes are in neither MATRIX nor "
        "NON_CLINICAL_MUTATORS:\n"
        + "\n".join(f"  {p}  ({e})" for e, p in sorted(unclassified.items()))
        + "\n\nAdd a MATRIX row with its guard set, or classify it in "
          "NON_CLINICAL_MUTATORS with the reason it carries no clinical or "
          "access-control weight.")


def test_no_matrix_row_names_a_route_that_no_longer_exists(app):
    """The matrix cannot rot in the other direction either.

    MUTATION: rename any endpoint named in MATRIX or KNOWN_GET_MUTATORS.
    """
    live = {rule.endpoint for rule in app.url_map.iter_rules()}
    stale = sorted({row.endpoint for row in MATRIX} - live)
    assert not stale, f"MATRIX names routes that no longer exist: {stale}"
    stale_get = sorted(set(KNOWN_GET_MUTATORS) - live)
    assert not stale_get, (
        f"KNOWN_GET_MUTATORS names routes that no longer exist: {stale_get}")


# --- GET routes that mutate ------------------------------------------------

_WRITE_MARKER = re.compile(
    r"db\.session\.(add|add_all|delete|commit)\(|\.delete\(synchronize_session")
# Audit is a matrix column of its own and its helpers commit, so counting
# them as "this handler writes" would flag every audited read and make the
# scanner decoration.
_NOT_A_STORE_WRITE = {"record_audit_event", "add_audit_event",
                      "_new_audit_event"}


def _source(fn):
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def _called_names(source):
    try:
        tree = ast.parse(inspect.cleandoc(source))
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _writes_to_the_store(fn, depth=2, seen=None):
    """True when fn, or something it calls inside r6/ within `depth`, writes.

    Deliberately bounded: a tripwire, not a proof. Its job is to catch a NEW
    GET handler that persists something — not to certify that everything it
    does not flag is clean. test_the_get_mutation_scanner_actually_detects_
    writes keeps it honest about that job.
    """
    seen = set() if seen is None else seen
    if fn in seen or depth < 0:
        return False
    seen.add(fn)
    source = _source(fn)
    if _WRITE_MARKER.search(source):
        return True
    if depth == 0:
        return False
    module = inspect.getmodule(fn)
    if module is None:
        return False
    for name in _called_names(source):
        if name in _NOT_A_STORE_WRITE:
            continue
        target = getattr(module, name, None)
        if not inspect.isfunction(target):
            continue
        target_module = inspect.getmodule(target)
        if target_module is None or not target_module.__name__.startswith(
                ("r6", "models")):
            continue
        if _writes_to_the_store(target, depth - 1, seen):
            return True
    return False


def _flagged_get_endpoints(app):
    flagged = {}
    for rule in app.url_map.iter_rules():
        if (rule.methods - {"HEAD", "OPTIONS"}) != {"GET"}:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is not None and _writes_to_the_store(view):
            flagged[rule.endpoint] = str(rule)
    return flagged


def test_the_get_mutation_scanner_actually_detects_writes(app):
    """The tripwire is armed: it finds the GET-mutations we already know about.

    MUTATION: break _WRITE_MARKER (drop the `db.session.add` alternative), or
    set depth=0 in _writes_to_the_store.

    Without this, test_no_new_get_route_mutates_the_store could pass because
    the scanner detects nothing whatsoever — retro defect #1, a monitor that
    counted how many checks ran rather than which.
    """
    flagged = _flagged_get_endpoints(app)
    for endpoint in ("fasten.agent_access",     # inline write, depth 0
                     "smbp.report",             # same-module helper, depth 1
                     "r6.curatr_evaluate"):     # cross-module helper, depth 2
        assert endpoint in flagged, (
            f"the scanner no longer detects the known GET-mutation "
            f"{endpoint}; it cannot be trusted to detect a new one")


def test_no_new_get_route_mutates_the_store(app):
    """A GET that persists something must be declared, not discovered later.

    MUTATION: add a `db.session.commit()` to any GET handler.

    The read/write split is the cheapest guard this system has: a reviewer
    who sees GET assumes no state change. Six routes already break it — the
    audit's S-10 names two of them — so this exists to stop a seventh
    arriving unannounced.
    """
    undeclared = {
        endpoint: path
        for endpoint, path in _flagged_get_endpoints(app).items()
        if endpoint not in KNOWN_GET_MUTATORS
    }
    assert not undeclared, (
        "these GET routes appear to mutate persistent state and are not "
        "declared in KNOWN_GET_MUTATORS:\n"
        + "\n".join(f"  {p}  ({e})" for e, p in sorted(undeclared.items())))


def test_every_matrix_row_states_a_refusal_and_a_reason():
    """The table itself is well-formed: a row cannot half-exist.

    MUTATION: add a MATRIX row without anon_refusal, or give a STEP_UP row no
    step_up_missing_status.

    A row with no pinned refusal would be silently skipped by the
    parametrized tests above while still counting toward the census — exactly
    the "looks covered, is not" shape this file exists to prevent.
    """
    for row in MATRIX:
        assert row.anon_refusal, f"{row.id}: no anonymous refusal pinned"
        assert row.guards, f"{row.id}: no guards recorded"
        if row.has(STEP_UP):
            assert row.step_up_missing_status is not None, (
                f"{row.id}: STEP_UP claimed with no pinned refusal status")
        if row.has(INTERNAL_SECRET):
            assert row.internal_secret_status is not None, (
                f"{row.id}: INTERNAL_SECRET claimed with no pinned status")
        if row.has(TENANT_FORMAT):
            assert row.has(TENANT_HEADER) or row.id == "internal-bind-telegram", (
                f"{row.id}: TENANT_FORMAT without a tenant selector")
        if row.defect_issue:
            assert row.note, f"{row.id}: defect recorded with no explanation"
    assert len({row.id for row in MATRIX}) == len(MATRIX), "duplicate row id"
