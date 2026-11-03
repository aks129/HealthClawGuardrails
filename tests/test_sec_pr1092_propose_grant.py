"""Security tester's exploit tests for PR #1092 (#1001: propose grant, `to` label).

Passing tests pin guards the PR relies on that its own tests did not
exercise. `xfail(strict=True)` rows pin a finding: each goes red the day it
is fixed, which is the signal to drop the marker.

Synthetic data only: 555-01xx numbers are fictional, names are canaries.
"""

from __future__ import annotations

import json

import pytest

from models import db
from r6.actions.models import ProposedAction
from r6.fasten.models import TenantClosure
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token

CANARY = "Zelda Canaryfield"
THIRD_PARTY = "+15555550199"


def _sms(to=CANARY + " 555-0142"):
    return {"kind": "sms", "payload": {"to": to, "phone": "+15555550142",
                                       "body": "synthetic reminder"}}


def _with(tenant_headers, token):
    return dict(tenant_headers, **{"X-Step-Up-Token": token})


def _rows(tenant):
    return ProposedAction.query.filter_by(tenant_id=tenant).count()


def _audit_details(tenant):
    return [r.detail or "" for r in AuditEventRecord.query.filter_by(
        tenant_id=tenant).all()]


# --- the gate: every refusal shape stages nothing ---------------------------

@pytest.mark.parametrize("make_headers", [
    pytest.param(lambda t: {}, id="absent"),
    pytest.param(lambda t: {"X-Step-Up-Token": "not-a-token"}, id="garbage"),
    pytest.param(lambda t: {"X-Step-Up-Token": generate_step_up_token(
        t, ttl_seconds=-5)}, id="expired"),
    pytest.param(lambda t: {"X-Step-Up-Token": generate_step_up_token(
        "sec1092-other")}, id="other-tenant"),
    pytest.param(lambda t: {"X-Step-Up-Token": generate_step_up_token(
        t, scope="read")}, id="read-scope"),
    pytest.param(lambda t: {"X-Step-Up-Token": generate_step_up_token(
        t, scope="admin")}, id="unknown-scope"),
    pytest.param(lambda t: {"X-Step-Up-Token": generate_step_up_token(
        t, audience="action-approval")}, id="approval-audience"),
    pytest.param(lambda t: {"Authorization": "Bearer "
                            + generate_step_up_token(t)}, id="bearer-only"),
])
def test_propose_refuses_every_bad_grant_and_stages_nothing(
        app, client, tenant_headers, make_headers):
    tenant = tenant_headers["X-Tenant-Id"]
    headers = dict(tenant_headers, **make_headers(tenant))
    r = client.post("/r6/actions/propose", json=_sms(), headers=headers)
    assert r.status_code == 401, r.get_data(as_text=True)
    with app.app_context():
        assert _rows(tenant) == 0
        details = _audit_details(tenant)
    assert details, "a refusal must leave an audit row"
    assert all(CANARY not in d and "555-01" not in d for d in details)


def test_a_token_in_the_body_is_not_a_grant(app, client, tenant_headers):
    tenant = tenant_headers["X-Tenant-Id"]
    body = dict(_sms(), step_up_token=generate_step_up_token(tenant))
    r = client.post("/r6/actions/propose", json=body, headers=tenant_headers)
    assert r.status_code == 401
    with app.app_context():
        assert _rows(tenant) == 0


@pytest.mark.parametrize("raw", [
    pytest.param(("[" * 5000 + "]" * 5000).encode(), id="deep-nest"),
    pytest.param(b"{{{", id="not-json"),
    pytest.param(b"[1, 2]", id="array"),
    pytest.param(json.dumps({"kind": "sms", "payload": {
        "to": "x", "body": "crushing chest pain"}}).encode(), id="emergency"),
])
def test_the_gate_runs_before_the_body_is_read(app, client, tenant_headers,
                                               raw):
    """No token: 401 whatever the body, and the emergency screen (which
    writes its own audit row) never runs for an anonymous caller."""
    tenant = tenant_headers["X-Tenant-Id"]
    r = client.post("/r6/actions/propose", data=raw,
                    content_type="application/json", headers=tenant_headers)
    assert r.status_code == 401
    with app.app_context():
        assert _rows(tenant) == 0
        assert not [d for d in _audit_details(tenant)
                    if d.startswith("emergency_indicated")]


# --- the allow-list: confusables fall back ----------------------------------

@pytest.mark.parametrize("to", [
    "your phone",            # no-break space
    "your phone​",           # zero-width space
    "yоur phone",            # Cyrillic o
    "ｙｏｕｒ phone",  # fullwidth
    "‮enohp ruoy",           # right-to-left override
    "your phóne",           # combining accent
    "your phone\u0000",           # NUL suffix
    '"your phone"',               # quoted
])
def test_a_lookalike_of_the_safe_label_falls_back(app, client, auth_headers,
                                                  to):
    r = client.post("/r6/actions/propose", json=_sms(to=to),
                    headers=auth_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    with app.app_context():
        action = db.session.get(ProposedAction, r.get_json()["id"])
        assert action.summary()["to"] == "a phone number"


# --- findings ----------------------------------------------------------------

# #1092 F1, fixed: SAFE_TO_LABELS is scoped by kind, so only an sms may be
# labelled 'your phone'.
@pytest.mark.parametrize("kind,extra", [
    ("phone-call", {"phone": THIRD_PARTY}),
    ("insurance-call", {"phone": THIRD_PARTY}),
    ("webhook-poster", {"url": "https://example.invalid/hook"}),
])
def test_your_phone_is_not_believed_for_a_kind_that_is_not_a_text(
        app, client, auth_headers, monkeypatch, kind, extra):
    pushed = []
    monkeypatch.setattr("r6.actions.routes.notify_tenant",
                        lambda tenant, message, *a, **k: pushed.append(message))
    tenant = auth_headers["X-Tenant-Id"]
    body = {"kind": kind,
            "payload": dict(extra, to="your phone", body="synthetic script")}
    r = client.post("/r6/actions/propose", json=body, headers=auth_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    aid = r.get_json()["id"]
    assert client.post("/r6/actions/%s/commit" % aid,
                       headers=auth_headers).status_code == 202
    with app.app_context():
        detail = AuditEventRecord.query.filter_by(
            tenant_id=tenant, resource_id=aid).first().detail
    # The push the person reads first, then the audit row.
    assert pushed and all("your phone" not in m for m in pushed), pushed
    assert json.loads(detail)["to"] != "your phone"


# #1092 F2, fixed: propose calls require_open_tenant after its grant.
def test_propose_refuses_a_closed_tenant(app, client, auth_headers):
    tenant = auth_headers["X-Tenant-Id"]
    with app.app_context():
        db.session.add(TenantClosure(tenant_id=tenant))
        db.session.commit()
    r = client.post("/r6/actions/propose", json=_sms(), headers=auth_headers)
    assert r.status_code == 409, r.status_code
    with app.app_context():
        assert _rows(tenant) == 0
