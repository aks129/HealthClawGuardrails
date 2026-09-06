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

SCOPE OF PROOF. The source-reading tests below assert on the TEXT of the
front-end surfaces. This repo has no browser harness, so they prove the
fabricating code is absent from the files — not that a rendered page behaves.
A DOM-level assertion would need a test browser we do not have.

WHERE THEY LOOK, AND WHY IT CHANGED. They used to read
`static/js/r6-dashboard.js`, which held the dashboard's copy of the Stitch
completion handler. That file is deleted: /r6-dashboard is a server-rendered
conformance report now and loads no script, so the file powered nothing. The
guard did not narrow to follow it — it widened. It now scans every template
and every shipped script, because the defect was never "this file lies", it
was "a surface claims an import the webhook has not delivered yet", and the
next surface to do that will be a file this list could not have named.

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
CONNECT_HTML = ROOT / "templates/fasten_connect.html"


def _front_end_sources():
    """Every template and shipped script, as (path, text) pairs.

    Vendored assets are skipped: they are third-party, we do not edit them,
    and a false positive there would train someone to widen the allowlist.
    """
    files = [
        *(ROOT / "templates").rglob("*.html"),
        *(ROOT / "static" / "js").rglob("*.js"),
    ]
    return [(f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8"))
            for f in files if "vendor" not in f.parts]

# The wording the honest patient-facing page already uses. The dashboard must
# reuse it verbatim rather than grow a second, drifting version.
#
# It used to end "Check Telegram for a ping when they land". The messaging
# surface has not been served since June (council ruling D6), so a patient who
# had just connected their records was told to wait for a notification that
# could not arrive, and would conclude the connection had failed (#564). What
# replaced it says the one thing that stops the wait.
PENDING_COPY = (
    "Connection registered. Records will stream in over the next 5–45 "
    "minutes. Nothing will notify you when they land."
)


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

def test_no_front_end_surface_calls_the_demo_route():
    """No caller survives, on any page — not just the one that had them."""
    offenders = [name for name, text in _front_end_sources()
                 if "/fasten/demo" in text]
    assert offenders == [], f"{offenders} still reference /fasten/demo"


def test_no_front_end_surface_claims_a_completed_import():
    """The import is asynchronous. When a Stitch connection completes, the
    webhook has not fired: nothing has been ingested, redacted or audited. A
    past-tense claim at that moment is fabricated by construction.

    Scoped to the sentence around the word rather than the whole file, because
    "imported" is an ordinary word — an `import` statement or a comment about
    imported records is not the defect. What is banned is telling the patient
    their data arrived.
    """
    # Past-tense claims of delivery only. "import complete" was on this list
    # for one commit and matched wiki.html's "Once an import completes, the
    # skills expose all ingested data" — conditional developer documentation
    # about a later moment, which is true. A guard that reddens on true prose
    # gets widened by whoever hits it next, and the exemption is what fails
    # silently afterwards (docs/defect-catalogue.md §4).
    banned = ("data imported", "Data imported", "records imported",
              "Records imported", "health data imported",
              "Health data imported")
    offenders = [(name, phrase)
                 for name, text in _front_end_sources()
                 for phrase in banned if phrase in text]
    assert offenders == [], (
        f"{offenders} claim a completed import; the webhook lands minutes "
        "later, so nothing on-screen can know this yet")


def test_the_connect_page_uses_the_pending_wording():
    """The honest sentence stays on the surface that shows it.

    This used to pin two surfaces to one sentence so they could not drift.
    There is one surface now — the dashboard's copy went with its script — so
    the test pins what remains rather than pretending to compare two things.
    """
    assert PENDING_COPY in CONNECT_HTML.read_text(), (
        "templates/fasten_connect.html no longer carries the honest "
        "'records will stream in' wording")


# --- #564: the page sends nobody to a surface that is not served ------------

def test_the_connect_page_names_no_messaging_channel():
    """A patient who has just connected is sent to nothing that is off.

    This page told them to watch for a Telegram ping and listed the slash
    commands to send when it arrived. The ping cannot arrive: the surface is
    not served (council ruling D6, 2026-09-02). Someone who follows the
    instruction waits, then concludes the connection failed — the same defect
    as the fabricated import above, inverted: the page asserts an outcome the
    system cannot produce, on the surface a first-time tester meets first.

    SCOPE OF PROOF. The file, not the rendered page — as with every source
    check in this module. Scoped to this one template on purpose:
    privacy.html, index.html, wiki.html, security.html and faq.html describe
    the component rather than promising a delivery, and a repo-wide ban would
    redden on prose that is not the defect.

    When the surface is served again, delete this test. That is a deliberate
    act, which is the point of pinning it.

    MUTATION: put "Check Telegram for a ping when they land." back into the
    registration status string -> this and
    test_the_connect_page_uses_the_pending_wording both redden.
    Verified 2026-09-04.
    """
    assert "Telegram" not in CONNECT_HTML.read_text(encoding="utf-8"), (
        "templates/fasten_connect.html points a patient at a messaging "
        "channel; that surface is not served, so the instruction cannot be "
        "followed")
