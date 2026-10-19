"""SEC941-PRE1: the gateway-wide command-center reads are operator-only.

GET /command-center/api/system and GET /command-center/api/openclaw/sessions
describe the whole OpenClaw gateway (its URL, and every chat session with its
Telegram/iMessage/WhatsApp peer handle), not one tenant. Before this fix any
tenant credential opened them, including a step-up token minted for free for
the public `desktop-demo` tenant. Now only `X-Internal-Secret` does
(r6/internal_auth.py, the same gate as /r6/ops, #304).

MUTATION: make `_require_operator` in r6/command_center/routes.py accept
`has_grant(scope=Scope.TENANT_BOUND, tenant=...)` as well -> every token test
here goes red. Make it accept `session.get(SESSION_KEY)` -> the session test
goes red.

Synthetic tenant ids, secrets and canaries only.
"""

from __future__ import annotations

import pytest

from r6.read_auth import TENANT_SESSION_KEY
from r6.stepup import generate_step_up_token

SECRET = "sec941-pre1-operator-secret"
PRIVATE_TENANT = "ca-sec941pre101"
CANARY_PEER = "CANARY-PEER-Ozymandias-Vex"
CANARY_URL = "http://canary-gateway.sec941-pre1.invalid:18789"

PATHS = ["/command-center/api/system", "/command-center/api/openclaw/sessions"]


class _Probe:
    def to_dict(self):
        return {"url": CANARY_URL, "up": True}


@pytest.fixture
def env(monkeypatch):
    from r6.command_center import gateway, projector
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "desktop-demo")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(gateway, "list_sessions",
                        lambda force=False: [{"peer": CANARY_PEER}])
    monkeypatch.setattr(gateway, "probe", lambda *a, **k: _Probe())
    monkeypatch.setattr(projector, "system_status",
                        lambda: {"gateway": {"url": CANARY_URL},
                                 "peers": [CANARY_PEER]})


def _assert_refused(resp):
    assert resp.status_code == 403, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    assert CANARY_PEER not in body
    assert CANARY_URL not in body
    assert resp.get_json() == {
        "error": "operator endpoint: X-Internal-Secret required"}


@pytest.mark.parametrize("path", PATHS)
def test_internal_secret_gets_200(app, env, path):
    resp = app.test_client().get(path, headers={"X-Internal-Secret": SECRET})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert CANARY_URL in body
    if path.endswith("/sessions"):
        assert CANARY_PEER in body


@pytest.mark.parametrize("path", PATHS)
def test_no_credential_gets_403(app, env, path):
    _assert_refused(app.test_client().get(path))


@pytest.mark.parametrize("path", PATHS)
def test_desktop_demo_stepup_token_gets_403(client, app, env, path):
    # Minted with no credential at all: desktop-demo is a public tenant.
    minted = client.post("/r6/fhir/internal/step-up-token",
                         json={"tenant_id": "desktop-demo"})
    assert minted.status_code == 200, minted.get_data(as_text=True)
    token = minted.get_json()["token"]
    c = app.test_client()
    _assert_refused(c.get(f"{path}?tenant=desktop-demo",
                          headers={"X-Step-Up-Token": token}))
    _assert_refused(c.get(path, headers={"X-Step-Up-Token": token}))


@pytest.mark.parametrize("source", ["query", "header"])
@pytest.mark.parametrize("path", PATHS)
def test_private_tenant_stepup_token_gets_403(app, env, path, source):
    c = app.test_client()
    for token in (generate_step_up_token(PRIVATE_TENANT),
                  generate_step_up_token(PRIVATE_TENANT, scope="read")):
        if source == "query":
            resp = c.get(f"{path}?tenant={PRIVATE_TENANT}",
                         headers={"X-Step-Up-Token": token})
        else:
            resp = c.get(path, headers={"X-Step-Up-Token": token,
                                        "X-Tenant-Id": PRIVATE_TENANT})
        _assert_refused(resp)


@pytest.mark.parametrize("path", PATHS)
def test_tenant_session_gets_403(app, env, path):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[TENANT_SESSION_KEY] = PRIVATE_TENANT
    _assert_refused(c.get(path))
    _assert_refused(c.get(f"{path}?tenant={PRIVATE_TENANT}"))


@pytest.mark.parametrize("presented", ["", " ", "not-the-operator-secret",
                                       SECRET + " "])
@pytest.mark.parametrize("path", PATHS)
def test_wrong_or_blank_secret_gets_403(app, env, path, presented):
    _assert_refused(app.test_client().get(
        path, headers={"X-Internal-Secret": presented}))
