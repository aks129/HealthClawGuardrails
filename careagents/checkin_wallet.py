"""SMART Health Check-in web wallet (FHI-54) and its records relay. Behind
CARE_CHECKIN_WALLET, off by default; off, nothing here is registered.

A check-in page (the Verifier) opens /checkin/wallet in a new tab. The tab
signs the person in where it stands, then tells its opener it is ready, takes
one request from it, fetches the requested kinds of record, shows each
requested item to share or decline, and posts an HPKE-sealed answer straight
back to the opener. The protocol, the sealing and the profile matching are
the vendored @smart-health-checkin/client 0.4.4
(static/vendor/smart-health-checkin-client/0.4.4/, Apache-2.0, the release's
wallet.js byte for byte); the page and the review are ours
(static/checkin_wallet.js, static/checkin_answer.js).

The order matters. The library posts `ready` the moment it starts, and the
Verifier sends its request once it sees `ready`. A redirect to /auth would
navigate the tab away and lose the request, so this page is not behind
login_required: it signs in inline, with the existing passkey and email-code
endpoints, and starts the library only once the session is a full one.

The records come from the engine's `checkin` export (r6/checkin.py) through
one relay route, POST /checkin/wallet/records:

- The browser sends only `{"resource_types": [...]}`: the kinds of record the
  website asked for. Not the request, not the website's origin, not a
  patient or tenant id; any other key is refused. So the origin never
  reaches the engine or its audit trail.
- The server picks the tenant (the account holder's own one active,
  non-sample connection) and the client resolves the patient from it.
- A single-use token for exactly that type set is minted per call and
  spent on the engine; it never reaches the browser.
- The engine's Bundle is passed through with `Cache-Control: no-store`.
  Nothing is written to a CareAgents table, the body is never logged, and
  an error says a fixed code, never what the engine said.

The records reach the person's own tab only. What reaches the website is
only what the person chose on the review, every item starting at "Don't
share"; that review is the human gate for this patient-directed disclosure.
It is not a chart write, so it uses neither the action rail nor
X-Human-Confirmed.

No Cross-Origin-Opener-Policy is set here, and none may be: the hand-off
depends on `window.opener` surviving into this tab.

The connectathon test patient (CARE_CHECKIN_TEST_PATIENT, off by default,
and only with the wallet's own flag on). A Verifier team testing without
us signs in fresh and has nothing to share, so an account with no
real-record connection is offered one choice on this page: POST
/checkin/wallet/test-patient makes one tenant, seeded from the synthetic
US Core test patient (careagents/checkin/us_core_test_patient.json, #959),
and records it as a `sample` connection that a fixed provider marks. A
`sample` on purpose: every gate that opens a real-record feature asks
`kind != "sample"`, so all of them stay shut for it, and deleting the
account purges it as it purges every connection. The body is `{}` and
nothing else; one per account; the sample tap's lease keeps two taps from
minting two. The relay answers from it only when the account has no
real-record connection and the flag is on.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from flask import Response, jsonify, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from careagents.healthclaw import HealthClawError, acting_for

logger = logging.getLogger(__name__)

#: The kinds of record a check-in may ask for: r6/checkin.py CHECKIN_TYPES,
#: which this image does not ship (it copies only careagents/).
#: tests/test_checkin_relay.py holds the two equal.
CHECKIN_TYPES = frozenset({
    "Patient", "Condition", "AllergyIntolerance", "MedicationRequest",
    "Immunization", "Coverage",
})

#: The relay's body is one short list of type names.
RELAY_BODY_MAX = 1024

#: The synthetic US Core test patient (#959). Under careagents/ because the
#: image copies only that directory.
TEST_PATIENT_FILE = (Path(__file__).resolve().parent / "checkin"
                     / "us_core_test_patient.json")
#: What marks a `sample` connection as the test patient. Set here only.
TEST_PROVIDER = "Connectathon test patient"
TEST_LABEL = "Connectathon test patient"


@lru_cache(maxsize=1)
def _test_patient_text() -> str:
    return TEST_PATIENT_FILE.read_text(encoding="utf-8")


def test_patient_bundle() -> dict:
    """A fresh copy of the fixture Bundle, read from disk once."""
    return json.loads(_test_patient_text())


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _refuse(code: str, status: int):
    return _no_store(jsonify({"error": code})), status


def requested_types(body) -> list | None:
    """The type set, sorted, or None unless the body is exactly
    `{"resource_types": [one or more of CHECKIN_TYPES]}`."""
    if not isinstance(body, dict) or set(body) != {"resource_types"}:
        return None
    types = body["resource_types"]
    if not isinstance(types, list) or not types:
        return None
    if not all(isinstance(t, str) and t in CHECKIN_TYPES for t in types):
        return None
    return sorted(set(types))


def _is_test_patient(c) -> bool:
    return c["kind"] == "sample" and c["provider"] == TEST_PROVIDER


def records_tenant(svc, account_id: str,
                   test_patient: bool = False) -> tuple[str | None, str]:
    """(tenant, "") for the account holder's one active connection that is
    not the sample, else (None, why). Someone the account cares for is not
    the person checking in, and the sample's made-up records claim no US
    Core profile, so they would answer nothing; an account with only the
    sample is told so (`sample_only`), not that it has no records. With two
    or more there is no telling which the person means, so none is
    guessed.

    With `test_patient` (the flag), an account with no active real-record
    connection of its own answers from its connectathon test patient, if
    it chose one. A real connection always wins.

    An account with a real-record connection the relay cannot answer from
    is told which kind, first match wins: one of its own still coming
    (`records_not_ready`), its own disconnected (`records_disconnected`),
    or only someone else's (`not_your_records`). None is offered the test
    patient, since the route refuses any account with a real-record
    connection (`has_real_connection`), and the two must agree (#1027 QA
    F1)."""
    home = svc.list_home(account_id)
    me = {p["id"] for p in home["people"] if p["kind"] == "self"}
    mine = [c for c in home["connections"]
            if c["person_id"] in me and c["status"] == "active"]
    tenants = {c["tenant_id"] for c in mine if c["kind"] != "sample"}
    if not tenants:
        test = [c for c in mine if _is_test_patient(c)]
        if test_patient and test:
            return test[0]["tenant_id"], ""
        real = [c for c in home["connections"] if c["kind"] != "sample"]
        own = [c for c in real if c["person_id"] in me]
        if any(c["status"] != "revoked" for c in own):
            return None, "records_not_ready"
        if own:
            return None, "records_disconnected"
        if real:
            return None, "not_your_records"
        return None, ("sample_only" if mine else "no_records")
    if len(tenants) > 1:
        return None, "several_records"
    return next(iter(tenants)), ""


def register(app, svc, cfg, hc, full_session) -> None:
    """Add the wallet routes, or nothing at all when the flag is off.

    `full_session` is a callable: false for a session a confirm link opened,
    which is refused here as everywhere a disclosure is made (#920 F1).
    """
    if not cfg.checkin_wallet_enabled:
        return
    test_patient = cfg.checkin_test_patient_enabled

    def signed_in() -> bool:
        aid = session.get("account_id")
        return bool(aid and svc.get_account(aid) is not None
                    and full_session())

    @app.get("/checkin/wallet")
    def checkin_wallet():
        return _no_store(Response(render_template(
            "checkin_wallet.html", signed_in=signed_in(),
            test_patient=test_patient,
            terms_url=f"{cfg.healthclaw_public_base}/terms",
            privacy_url=f"{cfg.healthclaw_public_base}/privacy")))

    @app.post("/checkin/wallet/records")
    def checkin_wallet_records():
        if not signed_in():
            return _refuse("sign in", 401)
        # JSON only: a cross-site form cannot send it without a preflight,
        # which is the CSRF guard here as in feedback.py.
        if not request.is_json:
            return _refuse("json_required", 415)
        clen = request.content_length
        if clen is not None and clen > RELAY_BODY_MAX:
            return _refuse("bad_request", 400)
        request.max_content_length = RELAY_BODY_MAX
        try:
            body = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return _refuse("bad_request", 400)
        types = requested_types(body)
        if types is None:
            return _refuse("bad_request", 400)
        account_id = session["account_id"]
        if svc.is_paused(account_id):
            return _refuse("paused", 403)
        tenant, why = records_tenant(svc, account_id, test_patient)
        if tenant is None:
            return _refuse(why, 409)
        try:
            with acting_for(account_id):
                bundle = hc.checkin_export(tenant, types)
        except HealthClawError as exc:
            # The status only: the engine's words stay with the engine.
            logger.warning("checkin export failed (%s)", exc.status)
            if exc.code == "checkin_too_large":
                return _refuse("too_large", 413)
            if exc.code == "no_patient":
                return _refuse("no_patient", 409)
            return _refuse("unavailable", 502)
        return _no_store(Response(bundle, mimetype="application/fhir+json"))

    if test_patient:
        _register_test_patient(app, svc, hc, signed_in)


def _register_test_patient(app, svc, hc, signed_in) -> None:
    @app.post("/checkin/wallet/test-patient")
    def checkin_wallet_test_patient():
        if not signed_in():
            return _refuse("sign in", 401)
        # JSON only, the CSRF guard, as on the relay.
        if not request.is_json:
            return _refuse("json_required", 415)
        clen = request.content_length
        if clen is not None and clen > RELAY_BODY_MAX:
            return _refuse("bad_request", 400)
        request.max_content_length = RELAY_BODY_MAX
        try:
            body = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return _refuse("bad_request", 400)
        # Nothing the browser sends chooses anything.
        if body != {}:
            return _refuse("bad_request", 400)
        account_id = session["account_id"]
        if svc.is_paused(account_id):
            return _refuse("paused", 403)
        if svc.has_real_connection(account_id):
            return _refuse("has_records", 409)
        # The sample tap's lease: one tenant mint at a time per account.
        if not svc.claim_sample_start(account_id):
            return _refuse("busy", 409)
        try:
            # One per account: a second choice makes nothing.
            if any(_is_test_patient(c)
                   for c in svc.list_home(account_id)["connections"]):
                return _no_store(jsonify({"ready": True}))
            tenant = hc.new_tenant_id()
            try:
                with acting_for(account_id):
                    hc.seed(tenant, bundle=test_patient_bundle())
            except HealthClawError as exc:
                logger.warning("checkin test patient seed failed (%s)",
                               exc.status)
                # Whatever landed has no pointer to it; take it back.
                try:
                    hc.purge_tenant(tenant)
                except HealthClawError:
                    logger.warning("checkin test patient purge unconfirmed")
                return _refuse("unavailable", 502)
            cid = svc.add_connection(account_id, "sample", tenant,
                                     TEST_LABEL, status="active",
                                     provider=TEST_PROVIDER)
            try:
                svc.mark_synced(cid, hc.record_count(tenant))
            except HealthClawError:
                logger.warning("record count after test patient seed "
                               "failed for %s", cid)
            # It is the account's visible sample now: give it the first
            # assistant, as the sample tap does, so the hub opens it.
            svc.ensure_first_agent(account_id, cid)
            return _no_store(jsonify({"ready": True}))
        finally:
            svc.release_sample_start(account_id)
