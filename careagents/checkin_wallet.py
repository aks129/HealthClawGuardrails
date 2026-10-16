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
"""

from __future__ import annotations

import logging

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


def records_tenant(svc, account_id: str) -> tuple[str | None, str]:
    """(tenant, "") for the account holder's one active connection that is
    not the sample, else (None, why). Someone the account cares for is not
    the person checking in, and the sample's made-up records claim no US
    Core profile, so they would answer nothing. With two or more there is
    no telling which the person means, so none is guessed."""
    home = svc.list_home(account_id)
    me = {p["id"] for p in home["people"] if p["kind"] == "self"}
    tenants = {c["tenant_id"] for c in home["connections"]
               if c["person_id"] in me and c["kind"] != "sample"
               and c["status"] == "active"}
    if not tenants:
        return None, "no_records"
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

    def signed_in() -> bool:
        aid = session.get("account_id")
        return bool(aid and svc.get_account(aid) is not None
                    and full_session())

    @app.get("/checkin/wallet")
    def checkin_wallet():
        return _no_store(Response(render_template(
            "checkin_wallet.html", signed_in=signed_in(),
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
        tenant, why = records_tenant(svc, account_id)
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
