"""Security review of #940: the patient connect view (#937).

The sig on /connect/<tenant>?src=careagents picks a template branch. What
it does not do is change any server decision: POST /fasten/connections
still sets the enrollment proof in the browser, and GET .../agent-access
mints a 30-day read token for whoever holds that proof. So "the patient
view never mints a read token" holds for the page's script only.

SEC940-F1 pinned that gap and SEC940-F2 pinned a token outliving
Disconnect; both are fixed server-side (tests/test_fasten_token_scope.py
holds the narrower pins). The rest pin what held: forged or replayed sigs,
the back link, and the confirm-link session.

Synthetic tenant and connection ids only.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import pytest

SECRET = "sec940-connect-view-secret"
CA_TENANT = "ca-00aa11bb22"


def _tag(tenant: str, secret: str = SECRET) -> str:
    key = hashlib.sha256(b"healthclaw-connect-view:" + secret.encode()).digest()
    return hmac.new(key, tenant.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FASTEN_PUBLIC_KEY", "public_test_patient")
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CAREAGENTS_CONSENT_URL", raising=False)


def _patient_view(html: str) -> bool:
    return 'id="back-to-careagents"' in html


# --- SEC940-F1: the patient view is a template, not a server decision -------

def test_patient_view_browser_cannot_mint_a_read_token(client, app, env):
    from models import db
    from r6.fasten.models import FastenConnection

    page = client.get(
        f"/connect/{CA_TENANT}?src=careagents&sig={_tag(CA_TENANT)}")
    assert page.status_code == 200 and _patient_view(page.get_data(as_text=True))

    # Exactly the request the patient page's own script sends.
    org_id = "sec940-oc-1"
    assert client.post(
        "/fasten/connections",
        headers={"X-Tenant-Id": CA_TENANT},
        json={"org_connection_id": org_id},
    ).status_code == 201

    # Fasten's signed connection_success lands (seconds later in prod).
    conn = db.session.get(FastenConnection, org_id)
    conn.webhook_verified_at = datetime.now(timezone.utc)
    db.session.commit()

    # One devtools fetch from the same browser.
    minted = client.get(f"/fasten/connections/{org_id}/agent-access")
    assert minted.status_code != 200, (
        "patient view minted a read token: "
        + str(sorted((minted.get_json() or {}).keys())))


# --- what held ---------------------------------------------------------------

@pytest.mark.parametrize("sig", [
    "",                                   # empty
    _tag(CA_TENANT).upper(),              # same tag, other case
    _tag("ca-ffffffffff"),                # replay across tenants
    _tag(CA_TENANT, "other-secret"),      # wrong key
    hmac.new(SECRET.encode(), CA_TENANT.encode(),
             hashlib.sha256).hexdigest(),  # raw secret, no domain label
    hmac.new(hashlib.sha256(b"healthclaw-consent-handoff:"
                            + SECRET.encode()).digest(),
             CA_TENANT.encode(), hashlib.sha256).hexdigest(),  # handoff key
    _tag(CA_TENANT)[:32],                 # truncated
    _tag(CA_TENANT) + "00",               # extended
    "é" * 64,                             # non-ASCII, never a 500
])
def test_forged_sig_gets_the_developer_view(client, env, sig):
    r = client.get(f"/connect/{CA_TENANT}",
                   query_string={"src": "careagents", "sig": sig})
    assert r.status_code == 200
    assert not _patient_view(r.get_data(as_text=True))


def test_sig_without_src_or_with_wrong_src_is_the_developer_view(client, env):
    for qs in ({"sig": _tag(CA_TENANT)},
               {"src": "CareAgents", "sig": _tag(CA_TENANT)},
               {"src": "careagents ", "sig": _tag(CA_TENANT)}):
        html = client.get(f"/connect/{CA_TENANT}", query_string=qs
                          ).get_data(as_text=True)
        assert not _patient_view(html), qs


def test_back_link_ignores_the_request(client, env):
    url = (f"/connect/{CA_TENANT}?src=careagents&sig={_tag(CA_TENANT)}"
           "&careagents_home=https://evil.example/&next=https://evil.example/")
    html = client.get(url, headers={
        "Host": "evil.example",
        "X-Forwarded-Host": "evil.example",
        "Referer": "https://evil.example/",
    }).get_data(as_text=True)
    assert _patient_view(html)
    assert "evil.example" not in html
    assert 'href="https://careagents.cloud/home"' in html


@pytest.mark.parametrize("configured", [
    "javascript:alert(1)//careagents.cloud",
    "https://user@evil.example/consent",
    "https://evil.example\" onmouseover=\"alert(1)/x",
    "//evil.example/consent",
    "data:text/html,<script>alert(1)</script>",
])
def test_bad_consent_url_config_falls_back_to_the_hub(
        client, env, monkeypatch, configured):
    monkeypatch.setenv("CAREAGENTS_CONSENT_URL", configured)
    html = client.get(
        f"/connect/{CA_TENANT}?src=careagents&sig={_tag(CA_TENANT)}"
    ).get_data(as_text=True)
    assert 'href="https://careagents.cloud/home"' in html
    assert "evil.example" not in html and "javascript:" not in html


def test_unsigned_page_gives_no_tenant_oracle(client, env):
    """A stranger cannot tell a live CareAgents tenant from a made-up one."""
    from models import db
    from r6.fasten.models import FastenConnection

    db.session.add(FastenConnection(org_connection_id="sec940-oc-live",
                                    tenant_id=CA_TENANT))
    db.session.commit()
    live = client.get(f"/connect/{CA_TENANT}").get_data(as_text=True)
    made_up = client.get("/connect/ca-99ee88dd77").get_data(as_text=True)
    assert live.replace(CA_TENANT, "T") == made_up.replace("ca-99ee88dd77", "T")


def test_disconnect_ends_an_issued_read_token(client, app, env):
    # Since F1 the patient view's browser cannot mint this token, so take
    # one minted before the fix, exactly as agent-access shaped it.
    from r6.stepup import (PATIENT_CONNECT_SUBJECT, READ_TOKEN_TTL_SECONDS,
                           generate_step_up_token)

    token = generate_step_up_token(
        CA_TENANT, agent_id=PATIENT_CONNECT_SUBJECT,
        ttl_seconds=READ_TOKEN_TTL_SECONDS, scope="read")
    reader = app.test_client()
    headers = {"X-Tenant-Id": CA_TENANT, "X-Step-Up-Token": token}
    assert reader.get("/r6/fhir/Observation", headers=headers).status_code == 200

    assert client.post("/r6/fhir/internal/fasten-revoke",
                       json={"tenant_id": CA_TENANT},
                       headers={"X-Internal-Secret": SECRET}).status_code == 200
    assert reader.get("/r6/fhir/Observation",
                      headers=headers).status_code in (401, 403, 410)
