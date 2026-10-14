"""Security probes for the ON CONFLICT conversation-create path (#923).

Attacks:
- Two tenants race the SAME conversation id. Each must get its own row and
  its own message; neither may attach to the other's thread (V6).
- A token for tenant B naming tenant A is refused before any row is made.
- A forced audit failure on a first turn leaves no conversation row and no
  message behind (V2: the INSERT now runs eagerly, before the audit).
- A winner that rolls back (reply_to outside the thread) does not strand the
  loser: the loser creates the row itself, no 500.
- Every 201 leaves exactly one PHI-free audit row in its own tenant.

Runs on a file SQLite by default, on Postgres when SQLALCHEMY_DATABASE_URI
points there. Synthetic data only.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

_ROUTE = "/command-center/api/conversations"
_CANARY = "Zebulon Quartermaine-Canary"


@pytest.fixture
def race_app(tmp_path):
    from main import create_app
    from models import db

    uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite"):
        uri = f"sqlite:///{tmp_path / 'race-sec.db'}"
    flask_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": uri,
        "LEGACY_BOOT_ON_CREATE": False,
    })
    with flask_app.app_context():
        db.create_all()
        from r6.rate_limit import _rate_limits
        _rate_limits.clear()
        yield flask_app
        db.session.remove()
        db.engine.dispose()


def _barrier_hook(engine, parties):
    from sqlalchemy import event

    barrier = threading.Barrier(parties)
    arrived = []

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO CC_CONVERSATIONS"):
            arrived.append(threading.get_ident())
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass

    event.listen(engine, "before_cursor_execute", _before)
    return _before, arrived


def _race(app, bodies_and_headers):
    results = {}
    lock = threading.Lock()

    def turn(key, headers, body):
        try:
            r = app.test_client().post(_ROUTE, headers=headers, json=body)
            out = (r.status_code, r.get_json())
        except Exception as exc:  # noqa: BLE001 - TESTING re-raises a 500
            out = (exc, None)
        with lock:
            results[key] = out

    threads = [threading.Thread(target=turn, args=(k, h, b))
               for k, (h, b) in bodies_and_headers.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_two_tenants_racing_one_conversation_id_stay_apart(race_app):
    from sqlalchemy import event

    from models import db
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    tenant_a = f"sec-a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"sec-b-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:shared-{uuid.uuid4().hex[:12]}"
    hook, arrived = _barrier_hook(db.engine, 2)

    def body(tenant, agent):
        return {"tenant_id": tenant, "conversation_id": conv,
                "agent_id": agent, "channel": "web", "role": "user",
                "text": f"synthetic {tenant}"}

    try:
        results = _race(race_app, {
            "a": ({"X-Step-Up-Token": generate_step_up_token(tenant_a)},
                  body(tenant_a, "agent-a")),
            "b": ({"X-Step-Up-Token": generate_step_up_token(tenant_b)},
                  body(tenant_b, "agent-b")),
        })
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)

    assert len(arrived) == 2, arrived
    assert results["a"][0] == 201 and results["b"][0] == 201, results
    assert results["a"][1]["tenant_id"] == tenant_a
    assert results["b"][1]["tenant_id"] == tenant_b

    db.session.expire_all()
    for tenant, agent in ((tenant_a, "agent-a"), (tenant_b, "agent-b")):
        rows = Conversation.query.filter_by(tenant_id=tenant, id=conv).all()
        assert [r.agent_id for r in rows] == [agent]
        msgs = ConversationMessage.query.filter_by(
            tenant_id=tenant, conversation_id=conv).all()
        assert [m.text for m in msgs] == [f"synthetic {tenant}"]


def test_foreign_token_cannot_create_in_victim_tenant(race_app):
    from models import db
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    victim = f"sec-victim-{uuid.uuid4().hex[:8]}"
    attacker = f"sec-attacker-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:hijack-{uuid.uuid4().hex[:12]}"
    client = race_app.test_client()
    headers = {"X-Step-Up-Token": generate_step_up_token(attacker)}

    # Body names the victim.
    r = client.post(_ROUTE, headers=headers, json={
        "tenant_id": victim, "conversation_id": conv, "agent_id": "x",
        "channel": "web", "role": "user", "text": "synthetic"})
    assert r.status_code == 401
    # Header names the victim, body omits tenant.
    r = client.post(_ROUTE, headers={**headers, "X-Tenant-Id": victim}, json={
        "conversation_id": conv, "agent_id": "x",
        "channel": "web", "role": "user", "text": "synthetic"})
    assert r.status_code == 401

    db.session.expire_all()
    assert Conversation.query.filter_by(tenant_id=victim).count() == 0
    assert ConversationMessage.query.filter_by(tenant_id=victim).count() == 0


def test_audit_failure_on_first_turn_leaves_no_conversation(race_app,
                                                            monkeypatch):
    from models import db
    from r6.command_center import routes
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    tenant = f"sec-audit-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:auditfail-{uuid.uuid4().hex[:12]}"

    class ForcedAuditFailure(RuntimeError):
        pass

    def boom(*a, **k):
        raise ForcedAuditFailure("synthetic audit outage")

    monkeypatch.setattr(routes, "add_audit_event", boom)
    client = race_app.test_client()
    with pytest.raises(ForcedAuditFailure):
        client.post(_ROUTE,
                    headers={"X-Step-Up-Token": generate_step_up_token(tenant)},
                    json={"tenant_id": tenant, "conversation_id": conv,
                          "agent_id": "a", "channel": "web", "role": "user",
                          "text": "synthetic"})

    # Read on a fresh connection: nothing may have committed.
    db.session.remove()
    assert Conversation.query.filter_by(tenant_id=tenant, id=conv).count() == 0
    assert ConversationMessage.query.filter_by(tenant_id=tenant).count() == 0


def test_rolled_back_winner_does_not_strand_the_loser(race_app):
    """Winner inserts, then fails reply_to (400) and rolls back. On Postgres
    the loser's ON CONFLICT waited on the winner and must now insert itself;
    `.one()` must not raise NoResultFound."""
    from sqlalchemy import event

    from models import db
    from r6.command_center.models import Conversation
    from r6.stepup import generate_step_up_token

    tenant = f"sec-rb-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:rb-{uuid.uuid4().hex[:12]}"
    headers = {"X-Step-Up-Token": generate_step_up_token(tenant)}
    hook, arrived = _barrier_hook(db.engine, 2)
    base = {"tenant_id": tenant, "conversation_id": conv, "agent_id": "a",
            "channel": "web", "role": "user", "text": "synthetic"}
    try:
        results = _race(race_app, {
            "bad": (headers, {**base, "reply_to": "no-such-message"}),
            "good": (headers, base),
        })
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)

    assert len(arrived) == 2, arrived
    assert results["bad"][0] == 400, results
    assert results["good"][0] == 201, results
    db.session.expire_all()
    assert Conversation.query.filter_by(tenant_id=tenant, id=conv).count() == 1


def test_each_raced_turn_is_audited_without_phi(race_app):
    from sqlalchemy import event

    from models import db
    from r6.command_center.models import ConversationMessage
    from r6.models import AuditEventRecord
    from r6.stepup import generate_step_up_token

    tenant = f"sec-aud-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:aud-{uuid.uuid4().hex[:12]}"
    headers = {"X-Step-Up-Token": generate_step_up_token(tenant)}
    hook, arrived = _barrier_hook(db.engine, 3)
    try:
        results = _race(race_app, {
            n: (headers, {"tenant_id": tenant, "conversation_id": conv,
                          "agent_id": "a", "channel": "web", "role": "user",
                          "request_id": f"aud-{n}",
                          "text": f"{_CANARY} turn {n}"})
            for n in range(3)
        })
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)

    assert len(arrived) == 3, arrived
    assert [results[n][0] for n in range(3)] == [201] * 3, results
    db.session.expire_all()
    msg_ids = {m.id for m in ConversationMessage.query.filter_by(
        tenant_id=tenant, conversation_id=conv)}
    audits = AuditEventRecord.query.filter_by(
        tenant_id=tenant, resource_type="ConversationMessage").all()
    assert {a.resource_id for a in audits} == msg_ids
    assert len(audits) == 3
    for a in audits:
        assert _CANARY not in (a.detail or "")
        assert "turn" not in (a.detail or "")
    for n in range(3):
        assert _CANARY not in str({k: v for k, v in results[n][1].items()
                                   if k != "text"})


def test_existing_thread_in_other_tenant_does_not_shadow_new_one(race_app):
    """Tenant A already owns conversation X for agent-a. Tenant B's first
    turn on X for agent-b must create B's own row (201), not read A's row
    back and answer 409, and must not touch A's thread."""
    from models import db
    from r6.command_center.models import Conversation, ConversationMessage
    from r6.stepup import generate_step_up_token

    tenant_a = f"sec-sa-{uuid.uuid4().hex[:8]}"
    tenant_b = f"sec-sb-{uuid.uuid4().hex[:8]}"
    conv = f"careagents:shadow-{uuid.uuid4().hex[:12]}"
    client = race_app.test_client()

    def post(tenant, agent):
        return client.post(
            _ROUTE, headers={"X-Step-Up-Token": generate_step_up_token(tenant)},
            json={"tenant_id": tenant, "conversation_id": conv,
                  "agent_id": agent, "channel": "web", "role": "user",
                  "text": f"synthetic {tenant}"})

    assert post(tenant_a, "agent-a").status_code == 201
    db.session.expire_all()
    a_before = Conversation.query.filter_by(tenant_id=tenant_a, id=conv).one()
    a_updated = a_before.updated_at

    r = post(tenant_b, "agent-b")
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["tenant_id"] == tenant_b

    db.session.expire_all()
    a_after = Conversation.query.filter_by(tenant_id=tenant_a, id=conv).one()
    assert a_after.agent_id == "agent-a"
    assert a_after.updated_at == a_updated
    assert ConversationMessage.query.filter_by(
        tenant_id=tenant_a, conversation_id=conv).count() == 1
    assert Conversation.query.filter_by(
        tenant_id=tenant_b, id=conv).one().agent_id == "agent-b"
