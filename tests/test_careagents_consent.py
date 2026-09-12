"""CareAgents is the consent front door for the MCP connector (spec §13).

HealthClaw parks the OAuth request and sends the browser here; this suite
holds what CareAgents does with it: verifies the handle before fetching
anything, offers only the person's own connections (real ones only where the
beta gate opens them), grants nothing without a fresh user-verified passkey
for this account, sends back a decision HealthClaw's own decoder accepts, and
takes a consent back at HealthClaw before showing it revoked here.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest

from careagents import consent

#: The HealthClaw decoder lands with #568's consent-handoff PR. Until it is on
#: main the cross-layer rows below skip, and `_reference_decode` holds the
#: format on this side; once it lands they run against the real thing.
try:
    from r6.oauth import decode_grant as _healthclaw_decode
except ImportError:  # pragma: no cover - main before the handoff PR
    _healthclaw_decode = None

cross_layer = pytest.mark.skipif(
    _healthclaw_decode is None,
    reason="r6.oauth.decode_grant is not on this branch yet (#568 PR 3)")


def _reference_decode(grant, secret="mint-secret"):
    """The format, spelled out: `<base64url(JSON)>.<hex HMAC-SHA256>` under
    sha256(b'healthclaw-consent-handoff:' + secret). Not HealthClaw's code."""
    body, tag = grant.split(".")
    key = hashlib.sha256(b"healthclaw-consent-handoff:" + secret.encode()).digest()
    assert hmac.compare_digest(tag, hmac.new(key, body.encode(), hashlib.sha256).hexdigest())
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def decode_grant(grant):
    if _healthclaw_decode is not None:
        return _healthclaw_decode(grant)
    try:
        return _reference_decode(grant)
    except AssertionError:
        return None
from careagents.config import Config
from tests.test_careagents import FakeClient, _make_account

MINT = "mint-secret"
PUBLIC = "https://app.healthclaw.io"


def _cfg(real_records="on"):
    import os
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        from careagents.models import Base, make_engine
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    return Config(env={"CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
                       "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
                       "HEALTHCLAW_MINT_SECRET": MINT,
                       "HEALTHCLAW_PUBLIC_BASE": PUBLIC,
                       "FASTEN_PUBLIC_KEY": "pub123",
                       "CARE_REAL_RECORDS": real_records,
                       "CARE_TELEGRAM_BOT": "carebot"})


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def cfg():
    return _cfg()


@pytest.fixture
def svc(cfg):
    from careagents.accounts import AccountService
    return AccountService(cfg)


@pytest.fixture
def app(cfg, svc, fake):
    from careagents.app import create_app
    a = create_app(config=cfg, client=fake, accounts=svc)
    a.config["TESTING"] = True
    return a


def _handle(request_id="req-1", exp=None, secret=MINT):
    exp = exp or int(time.time()) + 600
    return f"{request_id}.{exp}.{consent.tag(secret, f'{request_id}.{exp}')}"


def _signed_in(app, svc, monkeypatch, email="pat@example.com", passkey=False):
    acct = _make_account(svc, monkeypatch, email)
    client = app.test_client()
    with client.session_transaction() as s:
        s["account_id"] = acct.id
    if passkey:
        monkeypatch.setattr(svc, "has_passkey", lambda account_id: True)
    return client, acct


def _connect(svc, acct, kind="sample", label="My records", provider=None):
    return svc.add_connection(acct.id, kind, f"ca-{kind}-{acct.id[-4:]}", label,
                              provider=provider, consent_version="v1")


# --- the handle and the grant are the same bytes HealthClaw uses -------------


def test_parse_handle_accepts_only_a_verified_unexpired_handle():
    """MUTATION: skip the tag comparison in parse_handle -> the forged row
    passes; skip the expiry -> the expired row does."""
    assert consent.parse_handle(_handle(), MINT) == "req-1"
    assert consent.parse_handle(_handle(secret="guessed"), MINT) is None
    assert consent.parse_handle(_handle(exp=int(time.time()) - 1), MINT) is None
    assert consent.parse_handle("req-1.notanumber.abc", MINT) is None
    assert consent.parse_handle("", MINT) is None
    assert consent.parse_handle(_handle(), "") is None


def test_build_grant_is_what_healthclaw_decodes(monkeypatch):
    """Cross-layer: the grant CareAgents signs is the grant r6.oauth verifies,
    under the same derived key. MUTATION: change the domain-separation
    string on either side -> red."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT)
    grant, consent_id = consent.build_grant(MINT, "req-1", "approved", tenant_id="ca-x")
    payload = decode_grant(grant)
    assert payload is not None
    assert payload["decision"] == "approved" and payload["tenant_id"] == "ca-x"
    assert payload["consent_id"] == consent_id and payload["request_id"] == "req-1"
    assert payload["exp"] > time.time() and payload["nonce"]
    denied, _ = consent.build_grant(MINT, "req-1", "denied")
    assert decode_grant(denied)["decision"] == "denied"
    assert decode_grant(denied)["tenant_id"] is None
    if _healthclaw_decode is not None:
        monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "another")
        assert decode_grant(grant) is None


def test_an_approval_names_a_tenant_and_a_decision_is_one_of_two():
    with pytest.raises(ValueError):
        consent.build_grant(MINT, "req-1", "approved")
    with pytest.raises(ValueError):
        consent.build_grant(MINT, "req-1", "maybe")


# --- the page ----------------------------------------------------------------


def test_a_forged_link_bounces_before_anything_is_fetched(app, fake):
    resp = app.test_client().get("/authorize", query_string={"req": _handle(secret="x")})
    assert resp.status_code == 400
    assert fake.consent_requests == []


def test_a_valid_link_signed_out_goes_to_sign_in_and_resumes_after(
        app, svc, monkeypatch):
    client = app.test_client()
    resp = client.get("/authorize", query_string={"req": _handle()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth")
    with client.session_transaction() as s:
        assert s["consent_req"].startswith("req-1."), s["consent_req"]
        assert consent.parse_handle(s["consent_req"], MINT) == "req-1"
    acct = _make_account(svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        s["account_id"] = acct.id
    home = client.get("/home")
    assert home.status_code == 302
    assert "/authorize?req=req-1." in home.headers["Location"]


def test_the_page_names_the_client_the_permissions_and_the_persons_connections(
        app, svc, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    _connect(svc, acct, "fasten", "Clinic records", provider="Epic (Fasten)")
    resp = client.get("/authorize", query_string={"req": _handle()})
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Claude wants to read your records" in page
    assert "Read your health records" in page and "summaries" in page
    assert "Sample records" in page and "Clinic records" in page
    assert 'id="approve-btn"' in page and "Don't allow" in page


def test_real_connections_are_not_offered_when_the_beta_gate_is_closed(
        svc, fake, monkeypatch):
    """§13.7: CARE_REAL_RECORDS gates sharing exactly as it gates connecting.
    MUTATION: drop the real_open filter in _offered_connections -> red."""
    from careagents.accounts import AccountService
    from careagents.app import create_app
    cfg = _cfg(real_records="off")
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    real = _connect(svc, acct, "fasten", "Clinic records", provider="Epic (Fasten)")
    page = client.get("/authorize", query_string={"req": _handle()}).get_data(as_text=True)
    assert "Sample records" in page and "Clinic records" not in page
    assert "Only sample records can be shared" in page
    # And the decision endpoint refuses the real one even if the id is known.
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: acct)
    resp = client.post("/authorize/decide", data=json.dumps({
        "req": _handle(), "decision": "approved", "connection_id": real,
        "passkey": {}}), content_type="application/json")
    assert resp.status_code == 403


def test_without_a_passkey_the_page_offers_enrolment_not_approval(
        app, svc, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch, passkey=False)
    _connect(svc, acct)
    page = client.get("/authorize", query_string={"req": _handle()}).get_data(as_text=True)
    assert 'id="approve-btn"' not in page
    assert "/auth?enroll=1" in page


def test_an_expired_request_says_so(app, svc, fake, monkeypatch):
    client, _ = _signed_in(app, svc, monkeypatch)
    fake.parked = None
    resp = client.get("/authorize", query_string={"req": _handle()})
    assert resp.status_code == 410
    assert "expired" in resp.get_data(as_text=True)


# --- the decision --------------------------------------------------------------


def _approve(client, connection_id, req=None, passkey=None):
    return client.post("/authorize/decide", data=json.dumps({
        "req": req or _handle(), "decision": "approved",
        "connection_id": connection_id, "passkey": passkey or {"rawId": "x"}}),
        content_type="application/json")


def test_an_approval_needs_a_fresh_verified_passkey_for_this_account(
        app, svc, monkeypatch):
    """MUTATION: drop the `verified.id != acct.id` check -> the other-account
    row goes green; drop the challenge pop -> the no-challenge row does."""
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    conn = _connect(svc, acct)
    other = _make_account(svc, monkeypatch, "someone@example.com")

    # No challenge minted: the assertion cannot be fresh, whatever the
    # verifier would say about it (it would say yes here).
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: acct)
    resp = _approve(client, conn)
    assert resp.status_code == 400 and resp.get_json()["error"] == "no challenge"

    # A passkey that verifies as another account.
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: other)
    assert _approve(client, conn).status_code == 403

    # The verifier is asked for user verification, not presence.
    seen = {}

    def verify(cred, ch, require_uv=False):
        seen["require_uv"] = require_uv
        return acct
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication", verify)
    resp = _approve(client, conn)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert seen["require_uv"] is True


def test_an_approval_sends_back_a_grant_healthclaw_decodes_and_records_it(
        app, svc, fake, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT)
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    conn = _connect(svc, acct, "fasten", "Clinic records", provider="Epic (Fasten)")
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: acct)
    resp = _approve(client, conn)
    assert resp.status_code == 200
    redirect = resp.get_json()["redirect"]
    assert redirect.startswith(PUBLIC + "/r6/fhir/oauth/consent/return?grant=")
    payload = decode_grant(redirect.split("grant=", 1)[1])
    assert payload["decision"] == "approved"
    assert payload["tenant_id"] == svc.get_connection(acct.id, conn)["tenant_id"]
    grants = svc.list_grants(acct.id)
    assert len(grants) == 1
    g = grants[0]
    assert g["consent_id"] == payload["consent_id"]
    assert g["client_name"] == "Claude" and g["client_id"] == "cid-claude"
    assert g["scopes"] == "fhir.read context.read" and g["status"] == "active"
    assert g["connection_id"] == conn


def test_a_denial_needs_no_passkey_and_records_nothing(app, svc, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT)
    client, acct = _signed_in(app, svc, monkeypatch)
    resp = client.post("/authorize/decide", data=json.dumps({
        "req": _handle(), "decision": "denied"}), content_type="application/json")
    assert resp.status_code == 200
    payload = decode_grant(resp.get_json()["redirect"].split("grant=", 1)[1])
    assert payload["decision"] == "denied" and payload["tenant_id"] is None
    assert svc.list_grants(acct.id) == []


def test_a_decision_on_a_forged_or_foreign_request_is_refused(
        app, svc, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    conn = _connect(svc, acct)
    stranger = _make_account(svc, monkeypatch, "stranger@example.com")
    theirs = _connect(svc, stranger)
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: acct)
    assert _approve(client, conn, req=_handle(secret="x")).status_code == 400
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    assert _approve(client, theirs).status_code == 404, \
        "another account's connection is not this person's to share"


def test_the_hub_lists_grants_and_revokes_at_healthclaw_first(
        app, svc, fake, monkeypatch):
    """MUTATION: mark the row revoked before hc.revoke_consent, or ignore its
    failure -> the failing-revoke assertions go red."""
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    gid = svc.add_grant(acct.id, conn, tenant, "cid-claude", "Claude",
                        "fhir.read", "consent_abc")
    page = client.get("/home").get_data(as_text=True)
    assert "Shared with other apps" in page and "Claude" in page
    assert f'data-grant="{gid}"' in page

    fake.revoke_fails = True
    resp = client.post(f"/api/grants/{gid}/revoke")
    assert resp.status_code == 502 and resp.get_json()["revoked"] is False
    assert svc.get_grant(acct.id, gid)["status"] == "active"

    fake.revoke_fails = False
    resp = client.post(f"/api/grants/{gid}/revoke")
    assert resp.status_code == 200 and resp.get_json()["revoked"] is True
    assert fake.revoked == ["consent_abc"]
    assert svc.get_grant(acct.id, gid)["status"] == "revoked"
    assert client.post("/api/grants/nope/revoke").status_code == 404


def test_consent_options_ask_the_authenticator_for_user_verification(
        app, svc, monkeypatch):
    client, _ = _signed_in(app, svc, monkeypatch)
    resp = client.post("/webauthn/consent/options")
    assert resp.status_code == 200
    assert resp.get_json()["userVerification"] == "required"
    with client.session_transaction() as s:
        assert s["wa_consent_challenge"]


def test_deleting_a_connection_revokes_its_grants_at_healthclaw_and_keeps_the_record(
        app, svc, fake, monkeypatch):
    """The grant row carries a foreign key to the connection. Postgres refuses
    the delete while it points at the row; SQLite never would (found in
    review, not by a test). The grant is revoked at HealthClaw first, then
    detached, then the connection goes. Runs on both CI lanes.

    MUTATION: drop the detach loop in delete_connection -> red on Postgres;
    drop the revoke loop in the route -> the fake records no revocation.
    """
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    gid = svc.add_grant(acct.id, conn, tenant, "cid-claude", "Claude", "fhir.read", "consent_del")
    fake.purge_tenant = lambda t: {"rows_deleted": 3}
    resp = client.delete(f"/api/connections/{conn}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["deleted"] is True
    assert fake.revoked == ["consent_del"]
    assert svc.get_connection(acct.id, conn) is None
    g = svc.get_grant(acct.id, gid)
    assert g is not None and g["status"] == "revoked" and g["connection_id"] is None


def test_a_revoke_that_cannot_be_confirmed_keeps_the_connection_listed(
        app, svc, fake, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    gid = svc.add_grant(acct.id, conn, tenant, "cid-claude", "Claude", "fhir.read", "consent_x")
    fake.purge_tenant = lambda t: {"rows_deleted": 3}
    fake.revoke_fails = True
    resp = client.delete(f"/api/connections/{conn}")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["deleted"] is True and body["unlinked"] is False and body["grants_active"] == 1
    assert svc.get_connection(acct.id, conn) is not None, "still listed, so Delete can be retried"
    assert svc.get_grant(acct.id, gid)["status"] == "active"
