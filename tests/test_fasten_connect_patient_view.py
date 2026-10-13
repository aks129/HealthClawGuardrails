"""The connect page opened from CareAgents is a patient page (#937).

CareAgents signs the tenant into the link it builds; the engine checks the
tag and renders a plain page: no HealthClaw nav, no tenant id, and no
agent-access path at all, so the read token is never minted or shown from
it. The "Back to CareAgents" link is a server setting, never a parameter.
Any other caller gets the developer page, unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

SECRET = "connect-view-test-secret"
TENANT = "ca-0123456789"


def _tag(tenant: str, secret: str = SECRET) -> str:
    key = hashlib.sha256(b"healthclaw-connect-view:" + secret.encode()).digest()
    return hmac.new(key, tenant.encode(), hashlib.sha256).hexdigest()


def _setup(monkeypatch, **env):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_patient")
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _get(client, tenant=TENANT, **params):
    from urllib.parse import urlencode
    q = ("?" + urlencode(params)) if params else ""
    r = client.get(f"/connect/{tenant}{q}")
    assert r.status_code == 200
    return r.get_data(as_text=True)


_TOKEN_PATH = ("agent-access", "Read token", "_showAgentAccess",
               "_pollAgentAccess", "assistant key", "MCP")


def test_signed_link_renders_the_patient_page(client, monkeypatch):
    _setup(monkeypatch)
    html = _get(client, src="careagents", sig=_tag(TENANT))
    assert 'id="main-nav"' not in html
    assert "Command Center" not in html
    assert "Tenant:" not in html
    for word in _TOKEN_PATH:
        assert word not in html, word
    # A way back, to the CareAgents hub, from server config.
    assert 'id="back-to-careagents"' in html
    assert 'href="https://careagents.cloud/home"' in html
    # Honest timing: no "nothing will notify you" dead end.
    assert "Nothing will notify you" not in html
    assert "Nothing notifies you" not in html
    # The widget still registers the connection.
    assert "/fasten/connections" in html
    # The widget iframe loads from Fasten's embed host itself: the src is
    # parsed and its scheme and host compared exactly, never as a substring.
    srcs = [urlsplit(unescape(m)) for m in
            re.findall(r'src="(https://[^"]+)"', html)]
    assert any(u.scheme == "https"
               and u.hostname == "embed.connect.fastenhealth.com"
               and parse_qs(u.query).get("external-id") == [TENANT]
               for u in srcs), srcs


def test_developer_page_is_unchanged_without_a_valid_tag(client, monkeypatch):
    _setup(monkeypatch)
    for params in ({}, {"src": "careagents"},
                   {"src": "careagents", "sig": "0" * 64},
                   {"src": "careagents", "sig": _tag("ca-someoneelse")},
                   {"src": "careagents", "sig": _tag(TENANT, "wrong")},
                   # Non-ASCII is a mismatch, never a 500 (#557).
                   {"src": "careagents", "sig": "café" * 16}):
        html = _get(client, **params)
        assert 'id="main-nav"' in html, params
        assert "Tenant:" in html, params
        assert "agent-access" in html, params
        assert 'id="back-to-careagents"' not in html, params


def test_no_secret_means_no_patient_view(client, monkeypatch):
    """Unset secret: nothing can be verified, so the developer page."""
    _setup(monkeypatch)
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET")
    html = _get(client, src="careagents", sig=_tag(TENANT, ""))
    assert 'id="back-to-careagents"' not in html


def test_back_link_never_comes_from_the_request(client, monkeypatch):
    _setup(monkeypatch)
    html = _get(client, src="careagents", sig=_tag(TENANT),
                back="https://evil.example", next="https://evil.example",
                return_to="https://evil.example")
    assert "evil.example" not in html
    assert 'href="https://careagents.cloud/home"' in html


def test_back_link_follows_the_configured_careagents_origin(client,
                                                            monkeypatch):
    _setup(monkeypatch,
           CAREAGENTS_CONSENT_URL="http://localhost:5391/authorize")
    html = _get(client, src="careagents", sig=_tag(TENANT))
    assert 'href="http://localhost:5391/home"' in html


def test_a_malformed_configured_origin_falls_back(client, monkeypatch):
    _setup(monkeypatch, CAREAGENTS_CONSENT_URL="javascript:alert(1)//x")
    html = _get(client, src="careagents", sig=_tag(TENANT))
    assert "javascript:" not in html
    assert 'href="https://careagents.cloud/home"' in html


def test_patient_page_still_closes_a_disconnected_tenant(client, monkeypatch):
    """The disconnected page wins over either view."""
    _setup(monkeypatch)
    from r6.fasten import models
    monkeypatch.setattr(models, "tenant_closed", lambda t: True)
    html = _get(client, src="careagents", sig=_tag(TENANT))
    assert "embed.connect.fastenhealth.com" not in html


def test_the_link_careagents_builds_opens_the_patient_page(client,
                                                           monkeypatch):
    """Cross-layer: the real CareAgents builder against the real engine
    route, so the two derivations of the tag cannot drift apart."""
    from urllib.parse import urlsplit
    from careagents.healthclaw import HealthClawClient
    _setup(monkeypatch)
    url = HealthClawClient("https://engine.example", SECRET).fasten_connect_url(
        TENANT)
    parts = urlsplit(url)
    html = client.get(f"{parts.path}?{parts.query}").get_data(as_text=True)
    assert 'id="back-to-careagents"' in html
    assert "agent-access" not in html
    # A CareAgents client that holds no secret builds an untagged link.
    bare = HealthClawClient("https://engine.example", "").fasten_connect_url(
        TENANT)
    assert bare == f"https://engine.example/connect/{TENANT}"


def test_the_patient_page_says_tap_not_click(client, monkeypatch):
    """It opens on a phone (patient tester on #940)."""
    _setup(monkeypatch)
    html = _get(client, src="careagents", sig=_tag(TENANT))
    assert "Tap the button below." in html
    assert "Click the button below" not in html
