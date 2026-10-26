"""The request log never writes a caller-supplied id header (FHI-60).

`log_request` used to write `X-Tenant-Id`, `X-Agent-Id` and `X-Request-Id`
exactly as sent. Those are caller-chosen strings, so anything a caller puts
there reached the log: a step-up token pasted into the wrong header, one of
the engine's own OAuth tokens, free text. The request is refused, but the
log line is already written.

Recognising a token by its shape cannot be made reliable (see
tests/test_sec_fhi60_opaque_token_probe.py), so the log writes a short keyed
hash of each id instead, never the value. `flask --app main log-id-hash`
recomputes one so the owner can still search the log. A request id is
written verbatim only when it is the engine's own or a UUID. Paths keep
their credential scrub and never carry a query string.

Every test captures every logger at DEBUG, not just the ``request`` logger,
so a second place that echoes a header fails here too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets

import pytest

from r6.log_safety import id_hash, scrub, warn_if_unkeyed
from r6.stepup import generate_step_up_token

_HASH = re.compile(r"t:[0-9a-f]{10}")


@pytest.fixture
def step_up_token():
    return generate_step_up_token("log-scrub-tenant")


def _everything_logged(caplog):
    """Every captured line, message and traceback, as one string."""
    out = []
    for record in caplog.records:
        out.append(record.getMessage())
        if record.exc_info:
            out.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(out)


def _access_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "request"]


def _assert_absent(text, secret):
    assert secret not in text
    # Each half of a step-up token is a usable lead on its own: the payload
    # names the tenant, the signature is the part that must stay secret.
    for half in secret.split("."):
        if len(half) >= 16:
            assert half not in text


# --- the ids: hashed, never written ---------------------------------------

def test_a_step_up_token_in_the_tenant_header_never_reaches_the_log(
        client, caplog, step_up_token):
    with caplog.at_level(logging.DEBUG):
        r = client.get("/r6/fhir/Patient",
                       headers={"X-Tenant-Id": step_up_token})

    assert r.status_code == 400
    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == id_hash(step_up_token)


def test_a_token_in_the_agent_and_request_id_headers_never_reaches_the_log(
        client, caplog, step_up_token, tenant_id):
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": tenant_id,
                            "X-Agent-Id": step_up_token,
                            "X-Request-Id": step_up_token})

    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["agent_id"] == id_hash(step_up_token)
    assert line["request_id"] == id_hash(step_up_token)


@pytest.mark.parametrize("value", [
    pytest.param(secrets.token_urlsafe(48),  # an OAuth token: a valid tenant id
                 id="random-oauth-token"),
    "sk-ant-api03-" + "Zx9Qw7Er5Ty3Ui1Op0As2Df4Gh6Jk8Lz",
    pytest.param(secrets.token_hex(16), id="random-hex"),
    "Synthetic Person 1970-01-01 see notes",
])
def test_no_value_of_any_shape_is_written_for_an_id_header(
        client, caplog, value):
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": value, "X-Agent-Id": value,
                            "X-Request-Id": value})

    assert value not in _everything_logged(caplog)
    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == id_hash(value)
    assert line["agent_id"] == id_hash(value)
    assert line["request_id"] == id_hash(value)


def test_an_ordinary_tenant_id_is_hashed_too(client, caplog, tenant_id):
    """The rule is never the raw value, not 'the raw value when it looks
    harmless': a valid id is exactly what an opaque token looks like."""
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient", headers={"X-Tenant-Id": tenant_id})

    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == id_hash(tenant_id)
    assert _HASH.fullmatch(line["tenant_id"])


def test_a_write_never_logs_its_own_step_up_token(
        client, caplog, auth_headers):
    """The token in its proper header, and pasted into the agent header."""
    token = auth_headers["X-Step-Up-Token"]
    with caplog.at_level(logging.DEBUG):
        client.post("/r6/fhir/Patient",
                    json={"resourceType": "Patient",
                          "name": [{"family": "Logscrub"}]},
                    headers={**auth_headers, "X-Agent-Id": token})

    _assert_absent(_everything_logged(caplog), token)
    (line,) = _access_lines(caplog)
    assert line["agent_id"] == id_hash(token)


def test_absent_headers_log_as_a_dash(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/metadata")

    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == "-"
    assert line["agent_id"] == "-"


# --- request ids: ours and UUIDs stay readable ----------------------------

def test_the_engines_own_request_id_is_logged_as_is(client, caplog):
    with caplog.at_level(logging.DEBUG):
        r = client.get("/r6/fhir/metadata")

    (line,) = _access_lines(caplog)
    assert re.fullmatch(r"[0-9a-f]{8}", line["request_id"])
    assert line["request_id"] == r.headers["X-Request-Id"]


def test_a_uuid_request_id_is_logged_as_is(client, caplog):
    rid = "0b6f3c1e-5a2d-4c7e-9f10-2d3e4f5a6b7c"
    with caplog.at_level(logging.DEBUG):
        r = client.get("/r6/fhir/metadata", headers={"X-Request-Id": rid})

    (line,) = _access_lines(caplog)
    assert line["request_id"] == rid
    assert r.headers["X-Request-Id"] == rid


# --- the hash: keyed, separated from the signing key, recomputable --------

def test_the_hash_is_keyed_by_the_deployment_secret(monkeypatch):
    plain = "t:" + hashlib.sha256(b"some-tenant").hexdigest()[:10]
    keyed = id_hash("some-tenant")
    assert keyed != plain
    monkeypatch.setenv("STEP_UP_SECRET", "a-different-deployment-secret")
    assert id_hash("some-tenant") not in (keyed, plain)


def test_the_hash_is_never_a_piece_of_a_real_signature(step_up_token):
    """A caller who sends a token's payload half as a header must not read
    any of that payload's signature back out of the log."""
    payload, signature = step_up_token.split(".")
    assert id_hash(payload)[2:] not in signature


def test_without_a_secret_the_hash_is_unkeyed_and_boot_says_so(
        monkeypatch, caplog):
    monkeypatch.delenv("STEP_UP_SECRET", raising=False)
    assert id_hash("some-tenant") == (
        "t:" + hashlib.sha256(b"some-tenant").hexdigest()[:10])
    with caplog.at_level(logging.WARNING):
        warn_if_unkeyed()
    assert "hashed without a key" in caplog.text


def test_create_app_warns_at_boot_without_a_secret(monkeypatch, caplog):
    import os

    from main import create_app
    monkeypatch.delenv("STEP_UP_SECRET", raising=False)
    with caplog.at_level(logging.WARNING):
        create_app({"TESTING": True,
                    "SQLALCHEMY_DATABASE_URI":
                        os.environ["SQLALCHEMY_DATABASE_URI"],
                    "LEGACY_BOOT_ON_CREATE": False})
    assert "hashed without a key" in caplog.text


def test_a_configured_secret_boots_without_the_warning(caplog):
    with caplog.at_level(logging.WARNING):
        warn_if_unkeyed()
    assert "hashed without a key" not in caplog.text


def test_the_owner_can_recompute_a_hash_from_the_cli(app):
    result = app.test_cli_runner().invoke(args=["log-id-hash", "some-tenant"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == id_hash("some-tenant")


# --- paths and request lines ----------------------------------------------

def test_a_token_in_the_path_is_cut_out_of_the_logged_path(
        client, caplog, step_up_token, tenant_id):
    with caplog.at_level(logging.DEBUG):
        client.get(f"/r6/fhir/Patient/{step_up_token}",
                   headers={"X-Tenant-Id": tenant_id})

    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["path"].startswith("/r6/fhir/Patient/")


def test_a_lone_jwt_segment_in_the_path_is_cut_out(client, caplog, tenant_id):
    segment = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    with caplog.at_level(logging.DEBUG):
        client.get(f"/r6/fhir/Patient/{segment}",
                   headers={"X-Tenant-Id": tenant_id})

    assert segment not in _everything_logged(caplog)
    (line,) = _access_lines(caplog)
    assert line["path"] == "/r6/fhir/Patient/[redacted]"


def test_the_query_string_never_reaches_the_log(client, caplog, tenant_id):
    secret = secrets.token_urlsafe(32)
    with caplog.at_level(logging.DEBUG):
        client.get(f"/r6/fhir/Patient?access_token={secret}",
                   headers={"X-Tenant-Id": tenant_id})

    assert secret not in _everything_logged(caplog)
    (line,) = _access_lines(caplog)
    assert line["path"] == "/r6/fhir/Patient"


def test_the_dev_server_access_line_drops_the_query_and_scrubs_the_path(
        app, caplog, step_up_token):
    """werkzeug prints the raw path and query string on the local server.

    Emitted the way werkzeug's own _log does, because the test client never
    goes through the dev server.
    """
    opaque = secrets.token_urlsafe(32)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("werkzeug").info(
            '%s - - [%s] "%s" %s %s', "127.0.0.1", "16/Oct/2026 22:00:00",
            f"GET /r6/fhir/Patient/{step_up_token}?access_token={opaque} "
            "HTTP/1.1", "400", "-")

    logged = _everything_logged(caplog)
    _assert_absent(logged, step_up_token)
    assert opaque not in logged
    assert '"GET /r6/fhir/Patient/[redacted] HTTP/1.1"' in logged


@pytest.mark.parametrize("word", ["Bearer", "bearer", "BEARER"])
def test_a_bearer_value_is_scrubbed_in_any_case(word):
    opaque = secrets.token_urlsafe(32)
    assert opaque not in scrub(f"Authorization: {word} {opaque}")


# ---------------------------------------------------------------------------
# CareAgents: it has no request log of its own, and must not grow one that
# echoes these headers. Its gunicorn access log format (deploy/careagents)
# names no request header, so the app itself is what is checked here.
# ---------------------------------------------------------------------------

from tests.test_careagents import (  # noqa: E402,F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)


def test_careagents_never_logs_a_token_sent_in_these_headers(
        cfg, svc, monkeypatch, caplog, step_up_token):  # noqa: F811
    from careagents.app import create_app
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    headers = {"X-Tenant-Id": step_up_token, "X-Agent-Id": step_up_token,
               "X-Request-Id": step_up_token,
               "Authorization": f"Bearer {step_up_token}"}

    with caplog.at_level(logging.DEBUG):
        c.get("/", headers=headers)
        c.get("/no-such-page", headers=headers)
        _login(c, svc, monkeypatch)
        c.get("/home", headers=headers)
        c.post("/api/chat", json={"message": "hi"}, headers=headers)

    _assert_absent(_everything_logged(caplog), step_up_token)


def test_an_agent_id_the_engine_would_reject_is_stored_hashed(
        client, step_up_token, tenant_id):
    """The audit row's agent column is String(128), and Postgres enforces it:
    a step-up token pasted into X-Agent-Id failed the audit write (and so the
    request) as a DataError. An id outside the engine's id rule is stored as
    its log hash, which also keeps the pasted credential out of the table."""
    from r6.audit import record_audit_event
    from r6.models import AuditEventRecord

    assert len(step_up_token) > 128
    for raw in (step_up_token, "has spaces in it"):
        record_audit_event("read", "Patient", agent_id=raw,
                           tenant_id=tenant_id)
        AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, agent_id=id_hash(raw)).one()
        assert not AuditEventRecord.query.filter_by(agent_id=raw).count()


def test_a_well_formed_agent_id_is_stored_as_sent(client, tenant_id):
    from r6.audit import record_audit_event
    from r6.models import AuditEventRecord

    agent = "careagents:agent_" + "a" * 100
    record_audit_event("read", "Patient", agent_id=agent, tenant_id=tenant_id)
    row = AuditEventRecord.query.filter_by(
        tenant_id=tenant_id, agent_id=agent).one()
    assert row.agent_id == agent
