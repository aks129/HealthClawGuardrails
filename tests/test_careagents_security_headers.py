"""CareAgents sends the browser security headers app.healthclaw.io sends.

careagents.cloud served none: no HSTS, CSP, X-Frame-Options or
X-Content-Type-Options, and Referrer-Policy only on the texted-link pages.
"""

from __future__ import annotations

import pytest

from careagents.accounts import AccountService
from careagents.app import create_app
from careagents.config import Config
from tests.test_careagents import FakeClient


def _app():
    cfg = Config(env={"CARE_DATABASE_URL": "sqlite:///:memory:",
                      "CARE_RP_ID": "localhost",
                      "CARE_ORIGIN": "http://localhost",
                      "HEALTHCLAW_MINT_SECRET": "mint-secret"})
    app = create_app(config=cfg, client=FakeClient(),
                     accounts=AccountService(cfg))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client():
    return _app().test_client()


def _csp(response) -> dict:
    out = {}
    for part in response.headers["Content-Security-Policy"].split(";"):
        name, _, value = part.strip().partition(" ")
        out[name] = value
    return out


@pytest.mark.parametrize("path", ["/", "/auth", "/healthz", "/no-such-page",
                                  "/static/careagents.css"])
def test_every_response_carries_the_headers(client, path):
    r = client.get(path)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    csp = _csp(r)
    assert csp["default-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'"


def test_the_policy_allows_what_the_pages_load_and_no_other_origin(client):
    r = client.get("/")
    csp = _csp(r)
    assert csp["script-src"] == "'self' 'unsafe-inline'"
    assert csp["style-src"] == "'self' 'unsafe-inline'"
    assert csp["img-src"] == "'self' data:"
    assert csp["font-src"] == "'self'"
    assert csp["connect-src"] == "'self'"
    assert "http" not in r.headers["Content-Security-Policy"]


def test_referrer_policy_is_strict_by_default_and_none_on_the_link_path(
        client):
    assert (client.get("/").headers["Referrer-Policy"]
            == "strict-origin-when-cross-origin")
    assert client.get("/auth").headers["Referrer-Policy"] == "no-referrer"
    assert client.get("/link").headers["Referrer-Policy"] == "no-referrer"


def test_hsts_only_over_https(client):
    assert "Strict-Transport-Security" not in client.get("/").headers
    # Railway terminates TLS and forwards plain HTTP with this header.
    r = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
    r = client.get("/", base_url="https://localhost")
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_production_always_sends_hsts():
    from flask import Flask

    from careagents import security_headers
    app = Flask(__name__)
    security_headers.install(app, production=True)
    app.add_url_rule("/x", "x", lambda: "ok")
    r = app.test_client().get("/x")
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_a_header_a_handler_set_is_kept():
    app = _app()
    app.add_url_rule("/_own_frame_policy", "own",
                     lambda: ("ok", 200, {"X-Frame-Options": "SAMEORIGIN"}))
    r = app.test_client().get("/_own_frame_policy")
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
