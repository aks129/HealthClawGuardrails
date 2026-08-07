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
(docs/2026-08-02-retro.md), and when this file was written most of the paths
below still did. They were pinned `xfail(strict=True)` against #294 with the
live consequence spelled out, so the day someone fixed them the pins turned
red and had to be updated rather than quietly staying wrong. #403 was that
fix: `careagents/healthclaw.py` now sends every request through `_send` and
decodes every success body through `_json_object`, so what follows asserts
the behaviour rather than pinning the defect.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import time

import pytest
import requests

from careagents.healthclaw import (HealthClawClient, HealthClawError,
                                   HealthClawUnconfirmed)

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
        # `confirm_action` mints an approval token before it confirms. With
        # this set the mint is answered normally whatever the rest of the mode
        # says, so the mode describes the confirm POST itself — otherwise
        # every confirm case below would fail in the mint and never reach the
        # call it names.
        self.mint_ok = False


MODE = _Mode()

MINTED = b'{"token": "approval-token"}'


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        if MODE.mint_ok and self.path.endswith("/approval-token"):
            return self._write(200, MINTED, "application/json")
        if MODE.delay:
            time.sleep(MODE.delay)
        self._write(MODE.status, MODE.body, MODE.ctype)

    def _write(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def _set(status=200, body=b'{"ok": 1}', ctype="application/json", delay=0.0,
         mint_ok=False):
    MODE.status, MODE.body, MODE.ctype, MODE.delay = status, body, ctype, delay
    MODE.mint_ok = mint_ok


# --------------------------------------------------------------------------
# The headline: a failure that reads as an empty record set
# --------------------------------------------------------------------------
# `record_count` and `tenant_has_records` are the two calls behind the patient's
# connect-and-refresh screen. Both collapse an engine failure into a number or
# a boolean that means "nothing here", and careagents/app.py believes them.


def test_record_count_zero_must_not_be_how_an_outage_is_reported(
        stub_base, reset_mode):
    """A 503 from the engine must not read as "you have no records".

    `record_count` sums six `_summary=count` searches. It used to swallow
    `HealthClawError` per type and carry on, so a total outage returned 0 —
    the same value as a genuinely empty tenant.

    Live consequence it had: careagents/app.py baselines a refresh on this
    number and the poll endpoint reports growth against it. During an engine
    incident the patient was told, as fact, that their record count was zero.

    MUTATION: restore `except HealthClawError: continue` around the search in
    `record_count` -> red (returns 0 instead of raising). Ran it, saw red.
    """
    _set(status=503, body=b'{"error": "down"}')
    with pytest.raises(HealthClawError):
        _client(stub_base).record_count("t")


def test_poll_must_not_report_pending_when_the_engine_is_failing(
        stub_base, reset_mode):
    """`tenant_has_records` used to return False on a 503, and the poll
    endpoint turns False into `{"status": "pending"}`.

    Live consequence it had: during an engine incident the connect screen
    said "still fetching your records" forever. The patient waited on a
    spinner for a condition that would never be re-evaluated, and nothing
    anywhere said the record store was down. This is the connect journey, on
    a phone.

    MUTATION: restore `except HealthClawError: return False` in
    `tenant_has_records` -> red. Ran it, saw red. The paired assertion that
    the endpoint above it now says so out loud is
    `test_the_poll_says_the_engine_is_unreachable_rather_than_pending` in
    tests/test_careagents.py.
    """
    _set(status=503, body=b'{"error": "down"}')
    with pytest.raises(HealthClawError):
        _client(stub_base).tenant_has_records("t")


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


# Every method on the seam. This was two lists: `ingest_bundle` and the
# durable-run family, wrapped by #267, and the sixteen that still let a
# `requests` exception escape as itself. #403 routed all of them through
# `_send`, which is what collapses the two lists into one.
EVERY_SEAM_METHOD = [
    "ingest_bundle", "create_agent_run", "get_agent_run", "agent_run_events",
    "claim_agent_run", "agent_worker_health", "finalize_agent_run",
    "search", "read", "interpret_labs", "care_gaps", "record_count",
    "tenant_has_records", "purge_tenant", "action_status", "start_form_action",
    "confirm_action", "fetch_review_page", "submit_review", "seed",
    "mint_token", "bind_telegram", "conformance_badge",
]


@pytest.mark.parametrize("method", EVERY_SEAM_METHOD)
def test_connection_refused_is_a_typed_failure(method):
    """Nothing is listening. Every method must say so in the one type
    CareAgents catches.

    MUTATION: drop the `except requests.RequestException` arm from `_send`
    -> red for all 23, with `requests.exceptions.ConnectionError` raised
    instead of `HealthClawError` — the raw type no CareAgents caller catches
    and the one that reaches Flask as an unhandled 500. Ran it, saw red.
    """
    with pytest.raises(HealthClawError):
        _call(_client(DEAD_BASE), method)


@pytest.mark.parametrize("method", EVERY_SEAM_METHOD)
def test_a_timeout_is_a_typed_failure(method, stub_base, reset_mode):
    """The engine accepted the connection and then stopped answering — the
    shape a saturated worker pool actually produces."""
    _set(delay=SLOW_ENOUGH_TO_TIME_OUT)
    with pytest.raises(HealthClawError):
        _call(_client(stub_base), method)


# --------------------------------------------------------------------------
# A 200 whose body is not JSON
# --------------------------------------------------------------------------
# What a proxy, an edge cache or an auth interstitial returns. `requests`
# raises `JSONDecodeError`, which subclasses BOTH ValueError and
# RequestException — so wrapping RequestException covers this case too.

NON_JSON_BODY = b"<html><body>502 Bad Gateway</body></html>"


@pytest.mark.parametrize(
    "method",
    ("search", "read", "interpret_labs", "care_gaps", "seed", "purge_tenant",
     "action_status", "mint_token", "conformance_badge", "create_agent_run",
     "get_agent_run", "agent_run_events", "claim_agent_run",
     "finalize_agent_run"))
def test_an_html_body_on_a_200_is_a_typed_failure(method, stub_base,
                                                  reset_mode):
    """`r.json()` on the success path used to be unguarded, so an unparseable
    200 escaped as a raw `requests.JSONDecodeError`. On the durable-run
    methods the decode sat OUTSIDE the try that wrapped
    `requests.RequestException` — wrapping the call but not the decode left
    exactly half the seam covered.

    MUTATION: return `r.json()` directly instead of `self._json_object(r, …)`
    in any of these -> red for that method. Ran it, saw red.
    """
    _set(status=200, body=NON_JSON_BODY, ctype="text/html")
    with pytest.raises(HealthClawError):
        _call(_client(stub_base), method)


def test_an_unparseable_ingest_response_is_not_a_successful_empty_ingest(
        stub_base, reset_mode):
    """`ingest_bundle` used to map an unparseable 200 to `body = None` and
    return `{}`. `{}` means `ingested = 0`, which careagents/app.py treats as
    "empty file, not an error" and reports to the patient as a completed
    upload. The records were never written.

    MUTATION: return `body or {}` on the 200 path again -> red. Ran it, saw
    red.
    """
    _set(status=200, body=NON_JSON_BODY, ctype="text/html")
    with pytest.raises(HealthClawError):
        _client(stub_base).ingest_bundle("t", {"resourceType": "Bundle"})


def test_worker_health_already_rejects_a_body_it_cannot_parse(stub_base,
                                                              reset_mode):
    """The method that got this right first, kept green as the reference.

    `agent_worker_health` parsed, type-checked and raised while the rest of
    the seam did not. Every finding in this file was "apply what this method
    already does to the rest of the seam", not "invent a policy" — and #403
    did exactly that by extracting its two halves into `_send` and
    `_json_object`.
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
def test_a_wrongly_shaped_search_body_is_rejected_at_the_boundary(
        body, stub_base, reset_mode):
    """`search` used to return `r.json()` unchecked, so a non-object 200 was
    handed to the agent as if it were a FHIR Bundle. The failure surfaced
    later as an AttributeError in caller code, far from the boundary that
    accepted it.

    MUTATION: drop the `isinstance(body, dict)` arm from `_json_object` ->
    red for all three bodies. Ran it, saw red.
    """
    _set(status=200, body=body)
    with pytest.raises(HealthClawError):
        _client(stub_base).search("t", "Patient", {})


@pytest.mark.parametrize("body", WRONG_SHAPE)
def test_a_wrongly_shaped_count_body_is_a_typed_failure(body, stub_base,
                                                        reset_mode):
    """`record_count` calls `.get('total')` on whatever the 200 body decoded
    to, so a non-object body raised AttributeError — which is not a
    RequestException, so wrapping the transport alone would not have caught
    it. The type check inside the boundary is what does."""
    _set(status=200, body=body)
    with pytest.raises(HealthClawError):
        _client(stub_base).record_count("t")


def test_worker_health_type_checks_its_200_body(stub_base, reset_mode):
    """Kept green: where the check the rest of the seam now shares came
    from."""
    _set(status=200, body=b'"just-a-string"')
    with pytest.raises(HealthClawError):
        _client(stub_base).agent_worker_health()


# --------------------------------------------------------------------------
# A confirm that was never answered — with a status code on it
# --------------------------------------------------------------------------
# `confirm_action` is the one call on this seam that can EXECUTE a clinical
# action, so what it raises decides what a patient is told after they approve.
# #220 typed the case where no response arrives at all. #416 is the case a
# gateway answers instead: an edge 502/503/504 IS a response, so `_send`
# returns it and `if not r.ok` filed it as a refusal — "Nothing has been sent,
# please try approving again", for an action the engine had already run. QA
# drove that against a live engine and watched the action move
# awaiting_confirmation -> failed while the patient was told to approve again.
#
# So the question is never "was it 2xx". It is what the status says about the
# UPSTREAM: only an engine that answered and declined is a refusal.

GATEWAY_BODY = b"<html><body>gateway</body></html>"


def test_a_confirmed_action_returns_the_engines_answer(stub_base, reset_mode):
    """Positive control. Without it every case below could be passing because
    the confirm never reaches the stub at all."""
    _set(status=200, body=b'{"status": "confirmed"}', mint_ok=True)
    assert _client(stub_base).confirm_action("t", "a1") == {
        "status": "confirmed"}


@pytest.mark.parametrize("status", (408, 429, 500, 502, 503, 504))
def test_a_confirm_a_gateway_answered_is_not_a_refusal(status, stub_base,
                                                       reset_mode):
    """A 504 is the production shape of "we do not know", not of "no".

    The edge answers on the upstream's behalf after the request was already
    delivered, so the confirm may have executed. Reporting it as a refusal
    tells the patient nothing was sent and invites a second approval, which on
    the human-approval path is an instruction to double-execute.

    MUTATION: raise `HealthClawError` for 5xx in `confirm_action` -> red here
    for 500/502/503/504. Ran it with PYTHONDONTWRITEBYTECODE=1, saw red.
    """
    _set(status=status, body=GATEWAY_BODY, ctype="text/html", mint_ok=True)
    with pytest.raises(HealthClawUnconfirmed):
        _client(stub_base).confirm_action("t", "a1")


@pytest.mark.parametrize("status", (400, 401, 403, 404, 409, 410, 422))
def test_a_confirm_the_engine_declined_stays_a_refusal(status, stub_base,
                                                       reset_mode):
    """The other half, and the half that made the old bug invisible: a 4xx is
    an answer. Either the engine declined or an edge rejected the request
    before delivering it — nothing ran either way, so "nothing has been sent,
    try approving again" is true and must keep being said.
    """
    _set(status=status, body=b'{"error": "not awaiting confirmation"}',
         mint_ok=True)
    err = pytest.raises(HealthClawError,
                        _client(stub_base).confirm_action, "t", "a1")
    assert not isinstance(err.value, HealthClawUnconfirmed)


def test_a_confirm_answered_200_but_unreadable_is_not_a_refusal(stub_base,
                                                                reset_mode):
    """The narrower sibling. A 200 carrying a proxy's HTML interstitial is not
    a decline either, and `_json_object` raising plain `HealthClawError`
    produced the same "Nothing has been sent" message.

    MUTATION: decode with a bare `self._json_object(r, "confirm")` -> red.
    Ran it with PYTHONDONTWRITEBYTECODE=1, saw red.
    """
    _set(status=200, body=NON_JSON_BODY, ctype="text/html", mint_ok=True)
    with pytest.raises(HealthClawUnconfirmed):
        _client(stub_base).confirm_action("t", "a1")


def test_a_confirm_answered_by_nobody_stays_unconfirmed(stub_base,
                                                        reset_mode):
    """#220's own case, kept green here rather than only in the route test.

    The POST is on the wire and the read times out. This is the path
    `_send(error=HealthClawUnconfirmed)` already covers; regressing it
    reintroduces the defect the rest of this section generalizes.
    """
    _set(delay=SLOW_ENOUGH_TO_TIME_OUT, mint_ok=True)
    with pytest.raises(HealthClawUnconfirmed):
        _client(stub_base).confirm_action("t", "a1")


def test_a_lost_approval_mint_is_a_refusal_and_not_silence(stub_base,
                                                           reset_mode):
    """The mint runs BEFORE the confirm and has no side effect on the action
    (`issue_action_approval_token` reads it and signs a token), so losing its
    answer means the confirm never went out. Nothing executed, "try approving
    again" is the correct instruction, and the classification must not flatten
    that in the other direction to make the 504 case pass.
    """
    _set(status=504, body=GATEWAY_BODY, ctype="text/html")
    err = pytest.raises(HealthClawError,
                        _client(stub_base).confirm_action, "t", "a1")
    assert not isinstance(err.value, HealthClawUnconfirmed)


# --------------------------------------------------------------------------
# Failure vs emptiness, for the paths that deliberately degrade
# --------------------------------------------------------------------------
# Some methods are documented as best-effort and must not raise. That is a
# legitimate choice — but the caller still has to be able to tell the two
# states apart, and today it cannot.


def test_history_loss_is_not_the_same_value_as_an_empty_history(stub_base,
                                                                reset_mode):
    """PIN FLIPPED (E3). This test used to assert the collapse and invited
    exactly this change: "if this changed, the collapse was fixed and this
    test should assert the new distinction instead".

    [] meant both "this is a new conversation" and "HealthClaw is
    unreachable". The earlier note reasoned that answering without history
    beats failing the turn — true for the web tier, and wrong for the worker,
    which builds the agent's context from this and so answered with amnesia
    while saying nothing about it. The web tier had its own version: every
    return visit during an outage rendered as a first visit.

    MUTATION: return [] for the 503 again -> red. Ran it, saw red.
    """
    _set(status=200, body=b"[]")
    assert _client(stub_base).recent_messages("t") == [], (
        "an engine that answered with no rows still means no rows")

    # 401/403 included deliberately (QA F1). A rejected or rotated step-up
    # token is the engine answering about our CREDENTIAL, not about this
    # patient's conversation — we learn nothing about whether history exists,
    # so [] would be the same collapse this test was flipped to forbid. The
    # conversations endpoint answers 401 when the token is stale, which is a
    # realistic drift between web and worker.
    for status in (500, 502, 503, 504, 408, 429, 401, 403):
        _set(status=status, body=b'{"error": "down"}')
        with pytest.raises(HealthClawError):
            _client(stub_base).recent_messages("t")

    with pytest.raises(HealthClawError):
        _client(DEAD_BASE).recent_messages("t")


def test_a_malformed_history_body_does_not_escape_the_boundary(stub_base,
                                                              reset_mode):
    """QA F7. #430 hardened the brief against this and left its sibling open.

    `recent_messages` decoded a 200 and then indexed it, so a body that is
    valid JSON of the wrong type raised AttributeError or TypeError out of the
    client — past the one boundary whose whole job is to turn a bad call into
    a HealthClawError. `careagents/app.py` catches HealthClawError and nothing
    else, so each of these was an unhandled 500 on /chat.

    MUTATION: drop the isinstance/row guards -> red with AttributeError.
    Ran it, saw red.
    """
    for body in (b'{"error": "nope"}', b'42', b'"just-a-string"',
                 b'[1, 2, 3]', b'[{"role": 7}]'):
        _set(status=200, body=body)
        try:
            got = _client(stub_base).recent_messages("t")
        except HealthClawError:
            continue
        assert isinstance(got, list), f"{body!r} produced {got!r}"
        for row in got:
            assert isinstance(row, dict) and "role" in row, repr(row)


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
    """PIN FLIPPED (E3). They are not both "unavailable" to the reader.

    The template renders None as "Not available from your connected records"
    — a statement about the patient's records, made during an outage that
    read none of them. The same page already gets this right one section
    down, where the screening review requires an explicit "ok" (#381).

    404 is the engine answering that there is no brief, and still returns
    None. Everything else raises, and the route renders "we could not reach
    your records" instead.

    MUTATION: return None for the 503 again -> red. Ran it, saw red.
    """
    _set(status=404, body=b'{"error": "no brief"}')
    assert _client(stub_base).fetch_appointment_brief("t") is None, (
        "the engine answered: there is no brief")

    for status in (500, 502, 503, 504, 401, 403):
        _set(status=status, body=b'{"error": "down"}')
        with pytest.raises(HealthClawError):
            _client(stub_base).fetch_appointment_brief("t")

    with pytest.raises(HealthClawError):
        _client(DEAD_BASE).fetch_appointment_brief("t")

    _set(delay=SLOW_ENOUGH_TO_TIME_OUT)
    with pytest.raises(HealthClawError):
        _client(stub_base).fetch_appointment_brief("t")


def test_the_brief_returns_a_resource_or_none_and_never_a_bare_string(
        stub_base, reset_mode):
    """The contract is dict-or-None. Anything else moves the failure into
    whatever renders the brief, one layer away from the boundary that
    accepted it.

    `fetch_appointment_brief` used to return `r.json()` on any 200 without
    checking it decoded to an object, against its own docstring's promise of
    "the FHIR Basic resource dict, or None". Note what does NOT fix this: a
    wider `except`. The method never raised — it returned the wrong shape,
    which is why the type check and not the wrapping is what turns this
    green.

    PIN NARROWED (E3): the contract is now dict, or None when the engine said
    there is no brief, or a raise. A malformed 200 raises rather than
    returning None, because it means we did not learn whether a brief exists
    — the same fact as an unreachable engine, and the caller now has somewhere
    to put that. What the test is for is unchanged: a wrong shape must never
    reach the renderer.

    MUTATION: return `r.json()` here instead of `self._json_object(...)` ->
    red, with `'just-a-string'` returned. Ran it, saw red.
    """
    _set(status=200, body=b'"just-a-string"')
    try:
        got = _client(stub_base).fetch_appointment_brief("t")
    except HealthClawError:
        got = None
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
    exception, so it stayed a positive control across the #403 fix — the type
    is pinned by `test_connection_refused_is_a_typed_failure` above, not here.
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
