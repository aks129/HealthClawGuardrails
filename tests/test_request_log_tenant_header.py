"""The request log never echoes a credential a caller put in a header (FHI-60).

`log_request` used to write `X-Tenant-Id`, `X-Agent-Id` and `X-Request-Id`
exactly as sent. Those are caller-chosen strings, so anything a caller puts
there reaches the log: a step-up token pasted into the wrong header, an API
key, free text. The request is refused, but the log line is already written.

The access log now writes a tenant id only when it is a valid tenant id, the
other two ids only when they have an id's shape, and the word ``invalid``
otherwise. Values that are valid ids but look like a credential are refused
too, and credential-shaped runs are cut out of the logged path.

Every test captures every logger at DEBUG, not just the ``request`` logger,
so a second place that echoes the header fails here too.
"""

from __future__ import annotations

import json
import logging

import pytest

from r6.stepup import generate_step_up_token

#: A key-shaped value that is ALSO a valid tenant id ([A-Za-z0-9_-]{1,64}).
#: Synthetic: the shape of a provider API key, not a real one.
KEY_SHAPED = "sk-ant-api03-" + "Zx9Qw7Er5Ty3Ui1Op0As2Df4Gh6Jk8Lz"
#: A long hex run, the shape of an HMAC or a raw secret.
HEX_SECRET = "9f" * 32


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


def test_a_step_up_token_in_the_tenant_header_never_reaches_the_log(
        client, caplog, step_up_token):
    with caplog.at_level(logging.DEBUG):
        r = client.get("/r6/fhir/Patient",
                       headers={"X-Tenant-Id": step_up_token})

    assert r.status_code == 400
    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == "invalid"


def test_a_token_in_the_agent_and_request_id_headers_never_reaches_the_log(
        client, caplog, step_up_token, tenant_id):
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": tenant_id,
                            "X-Agent-Id": step_up_token,
                            "X-Request-Id": step_up_token})

    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["agent_id"] == "invalid"
    assert line["request_id"] == "invalid"


@pytest.mark.parametrize("value", [KEY_SHAPED, HEX_SECRET])
def test_a_credential_shaped_value_that_is_a_valid_id_is_still_withheld(
        client, caplog, value):
    """The tenant pattern alone admits these, so a pattern check is not enough."""
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": value, "X-Agent-Id": value,
                            "X-Request-Id": value})

    assert value not in _everything_logged(caplog)
    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == "invalid"
    assert line["agent_id"] == "invalid"
    assert line["request_id"] == "invalid"


def test_free_text_in_the_headers_is_not_logged(client, caplog):
    """No credential shape at all: the id pattern is what withholds it."""
    text = "Synthetic Person 1970-01-01 see notes"
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/Patient",
                   headers={"X-Tenant-Id": text, "X-Agent-Id": text,
                            "X-Request-Id": text})

    assert "Synthetic Person" not in _everything_logged(caplog)
    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == "invalid"
    assert line["agent_id"] == "invalid"
    assert line["request_id"] == "invalid"


def test_a_token_in_the_path_is_cut_out_of_the_logged_path(
        client, caplog, step_up_token, tenant_id):
    with caplog.at_level(logging.DEBUG):
        client.get(f"/r6/fhir/Patient/{step_up_token}",
                   headers={"X-Tenant-Id": tenant_id})

    _assert_absent(_everything_logged(caplog), step_up_token)
    (line,) = _access_lines(caplog)
    assert line["path"].startswith("/r6/fhir/Patient/")


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
    assert line["agent_id"] == "invalid"


def test_the_dev_server_access_line_is_scrubbed_too(app, caplog,
                                                    step_up_token):
    """werkzeug prints the raw path and query string on the local server.

    Emitted the way werkzeug's own _log does, because the test client never
    goes through the dev server.
    """
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("werkzeug").info(
            '%s - - [%s] "%s" %s %s', "127.0.0.1", "16/Oct/2026 22:00:00",
            f"GET /r6/fhir/Patient/{step_up_token}?t={step_up_token} HTTP/1.1",
            "400", "-")

    _assert_absent(_everything_logged(caplog), step_up_token)
    assert "/r6/fhir/Patient/[redacted]" in _everything_logged(caplog)


def test_ordinary_ids_are_still_logged_as_sent(client, caplog, tenant_id):
    """The scrub must not cost the operator the correlation it exists for."""
    agent = "careagents:agent_" + "0123456789abcdef" * 2
    with caplog.at_level(logging.DEBUG):
        r = client.get("/r6/fhir/Patient",
                       headers={"X-Tenant-Id": tenant_id,
                                "X-Agent-Id": agent,
                                "X-Request-Id": "corr-123"})

    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == tenant_id
    assert line["agent_id"] == agent
    assert line["request_id"] == "corr-123"
    assert line["path"] == "/r6/fhir/Patient"
    assert r.headers["X-Request-Id"] == "corr-123"


def test_absent_headers_log_as_a_dash(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get("/r6/fhir/metadata")

    (line,) = _access_lines(caplog)
    assert line["tenant_id"] == "-"
    assert line["agent_id"] == "-"


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
