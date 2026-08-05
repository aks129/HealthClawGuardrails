"""#294 — what a CareAgents caller sees when the HealthClaw transport fails.

`careagents/healthclaw.py` is the ONLY data path out of CareAgents, so its
failure wrapping is the seam every call crosses. The repo's standing trap is
that CareAgents tests fake this client: they prove a call is MADE, never that
it is ACCEPTED, and never what comes back when the wire itself fails.

So nothing here is faked below the client. A real `requests.Session` talks to
a real socket:

  * connection refused — a closed port
  * timeout            — a server that sleeps past the client deadline
  * non-JSON body      — the HTML error page a proxy or edge serves
  * 5xx                — the engine up but failing
  * 200, wrong shape   — a JSON body that is not the object the caller expects

The property under test is one line: **a caller must be able to tell "this
failed" from "there is nothing there."** A method that answers a dead socket
with `0`, `False`, `[]` or `None` has erased that difference, and every
consumer downstream then reports emptiness to a patient as fact.

That is not a hypothetical here. Six defects in one week had this exact shape
(docs/2026-08-02-retro.md), and two of the paths below still do — they are
`xfail(strict=True)` against #294 with the live consequence spelled out, so
the day someone fixes them the pin turns red and gets updated rather than
quietly staying wrong.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import time

import pytest
import requests

from careagents.healthclaw import HealthClawClient, HealthClawError

# The discard port: nothing listens, so connect() fails immediately.
DEAD_BASE = "http://127.0.0.1:9"

CLIENT_TIMEOUT = 0.4
SLOW_ENOUGH_TO_TIME_OUT = 2.0


class _Mode:
    """What the stub engine should do to the next request."""

    def __init__(self):
        self.status = 200
        self.body = b'{"ok": 1}'
        self.ctype = "application/json"
        self.delay = 0.0


MODE = _Mode()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        if MODE.delay:
            time.sleep(MODE.delay)
        self.send_response(MODE.status)
        self.send_header("Content-Type", MODE.ctype)
        self.send_header("Content-Length", str(len(MODE.body)))
        self.end_headers()
        self.wfile.write(MODE.body)

    do_GET = do_POST = _serve

    def log_message(self, *_args):
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The timeout cases hang up mid-response on purpose, so the handler
        # thread reliably sees ConnectionResetError. Printing a traceback for
        # the thing the test asked for buries the real output.
        pass


@pytest.fixture(scope="module")
def stub_base():
    """A real HTTP server on a real port, whose behaviour each test sets."""
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def reset_mode():
    yield
    MODE.__init__()


def _client(base):
    """A client with its step-up token pre-cached.

    Without this every call would fail inside `mint_token` first, and the
    method under test would never run its own response handling — the tests
    would all pass while proving nothing about the method named in them.
    """
    c = HealthClawClient(base, "mint-secret", timeout=CLIENT_TIMEOUT)
    c._tokens["t"] = ("cached-step-up-token", time.time())
    return c


def _set(status=200, body=b'{"ok": 1}', ctype="application/json", delay=0.0):
    MODE.status, MODE.body, MODE.ctype, MODE.delay = status, body, ctype, delay


# --------------------------------------------------------------------------
# The headline: a failure that reads as an empty record set
# --------------------------------------------------------------------------
# `record_count` and `tenant_has_records` are the two calls behind the patient's
# connect-and-refresh screen. Both collapse an engine failure into a number or
# a boolean that means "nothing here", and careagents/app.py believes them.


def test_record_count_zero_must_not_be_how_an_outage_is_reported(
        stub_base, reset_mode):
    """A 503 from the engine must not read as "you have no records".

    `record_count` sums six `_summary=count` searches and swallows
    `HealthClawError` per type (careagents/healthclaw.py:311-324), so a total
    outage returns 0 — the same value as a genuinely empty tenant.

    Live consequence: careagents/app.py:526 baselines a refresh on this
    number and app.py:554 reports growth against it. During an engine
    incident the patient is told, as fact, that their record count is zero.

    MUTATION (once fixed): make the `except HealthClawError: continue` arm
    swallow again -> red.
    """
    _set(status=503, body=b'{"error": "down"}')
    with pytest.raises(HealthClawError):
        _client(stub_base).record_count("t")


def test_poll_must_not_report_pending_when_the_engine_is_failing(
        stub_base, reset_mode):
    """`tenant_has_records` returns False on a 503, and the poll endpoint
    (careagents/app.py:544) turns False into `{"status": "pending"}`.

    Live consequence: during an engine incident the connect screen says "still
    fetching your records" forever. The patient waits on a spinner for a
    condition that will never be re-evaluated, and nothing anywhere says the
    record store is down. This is the connect journey, on a phone.
    """
    _set(status=503, body=b'{"error": "down"}')
    with pytest.raises(HealthClawError):
        _client(stub_base).tenant_has_records("t")


test_record_count_zero_must_not_be_how_an_outage_is_reported = pytest.mark.xfail(
    strict=True,
    reason="#294: record_count() catches HealthClawError per resource type and "
           "returns 0, so 'the engine is down' and 'this tenant has no "
           "records' are the same answer to the caller.",
)(test_record_count_zero_must_not_be_how_an_outage_is_reported)

test_poll_must_not_report_pending_when_the_engine_is_failing = pytest.mark.xfail(
    strict=True,
    reason="#294: tenant_has_records() catches HealthClawError and returns "
           "False, which careagents/app.py:544 renders as 'pending' — an "
           "outage is indistinguishable from records not having landed yet.",
)(test_poll_must_not_report_pending_when_the_engine_is_failing)


# --------------------------------------------------------------------------
# Transport failure must be typed, on every method
# --------------------------------------------------------------------------
# `HealthClawError` is the type CareAgents catches. Anything else escaping this
# module reaches Flask as an unhandled 500, which breaks the JSON error
# contract the UI depends on — the defect #267 fixed for ingest_bundle alone.

def _call(client, name):
    return {
        "search": lambda: client.search("t", "Patient", {"_summary": "count"}),
        "read": lambda: client.read("t", "Patient", "p1"),
        "interpret_labs": lambda: client.interpret_labs("t"),
        "care_gaps": lambda: client.care_gaps("t"),
        "record_count": lambda: client.record_count("t"),
        "tenant_has_records": lambda: client.tenant_has_records("t"),
        "purge_tenant": lambda: client.purge_tenant("t"),
        "action_status": lambda: client.action_status("t", "a1"),
        "start_form_action": lambda: client.start_form_action("t"),
        "confirm_action": lambda: client.confirm_action("t", "a1"),
        "fetch_review_page": lambda: client.fetch_review_page("t", "a1"),
        "submit_review": lambda: client.submit_review("t", "a1", {}),
        "seed": lambda: client.seed("t"),
        "mint_token": lambda: client.mint_token("fresh-tenant"),
        "bind_telegram": lambda: client.bind_telegram("t", 1),
        "conformance_badge": lambda: client.conformance_badge(),
        "ingest_bundle": lambda: client.ingest_bundle(
            "t", {"resourceType": "Bundle"}),
        "create_agent_run": lambda: client.create_agent_run("t", "m1"),
        "get_agent_run": lambda: client.get_agent_run("t", "r1"),
        "agent_run_events": lambda: client.agent_run_events("t", "r1"),
        "claim_agent_run": lambda: client.claim_agent_run("w1"),
        "agent_worker_health": lambda: client.agent_worker_health(),
        "finalize_agent_run": lambda: client.finalize_agent_run(
            "r1", "w1", "text", "cp1"),
    }[name]()


# Wrapped today: ingest_bundle (fixed by #267) and the durable-run family.
WRAPS_TRANSPORT_FAILURE = [
    "ingest_bundle", "create_agent_run", "get_agent_run", "agent_run_events",
    "claim_agent_run", "agent_worker_health", "finalize_agent_run",
]

# Unwrapped: `requests` exceptions escape as themselves. #267 fixed one method
# rather than the seam, so the reads the patient's chat depends on are all here.
LEAKS_TRANSPORT_FAILURE = [
    "search", "read", "interpret_labs", "care_gaps", "record_count",
    "tenant_has_records", "purge_tenant", "action_status", "start_form_action",
    "confirm_action", "fetch_review_page", "submit_review", "seed",
    "mint_token", "bind_telegram", "conformance_badge",
]

_LEAK_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="#294: the method does not wrap requests.RequestException, so a "
           "connection refusal or timeout escapes careagents/healthclaw.py as "
           "a raw requests exception and reaches Flask as an unhandled 500.",
)


def _leaky(names):
    return [pytest.param(n, marks=[_LEAK_XFAIL]) for n in names]


@pytest.mark.parametrize(
    "method",
    WRAPS_TRANSPORT_FAILURE + _leaky(LEAKS_TRANSPORT_FAILURE))
def test_connection_refused_is_a_typed_failure(method):
    """Nothing is listening. Every method must say so in the one type
    CareAgents catches."""
    with pytest.raises(HealthClawError):
        _call(_client(DEAD_BASE), method)


@pytest.mark.parametrize(
    "method",
    WRAPS_TRANSPORT_FAILURE + _leaky(LEAKS_TRANSPORT_FAILURE))
def test_a_timeout_is_a_typed_failure(method, stub_base, reset_mode):
    """The engine accepted the connection and then stopped answering — the
    shape a saturated worker pool actually produces."""
    _set(delay=SLOW_ENOUGH_TO_TIME_OUT)
    with pytest.raises(HealthClawError):
        _call(_client(stub_base), method)


def test_a_raw_requests_exception_really_does_escape_the_unwrapped_reads():
    """Pins the CURRENT defect, so the xfails above cannot be dismissed as a
    harness artefact.

    `search` is the call behind every question the patient asks. On a refused
    connection it raises `requests.ConnectionError`, which is not
    `HealthClawError` and which no CareAgents caller catches.

    DELETE THIS TEST when #294 is fixed. It asserts the defect, so it goes red
    on the fix — which is the point: the fix cannot land while a test still
    claims the old behaviour is correct.
    """
    with pytest.raises(requests.RequestException) as exc:
        _client(DEAD_BASE).search("t", "Patient", {})
    assert not isinstance(exc.value, HealthClawError)


# --------------------------------------------------------------------------
# A 200 whose body is not JSON
# --------------------------------------------------------------------------
# What a proxy, an edge cache or an auth interstitial returns. `requests`
# raises `JSONDecodeError`, which subclasses BOTH ValueError and
# RequestException — so wrapping RequestException covers this case too.

NON_JSON_BODY = b"<html><body>502 Bad Gateway</body></html>"


_DECODE_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="#294: r.json() on the success path is unguarded, so an "
           "unparseable 200 escapes as a raw requests.JSONDecodeError. On the "
           "durable-run methods the decode sits OUTSIDE the try that wraps "
           "requests.RequestException — wrapping the call but not the decode "
           "leaves exactly half the seam covered.",
)


@pytest.mark.parametrize(
    "method",
    [pytest.param(n, marks=[_DECODE_XFAIL]) for n in
     ("search", "read", "interpret_labs", "care_gaps", "seed", "purge_tenant",
      "action_status", "mint_token", "conformance_badge", "create_agent_run",
      "get_agent_run", "agent_run_events", "claim_agent_run",
      "finalize_agent_run")])
def test_an_html_body_on_a_200_is_a_typed_failure(method, stub_base,
                                                  reset_mode):
    _set(status=200, body=NON_JSON_BODY, ctype="text/html")
    with pytest.raises(HealthClawError):
        _call(_client(stub_base), method)


@pytest.mark.xfail(
    strict=True,
    reason="#294: ingest_bundle() maps an unparseable 200 body to `body = "
           "None` and then returns `{}`, so a proxy interstitial is reported "
           "to the upload tile as a successful ingest of nothing.",
)
def test_an_unparseable_ingest_response_is_not_a_successful_empty_ingest(
        stub_base, reset_mode):
    """`{}` here means `ingested = 0`, which careagents/app.py:433 treats as
    "empty file, not an error" and reports to the patient as a completed
    upload. The records were never written."""
    _set(status=200, body=NON_JSON_BODY, ctype="text/html")
    with pytest.raises(HealthClawError):
        _client(stub_base).ingest_bundle("t", {"resourceType": "Bundle"})


def test_worker_health_already_rejects_a_body_it_cannot_parse(stub_base,
                                                              reset_mode):
    """The one method that gets this right, kept green as the reference.

    `agent_worker_health` parses, type-checks and raises. Every finding in
    this file is "apply what this method already does to the rest of the
    seam", not "invent a policy".
    """
    _set(status=200, body=NON_JSON_BODY, ctype="text/html")
    with pytest.raises(HealthClawError):
        _client(stub_base).agent_worker_health()


# --------------------------------------------------------------------------
# A 200 whose body is JSON but the wrong shape
# --------------------------------------------------------------------------
# Valid JSON, wrong type. The client asks it for `.get(...)`, or hands it
# straight back to the agent as if it were a Bundle.

WRONG_SHAPE = [
    pytest.param(b'"just-a-string"', id="json-string"),
    pytest.param(b"[1, 2, 3]", id="json-array"),
    pytest.param(b"null", id="json-null"),
]


@pytest.mark.parametrize("body", WRONG_SHAPE)
@pytest.mark.xfail(
    strict=True,
    reason="#294: search() returns r.json() unchecked, so a non-object 200 is "
           "handed to the agent as if it were a FHIR Bundle. The failure "
           "surfaces later as an AttributeError in caller code, far from the "
           "boundary that accepted it.",
)
def test_a_wrongly_shaped_search_body_is_rejected_at_the_boundary(
        body, stub_base, reset_mode):
    _set(status=200, body=body)
    with pytest.raises(HealthClawError):
        _client(stub_base).search("t", "Patient", {})


@pytest.mark.parametrize("body", WRONG_SHAPE)
@pytest.mark.xfail(
    strict=True,
    reason="#294: record_count() calls .get('total') on whatever the 200 "
           "body decoded to, so a non-object body raises AttributeError — "
           "which is not a RequestException, so even wrapping the transport "
           "would not catch it.",
)
def test_a_wrongly_shaped_count_body_is_a_typed_failure(body, stub_base,
                                                        reset_mode):
    _set(status=200, body=body)
    with pytest.raises(HealthClawError):
        _client(stub_base).record_count("t")


def test_worker_health_type_checks_its_200_body(stub_base, reset_mode):
    """Kept green: the reference implementation of the missing check."""
    _set(status=200, body=b'"just-a-string"')
    with pytest.raises(HealthClawError):
        _client(stub_base).agent_worker_health()


# --------------------------------------------------------------------------
# Failure vs emptiness, for the paths that deliberately degrade
# --------------------------------------------------------------------------
# Some methods are documented as best-effort and must not raise. That is a
# legitimate choice — but the caller still has to be able to tell the two
# states apart, and today it cannot.


def test_history_loss_is_not_the_same_value_as_an_empty_history(stub_base,
                                                                reset_mode):
    """`recent_messages` returns [] both for "this is a new conversation" and
    for "HealthClaw is unreachable", so the agent silently answers with no
    history instead of saying it lost the thread.

    Not raised as a blocking defect: it logs, and answering without history
    beats failing the turn. Pinned so the collapse is a decision on the
    record rather than an accident.
    """
    _set(status=503, body=b'{"error": "down"}')
    outage = _client(stub_base).recent_messages("t")

    _set(status=200, body=b"[]")
    empty = _client(stub_base).recent_messages("t")

    assert outage == empty == [], (
        "pinned as today's behaviour — if this changed, the collapse was "
        "fixed and this test should assert the new distinction instead")


def test_a_lost_chat_turn_is_reported_to_the_caller(stub_base, reset_mode):
    """`log_message` returning False is honest: losing a transcript must not
    break the conversation, and the caller is told."""
    _set(status=503, body=b'{"error": "down"}')
    assert _client(stub_base).log_message("t", "user", "hi") is False
    assert _client(DEAD_BASE).log_message("t", "user", "hi") is False


def test_an_unclaimable_inbound_message_is_never_reported_as_claimed(
        stub_base, reset_mode):
    """The idempotency contract: (True, id) created, (False, id) replay,
    (None, None) storage unavailable. A transport failure must land in the
    third case — reporting a replay would drop the patient's message."""
    for status, body in ((503, b'{"error": "down"}'),
                         (200, b"<html>502</html>")):
        _set(status=status, body=body, ctype="text/html")
        assert _client(stub_base).claim_inbound_message(
            "t", "hi", "a", "c", "web", "r1") == (None, None)
    assert _client(DEAD_BASE).claim_inbound_message(
        "t", "hi", "a", "c", "web", "r1") == (None, None)


def test_a_missing_brief_and_an_unreachable_engine_are_both_unavailable(
        stub_base, reset_mode):
    """`fetch_appointment_brief` is documented as None-on-any-failure and the
    callers render "brief unavailable", which is true of both states. Pinned
    because the docstring's promise — never raise to the UI — is the part
    that must not regress."""
    _set(status=404, body=b'{"error": "no brief"}')
    assert _client(stub_base).fetch_appointment_brief("t") is None

    _set(status=503, body=b'{"error": "down"}')
    assert _client(stub_base).fetch_appointment_brief("t") is None

    assert _client(DEAD_BASE).fetch_appointment_brief("t") is None

    _set(delay=SLOW_ENOUGH_TO_TIME_OUT)
    assert _client(stub_base).fetch_appointment_brief("t") is None


@pytest.mark.xfail(
    strict=True,
    reason="#294: fetch_appointment_brief() returns r.json() on any 200 "
           "without checking it decoded to an object, so a wrongly-shaped "
           "body is handed to the brief renderer as though it were the FHIR "
           "Basic resource. Its own docstring promises 'the FHIR Basic "
           "resource dict, or None'.",
)
def test_the_brief_returns_a_resource_or_none_and_never_a_bare_string(
        stub_base, reset_mode):
    """The contract is dict-or-None. Anything else moves the failure into
    whatever renders the brief, one layer away from the boundary that
    accepted it."""
    _set(status=200, body=b'"just-a-string"')
    got = _client(stub_base).fetch_appointment_brief("t")
    assert got is None or isinstance(got, dict), repr(got)


# --------------------------------------------------------------------------
# Against a REAL running HealthClaw
# --------------------------------------------------------------------------
# The stub above proves what happens when the wire breaks. It cannot prove the
# client's requests are shaped the way the engine accepts — a fake accepts wire
# formats production rejects, which is this repo's documented trap. So: the
# real engine, served over a real socket, on a real port.


@pytest.fixture
def live_engine(tmp_path, monkeypatch):
    """A real HealthClaw serving real HTTP on a real port."""
    from werkzeug.serving import make_server

    # READ_AUTH_ENABLED is what production sets; without it every tenant read
    # is allowed for local convenience, and a test that did not set it would
    # be checking the permissive path while claiming to check the gate.
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "ca-transport")
    db_path = tmp_path / "engine.db"
    from main import create_app
    from models import db

    app = create_app({"TESTING": True,
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
                      "LEGACY_BOOT_ON_CREATE": False})
    with app.app_context():
        db.create_all()

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", app
    finally:
        srv.shutdown()


def test_a_real_engine_answers_the_clients_own_search_over_real_http(
        live_engine):
    """Positive control. Without this the failure tests could all be passing
    because the client cannot talk to HealthClaw at all."""
    base, _app = live_engine
    client = HealthClawClient(base, "mint-secret", timeout=10.0)
    client._tokens["ca-transport"] = ("unused-public-tenant", time.time())

    bundle = client.search("ca-transport", "Patient", {"_summary": "count"})
    assert isinstance(bundle, dict)
    assert bundle.get("resourceType") == "Bundle"
    assert int(bundle.get("total") or 0) == 0


def test_a_real_engines_rejection_reaches_the_caller_as_a_typed_failure(
        live_engine):
    """A refusal the ENGINE decides on — not one the harness simulated.

    An unknown tenant with no credential is refused by the real read-auth
    gate, and the caller must see HealthClawError carrying the status, not a
    bare empty Bundle that would read as "this person has no records".
    """
    base, _app = live_engine
    client = HealthClawClient(base, "wrong-secret", timeout=10.0)
    client._tokens["not-a-tenant"] = ("forged", time.time())

    with pytest.raises(HealthClawError) as exc:
        client.search("not-a-tenant", "Patient", {"_summary": "count"})
    assert exc.value.status >= 400
    assert exc.value.status != 0, "a refusal is not a transport failure"


def test_the_real_engine_is_what_makes_the_dead_socket_meaningful(live_engine):
    """Same client, same call, one difference: the engine is gone.

    The contrast is the whole of #294. Against a live engine the call returns
    a Bundle; against a closed port it must raise rather than return anything
    that could be mistaken for one. Deliberately indifferent to WHICH
    exception, so this stays a positive control after the wrapping is fixed —
    the type is pinned by the xfail matrix above, not here.
    """
    base, _app = live_engine
    live = HealthClawClient(base, "mint-secret", timeout=10.0)
    live._tokens["ca-transport"] = ("unused-public-tenant", time.time())
    assert live.search(
        "ca-transport", "Patient", {"_summary": "count"})["resourceType"] == (
            "Bundle")

    dead = HealthClawClient(DEAD_BASE, "mint-secret", timeout=CLIENT_TIMEOUT)
    dead._tokens["ca-transport"] = ("unused-public-tenant", time.time())
    with pytest.raises((requests.RequestException, HealthClawError)):
        dead.search("ca-transport", "Patient", {"_summary": "count"})
