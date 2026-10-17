"""SMART Health Check-in web wallet, skeleton (FHI-54). Behind
CARE_CHECKIN_WALLET, off by default; off, nothing here is registered.

A check-in page (the Verifier) opens /checkin/wallet in a new tab. The tab
signs the person in where it stands, then tells its opener it is ready, takes
one request from it, shows each requested item to share or decline, and posts
an HPKE-sealed answer straight back to the opener. The protocol, the sealing
and the profile matching are the vendored @smart-health-checkin/client 0.4.4
(static/vendor/smart-health-checkin-client/0.4.4/, Apache-2.0, the release's
wallet.js byte for byte); the page and the review are ours
(static/checkin_wallet.js, static/checkin_answer.js).

The order matters. The library posts `ready` the moment it starts, and the
Verifier sends its request once it sees `ready`. A redirect to /auth would
navigate the tab away and lose the request, so this page is not behind
login_required: it signs in inline, with the existing passkey and email-code
endpoints, and starts the library only once the session is a full one.

CareAgents stores no PHI, and nothing about a request or its answer reaches
this server. Both routes are GETs. The request arrives by postMessage, the
answer is assembled and sealed in the browser and leaves by postMessage, and
neither is fetched, posted, stored or logged. For this skeleton the answer
comes from the synthetic US Core test patient (FHI-51,
careagents/checkin/us_core_test_patient.json), never from an account's
records; a real export with step-up, redaction and audit is later work.

No Cross-Origin-Opener-Policy is set here, and none may be: the hand-off
depends on `window.opener` surviving into this tab.
"""

from __future__ import annotations

from pathlib import Path

from flask import Response, jsonify, render_template, session

#: The synthetic record set the skeleton answers from.
TEST_PATIENT = Path(__file__).with_name("checkin") / "us_core_test_patient.json"


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def register(app, svc, cfg, full_session) -> None:
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

    @app.get("/checkin/wallet/test-patient.json")
    def checkin_wallet_test_patient():
        # Synthetic, but asked for the way a real export will be: signed in.
        if not signed_in():
            return _no_store(jsonify({"error": "sign in"})), 401
        return _no_store(Response(TEST_PATIENT.read_bytes(),
                                  mimetype="application/fhir+json"))
