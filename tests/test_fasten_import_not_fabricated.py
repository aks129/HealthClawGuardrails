"""
#305 + #310a: nothing claims a Fasten import that has not happened.

`POST /fasten/demo` is deleted. It was the unauthenticated write path of #305
(three audit rows per anonymous request, unlimited) AND the engine of the
#310a fabrication: `onStitchComplete` ran a REAL Stitch connection, then drove
"steps 2-5 (webhook -> ingest -> redact -> audit)" off a hardcoded
`fasten-demo-tenant` and toasted "Health data imported" with the patient's real
org id. The real ingest lands later, via the async Fasten webhook.

Deleting the route and both of its callers closes both: no route, no residual
auth question, and no synchronous claim to fabricate.

SCOPE OF PROOF. The two source-reading tests below assert on the TEXT of
`static/js/r6-dashboard.js`. This repo has no browser harness, so they prove
the fabricating code is absent from the file — not that a rendered page
behaves. A DOM-level assertion would need a test browser we do not have.

MUTATION (route): restore the `@fasten_blueprint.route('/demo', ...)` handler
in r6/fasten/routes.py -> test_the_demo_route_is_gone and
test_no_endpoint_serves_the_demo_route redden. Verified 2026-08-04.

MUTATION (claim): put `toast('Health data imported ...')` back in
`onStitchComplete`, or re-add a `fetch('/fasten/demo')` ->
test_stitch_completion_claims_no_completed_import and
test_the_dashboard_never_calls_the_demo_route redden. Verified 2026-08-04.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "static/js/r6-dashboard.js"
CONNECT_HTML = ROOT / "templates/fasten_connect.html"

# The wording the honest patient-facing page already uses. The dashboard must
# reuse it verbatim rather than grow a second, drifting version.
PENDING_COPY = (
    "Connection registered. Records will stream in over the next 5–45 "
    "minutes. Check Telegram for a ping when they land."
)


def _on_stitch_complete_source() -> str:
    """The body of onStitchComplete, from its `async function` to its close."""
    text = DASHBOARD_JS.read_text()
    start = text.index("async function onStitchComplete(")
    end = text.index("\n}\n", start)
    return text[start:end]


# --- #305: the route is gone ------------------------------------------------

def test_the_demo_route_is_gone(client):
    """The unauthenticated write path answers 404, not 200."""
    resp = client.post("/fasten/demo")
    assert resp.status_code == 404, (
        f"POST /fasten/demo answered {resp.status_code}; the route was "
        f"deleted in #305 and must stay deleted.")


def test_no_endpoint_serves_the_demo_route(app):
    """Belt and braces: nothing in the URL map serves it under any method."""
    rules = [str(rule) for rule in app.url_map.iter_rules()]
    assert "/fasten/demo" not in rules, "a rule still serves /fasten/demo"
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "fasten.run_demo" not in endpoints


# --- #310a: the page claims no import it cannot see -------------------------

def test_the_dashboard_never_calls_the_demo_route():
    """Neither caller survives — the button's and onStitchComplete's."""
    assert "/fasten/demo" not in DASHBOARD_JS.read_text(), (
        "static/js/r6-dashboard.js still references /fasten/demo")


def test_stitch_completion_claims_no_completed_import():
    """A real Stitch completion asserts nothing about the import.

    The import is asynchronous: when this handler runs, the webhook has not
    fired, nothing has been ingested, redacted or audited. Any past-tense
    claim here is fabricated by construction.
    """
    source = _on_stitch_complete_source()
    for claim in ("imported", "Imported", "ingested", "Ingested"):
        assert claim not in source, (
            f"onStitchComplete claims {claim!r} before the async import has "
            f"run")


def test_the_pending_wording_matches_the_connect_page():
    """One sentence, two surfaces — not two sentences that drift apart."""
    assert PENDING_COPY in CONNECT_HTML.read_text(), (
        "templates/fasten_connect.html no longer carries the wording this "
        "test pins the dashboard to; update both together")
    assert PENDING_COPY in _on_stitch_complete_source(), (
        "onStitchComplete does not use the connect page's honest wording")
