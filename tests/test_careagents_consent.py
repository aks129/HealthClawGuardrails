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
    service = AccountService(cfg)
    yield service
    service.engine.dispose()  # see tests/test_careagents.py svc (#232)


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
        app, svc, fake, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    _connect(svc, acct, "fasten", "Clinic records", provider="Epic (Fasten)")
    fake.parked["redirect_host"] = "claude.ai"
    resp = client.get("/authorize", query_string={"req": _handle()})
    page = _words(resp.get_data(as_text=True))
    assert resp.status_code == 200
    assert "claude.ai wants to read your records" in page
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


def test_a_denial_needs_no_passkey_and_records_nothing(app, svc, fake, monkeypatch):
    """A recognized host gets the OAuth access_denied back; see the
    unrecognized-host rows below for the page that stays here."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT)
    fake.parked["redirect_host"] = "claude.ai"
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
    page = client.get("/settings").get_data(as_text=True)
    assert "Apps you have shared records with" in page and "Claude" in page
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


# --- who is asking: the address the code goes to, not the name it chose -----
#
# Registration is open (spec §13.1), so anyone can register a client named
# "Claude" with their own callback and send a person the authorize link. The
# name is the client's claim; the redirect host is where the code actually
# goes, and it is the one thing such a client cannot fake.


def _ask_page(app, svc, fake, monkeypatch, host, name="Claude"):
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    fake.parked["client_name"] = name
    if host is None:
        fake.parked.pop("redirect_host", None)
    else:
        fake.parked["redirect_host"] = host
    resp = client.get("/authorize", query_string={"req": _handle()})
    assert resp.status_code == 200
    return _words(resp.get_data(as_text=True))


HOST_OPEN = '<span class="consent-host">'


def _words(page):
    """The page as read: the host's wrapper and its line-break hints (one
    after each dot) are markup around the words, not words."""
    return page.replace("<wbr>", "").replace(HOST_OPEN, "").replace("</span>", "")


CAUTION = "We don't recognize this address"
STOP_LINE = "If you didn't just ask {} to connect, tap Don't allow."


@pytest.mark.parametrize("host", ["claude.ai", "claude.com"])
def test_a_recognized_host_heads_the_page_and_reads_as_recognized(
        app, svc, fake, monkeypatch, host):
    page = _ask_page(app, svc, fake, monkeypatch, host)
    assert f"{host} wants to read your records" in page
    assert f"you'll go back to <strong>{host}</strong>" in page
    assert "calls itself" in page and "Claude" in page
    assert "check the web address above" in page
    assert STOP_LINE.format(host) in page
    assert CAUTION not in page and "This is not" not in page


@pytest.mark.parametrize("host", [
    "evil.example", "claude.ai.evil.example", "evilclaude.ai", "localhost"])
def test_any_other_host_is_named_with_a_caution_whatever_the_client_calls_itself(
        app, svc, fake, monkeypatch, host):
    """The attack: a client named "Claude" whose code goes elsewhere.
    MUTATION: treat every host as recognized -> red; match on suffix -> the
    lookalike rows go red."""
    page = _ask_page(app, svc, fake, monkeypatch, host, name="Claude")
    assert f"{host} wants to read your records" in page
    assert CAUTION in page
    assert f"Only allow it if you started this from {host} yourself" in page
    assert STOP_LINE.format(host) in page
    assert "Claude wants to read" not in page
    assert "this app" not in page.split("It would be able to")[0].split(CAUTION)[1]


def test_without_a_host_the_page_says_it_cannot_tell_where_the_code_goes(
        app, svc, fake, monkeypatch):
    """A HealthClaw that predates redirect_host: caution, never recognition."""
    page = _ask_page(app, svc, fake, monkeypatch, None)
    assert "An app wants to read your records" in page
    assert "We can't tell which address you'd go back to" in page
    assert STOP_LINE.format("an app") in page
    assert "go back to <strong" not in page


@pytest.mark.parametrize("host, not_this", [
    ("claude.ai.secure-login.example", "claude.ai"),
    ("claudeai-help.example", "claude.ai"),
    ("evilclaude.ai", "claude.ai"),
    ("claude.com.evil.example", "claude.com"),
    ("my-claude-tools.example", "claude.ai"),
])
def test_a_host_that_borrows_a_recognized_name_says_plainly_it_is_not_that(
        app, svc, fake, monkeypatch, host, not_this):
    """MUTATION: never set lookalike_of -> red; set it for exact hosts too ->
    the recognized rows above go red."""
    page = _ask_page(app, svc, fake, monkeypatch, host)
    assert f"This is not {not_this}." in page
    # Near the top: straight after the headline, before the scopes.
    top = page.split("It would be able to")[0]
    assert top.index("wants to read your records") < top.index(f"This is not {not_this}.")


@pytest.mark.parametrize("host", ["evil.example", "localhost", None])
def test_an_unrecognized_host_makes_dont_allow_the_primary_button(
        app, svc, fake, monkeypatch, host):
    """MUTATION: keep Allow primary for every host -> red."""
    page = _ask_page(app, svc, fake, monkeypatch, host)
    assert 'class="btn-primary btn-block" id="deny-btn"' in page
    assert 'class="btn-secondary btn-block" id="approve-btn"' in page
    assert page.index('id="deny-btn"') < page.index('id="approve-btn"')


@pytest.mark.parametrize("host", ["claude.ai", "claude.com"])
def test_a_recognized_host_keeps_allow_primary(app, svc, fake, monkeypatch, host):
    page = _ask_page(app, svc, fake, monkeypatch, host)
    assert 'class="btn-primary btn-block" id="approve-btn"' in page
    assert 'class="btn-secondary btn-block" id="deny-btn"' in page


def test_the_host_wraps_only_at_its_dots(app, svc, fake, monkeypatch):
    """A host is one long word: at 375px it wraps after a dot, never mid-label.
    MUTATION: render the host without <wbr> -> red."""
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    fake.parked["redirect_host"] = "claude.ai.secure-login.example"
    page = client.get("/authorize", query_string={"req": _handle()}).get_data(as_text=True)
    assert (f"{HOST_OPEN}claude.<wbr>ai.<wbr>secure-login.<wbr>example</span> "
            "wants to read") in page
    import pathlib
    css = pathlib.Path("careagents/static/careagents.css").read_text()
    consent_css = css.split("--- consent page")[1].split("Leaving (#554)")[0]
    assert "anywhere" not in consent_css


def test_the_client_name_and_host_are_escaped_on_the_page(app, svc, fake, monkeypatch):
    """Jinja autoescape is on for consent.html; this row is the proof.
    MUTATION: render client_name with |safe -> red."""
    page = _ask_page(app, svc, fake, monkeypatch, "evil.example",
                     name='<img src=x onerror="alert(1)">Claude')
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;Claude" in page


def test_the_host_is_escaped_between_its_line_breaks(app, svc, fake, monkeypatch):
    """The <wbr> hints are markup; the labels between them are not.
    MUTATION: join the raw labels without escape() -> red."""
    page = _ask_page(app, svc, fake, monkeypatch, 'evil<b>.example"')
    assert "evil<b>" not in page
    assert "evil&lt;b&gt;.example&#34; wants to read" in page


def test_app_identity_matches_hosts_exactly():
    ident = consent.app_identity({"redirect_host": "CLAUDE.AI", "client_name": "C"})
    assert ident["client_name"] == "C" and ident["redirect_host"] == "claude.ai"
    assert ident["host_recognized"] is True and ident["lookalike_of"] is None
    assert str(ident["host_html"]) == f"{HOST_OPEN}claude.<wbr>ai</span>"
    for host in ("claude.ai.evil.example", "evilclaude.ai", "", None, 7):
        ident = consent.app_identity({"redirect_host": host})
        assert ident["host_recognized"] is False, host
    assert consent.app_identity({})["client_name"] == "An agent"
    assert consent.app_identity({})["host_html"] is None


@pytest.mark.parametrize("given, shown", [
    ("clаude.com", "xn--clude-5ve.com"),          # Cyrillic a
    ("claude.ai。evil.example", "claude.ai.evil.example"),
    ("claude.ai．evil.example", "claude.ai.evil.example"),
    ("claude.ai｡evil.example", "claude.ai.evil.example"),
])
def test_app_identity_shows_a_non_ascii_host_as_the_browser_resolves_it(given, shown):
    """Defence in depth: HealthClaw sends A-labels, but the page never shows
    a Unicode host whatever it is sent. MUTATION: skip the A-label step -> red."""
    ident = consent.app_identity({"redirect_host": given})
    assert ident["redirect_host"] == shown and ident["host_recognized"] is False


def test_app_identity_shows_no_host_rather_than_one_it_cannot_encode():
    ident = consent.app_identity({"redirect_host": "a" * 64 + "é.example"})
    assert ident["redirect_host"] is None and ident["host_recognized"] is False


# --- signing in on the way to a consent request ------------------------------


def test_signing_in_by_email_code_resumes_the_consent_request(app, svc, fake, monkeypatch):
    """Bug A: _login clears the session against fixation, and used to take the
    parked request with it, so the person landed on the hub instead.
    MUTATION: clear consent_req along with the rest -> red."""
    from tests.test_careagents import _login as email_login
    client = app.test_client()
    req = _handle()
    assert client.get("/authorize", query_string={"req": req}).status_code == 302
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "planted-before-sign-in"
        s["something_else"] = "planted-before-sign-in"
    email_login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        # Only the parked handle survives the clear.
        assert s["consent_req"] == req
        assert "wa_consent_challenge" not in s and "something_else" not in s
    # "Skip for now" and every other post-sign-in path land on /home.
    home = client.get("/home")
    assert home.status_code == 302
    assert home.headers["Location"].endswith(f"/authorize?req={req}")
    page = client.get(home.headers["Location"])
    assert page.status_code == 200 and "wants to read your records" in page.get_data(as_text=True)
    # Once: the hub is the hub again afterwards.
    assert client.get("/home").status_code == 200


def test_a_forged_consent_handle_is_not_carried_across_sign_in(app, svc, monkeypatch):
    from tests.test_careagents import _login as email_login
    client = app.test_client()
    with client.session_transaction() as s:
        s["consent_req"] = _handle(secret="guessed")
    email_login(client, svc, monkeypatch, "pat@example.com")
    with client.session_transaction() as s:
        assert "consent_req" not in s


# --- saying no ------------------------------------------------------------------


def test_dont_allow_on_an_unrecognized_host_stays_here_and_sends_nothing(
        app, svc, fake, monkeypatch):
    """Bug B: a denial used to send the browser to the client's redirect URI,
    which for a stranger's client is the stranger's page.
    MUTATION: send the access_denied grant for every host -> red."""
    client, acct = _signed_in(app, svc, monkeypatch)
    fake.parked["redirect_host"] = "evil.example"
    resp = client.post("/authorize/decide", data=json.dumps({
        "req": _handle(), "decision": "denied"}), content_type="application/json")
    assert resp.status_code == 200
    redirect = resp.get_json()["redirect"]
    assert redirect == "/authorize/declined"
    assert "grant=" not in redirect
    page = client.get(redirect)
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "You said no." in text and "Nothing was shared." in text
    assert "You can close this page." in text
    assert svc.list_grants(acct.id) == []


def test_dont_allow_without_a_host_also_stays_here(app, svc, fake, monkeypatch):
    client, _ = _signed_in(app, svc, monkeypatch)
    fake.parked.pop("redirect_host", None)
    resp = client.post("/authorize/decide", data=json.dumps({
        "req": _handle(), "decision": "denied"}), content_type="application/json")
    assert resp.get_json()["redirect"] == "/authorize/declined"


# --- the hub says where a shared app's codes go ---------------------------------


def test_a_grant_records_the_redirect_host_and_the_hub_leads_with_it(
        app, svc, fake, monkeypatch):
    """The name is the client's claim; the hub, like the consent page, names
    the app by its address. MUTATION: drop redirect_host from add_grant -> red."""
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", MINT)
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    conn = _connect(svc, acct, "sample", "Sample records")
    fake.parked["redirect_host"] = "evil.example"
    with client.session_transaction() as s:
        s["wa_consent_challenge"] = "c"
    monkeypatch.setattr(svc, "finish_authentication",
                        lambda cred, ch, require_uv=False: acct)
    assert _approve(client, conn).status_code == 200
    g = svc.list_grants(acct.id)[0]
    assert g["redirect_host"] == "evil.example" and g["client_name"] == "Claude"
    page = client.get("/settings").get_data(as_text=True).replace("<wbr>", "")
    assert '<div class="hub-card-name grant-host">evil.example</div>' in page
    assert "calls itself &ldquo;Claude&rdquo;" in page or "calls itself “Claude”" in page
    assert '<div class="hub-card-name grant-host">Claude</div>' not in page


def test_a_grant_from_before_hosts_were_kept_still_shows_its_name(
        app, svc, monkeypatch):
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    svc.add_grant(acct.id, conn, tenant, "cid-claude", "Claude", "fhir.read",
                  "consent_old")
    assert svc.list_grants(acct.id)[0]["redirect_host"] is None
    page = client.get("/settings").get_data(as_text=True)
    # No host to lead with: the name alone, not "Claude / calls itself Claude".
    # MUTATION: show the calls-itself line whatever the host -> red.
    assert '<div class="hub-card-name grant-host">Claude</div>' in page
    assert "calls itself" not in page


def test_a_long_shared_app_host_wraps_at_dots_with_a_fallback(app, svc, monkeypatch):
    """The settings card names the app by its host, which can be one label
    wider than a 375px card. MUTATION: drop the <wbr> loop or the
    .grant-host wrap rule -> red."""
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    svc.add_grant(acct.id, conn, tenant, "cid-x", "Claude", "fhir.read",
                  "consent_long", redirect_host=f"a.{LONG_LABEL}")
    page = client.get("/settings").get_data(as_text=True)
    assert (f'<div class="hub-card-name grant-host">a.<wbr>{LONG_LABEL.split(".")[0]}'
            f'.<wbr>example</div>') in page
    import pathlib
    css = pathlib.Path("careagents/static/careagents.css").read_text()
    assert ".grant-host { overflow-wrap: break-word; }" in css
    assert ".grant-card { min-width: 0; }" in css
    # The badge sits above the host, in the flow, as on a record card: a long
    # host ran underneath the absolutely placed ACTIVE badge.
    assert ".grant-card .status { position: static; align-self: flex-start; order: -1; }" in css


def test_the_settings_card_escapes_the_host_between_its_line_breaks(
        app, svc, monkeypatch):
    """Only the <wbr> hints are markup. MUTATION: render a label |safe -> red."""
    client, acct = _signed_in(app, svc, monkeypatch)
    conn = _connect(svc, acct, "sample", "Sample records")
    tenant = svc.get_connection(acct.id, conn)["tenant_id"]
    svc.add_grant(acct.id, conn, tenant, "cid-x", "Claude", "fhir.read",
                  "consent_esc", redirect_host="evil<b>.example")
    page = client.get("/settings").get_data(as_text=True)
    assert "evil<b>" not in page
    assert '<div class="hub-card-name grant-host">evil&lt;b&gt;.<wbr>example</div>' in page


def test_an_existing_grants_table_gains_redirect_host_at_start(tmp_path):
    """create_all() adds tables, never columns, so a live ca_grants reaches
    this code without redirect_host. MUTATION: delete the ca_grants block
    from _ensure_columns -> red."""
    from sqlalchemy import create_engine, inspect, text
    from careagents.models import _ensure_columns
    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_grants (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), connection_id VARCHAR(32), "
            "tenant_id VARCHAR(64), client_id VARCHAR(64), "
            "client_name VARCHAR(120), scopes VARCHAR(255), "
            "consent_id VARCHAR(64), granted_at FLOAT, revoked_at FLOAT)"))
        conn.execute(text("INSERT INTO ca_grants (id, client_name) "
                          "VALUES ('g1', 'Claude')"))
    _ensure_columns(engine)
    assert "redirect_host" in {c["name"] for c in inspect(engine).get_columns("ca_grants")}
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT redirect_host FROM ca_grants WHERE id='g1'")).scalar() is None
    _ensure_columns(engine)  # idempotent: boot runs it every time
    engine.dispose()


# --- patient re-walk: wrapping, unusual letters, small copy -------------------

LONG_LABEL = "claudeaiconnectorauthorizationcallbackservicesecureverification.example"


def _consent_css_rule(selector):
    import pathlib
    import re as _re
    css = pathlib.Path("careagents/static/careagents.css").read_text()
    consent_css = css.split("--- consent page")[1].split("Leaving (#554)")[0]
    for m in _re.finditer(r"([^{}]+)\{([^}]*)\}", consent_css):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            return m.group(2)
    return ""


@pytest.mark.parametrize("host", [LONG_LABEL, "evil.example", "claude.ai"])
def test_every_occurrence_of_the_host_carries_the_wrap_fallback(
        app, svc, fake, monkeypatch, host):
    """A single label wider than 375px overflowed wherever the host was plain
    text (the caution and the stop line). Every occurrence now sits in the
    consent-host wrapper, and that wrapper breaks a too-long label.
    MUTATION: render any occurrence without host_html, or drop break-word
    from .consent-host -> red."""
    client, acct = _signed_in(app, svc, monkeypatch, passkey=True)
    _connect(svc, acct, "sample", "Sample records")
    fake.parked["redirect_host"] = host
    page = client.get("/authorize", query_string={"req": _handle()}).get_data(as_text=True)
    first_label = host.split(".")[0]
    starts = [i for i in range(len(page)) if page.startswith(first_label, i)]
    # Headline, "go back to", the stop line, and the caution when unrecognized.
    assert len(starts) == (3 if host == "claude.ai" else 4), starts
    for i in starts:
        assert page[:i].endswith(HOST_OPEN), page[max(0, i - 80):i + 40]
    assert "overflow-wrap: break-word" in _consent_css_rule(".consent-host")


@pytest.mark.parametrize("host, lookalike", [
    ("xn--clude-5ve.com", "claude.com"),          # clаude.com, Cyrillic a
    ("xn--laude-0ye.ai", "claude.ai"),            # сlaude.ai, Cyrillic c
    ("login.xn--80ak6aa92e.com", None),           # a Cyrillic label, no claude
])
def test_a_punycode_host_is_explained_in_plain_words(
        app, svc, fake, monkeypatch, host, lookalike):
    """xn--… reads as machine code. MUTATION: never set unusual_letters ->
    red; skip the decode in the lookalike check -> the claude rows go red."""
    page = " ".join(_ask_page(app, svc, fake, monkeypatch, host).split())
    assert "This address uses unusual letters that can imitate another address." in page
    if lookalike:
        assert f"This is not {lookalike}.</strong> It just has {lookalike} in it." in page
    else:
        assert "This is not" not in page


@pytest.mark.parametrize("host", ["evil.example", "claude.ai", "claude.ai.xn--evil.example"])
def test_unusual_letters_line_only_for_a_label_that_starts_with_xn(
        app, svc, fake, monkeypatch, host):
    page = _ask_page(app, svc, fake, monkeypatch, host)
    has = "unusual letters" in page
    assert has is any(label.startswith("xn--") for label in host.split("."))


def test_the_lookalike_line_names_the_recognized_address_it_borrows(
        app, svc, fake, monkeypatch):
    page = _ask_page(app, svc, fake, monkeypatch, "claude.com.evil.example")
    assert ("This is not claude.com.</strong> It just has claude.com in it."
            in " ".join(page.split()))
    assert "only uses the name" not in page


def test_the_declined_page_is_titled_you_said_no(app):
    page = app.test_client().get("/authorize/declined").get_data(as_text=True)
    assert "<title>You said no — CareAgents</title>" in page
    assert "Share your records?" not in page
