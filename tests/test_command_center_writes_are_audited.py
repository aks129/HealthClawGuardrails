"""The command centre's writes must leave evidence, and it must be PHI-free.

The 08-05 pattern review's verdict on this blueprint was "under-guarded:
3 step-up-gated writes, 30 tenant filters, 0 audit events. Privilege without
evidence." `tests/test_write_guard_matrix.py` recorded the same fact in
passing, in a comment on NON_CLINICAL_MUTATORS: "NOTE: none of these emits an
AuditEvent."

Both are true statements about a gap that nothing was closing, which is how a
gap survives being known about. These tests close it.

The second half matters more than the first here. `api_conversations_create`
writes the text of a conversation turn, and CLAUDE.md treats chat transcripts
as PHI-adjacent. An audit trail that helpfully recorded the message would
duplicate the most sensitive field in the request into the one store whose
whole contract is that it holds no PHI.
"""

from __future__ import annotations

import json

import pytest

from models import db
from r6.models import AuditEventRecord

_TENANT = "cc-audit-tenant"
#: A distinctive string that must never reach the audit trail.
_SECRET_TEXT = "Quintavious Zzyzxbarton says his A1c is 8.1 and he is scared"


@pytest.fixture
def step_up(app):
    from r6.stepup import generate_step_up_token
    with app.app_context():
        return generate_step_up_token(_TENANT)


def _events(app, resource_type=None):
    with app.app_context():
        q = AuditEventRecord.query.filter_by(tenant_id=_TENANT)
        if resource_type:
            q = q.filter_by(resource_type=resource_type)
        return [
            {"action": e.event_type, "resource_type": e.resource_type,
             "resource_id": e.resource_id, "detail": e.detail}
            for e in q.all()
        ]


def _clear(app):
    with app.app_context():
        AuditEventRecord.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()


# --- conversations ----------------------------------------------------------

def test_logging_a_conversation_turn_is_audited(app, client, step_up):
    """MUTATION: delete the record_audit_event in api_conversations_create -> red."""
    _clear(app)
    resp = client.post("/command-center/api/conversations",
                       headers={"X-Step-Up-Token": step_up},
                       json={"tenant_id": _TENANT, "role": "user",
                             "text": _SECRET_TEXT, "agent_id": "a1"})
    assert resp.status_code == 201, resp.get_data(as_text=True)

    events = _events(app, "ConversationMessage")
    assert len(events) == 1, f"expected one audit event, got {events}"
    assert events[0]["action"] == "create"
    assert events[0]["resource_id"] == resp.get_json()["id"]


def test_the_conversation_audit_never_carries_the_message(app, client, step_up):
    """The one that would be a leak rather than a gap.

    `text` is the conversation turn. CLAUDE.md: audit `detail` stays
    PHI-free, and chat transcripts are PHI-adjacent.

    MUTATION: put `text` (or any slice of it) into the detail -> red.
    """
    _clear(app)
    client.post("/command-center/api/conversations",
                headers={"X-Step-Up-Token": step_up},
                json={"tenant_id": _TENANT, "role": "user",
                      "text": _SECRET_TEXT, "agent_id": "a1"})

    blob = json.dumps(_events(app))
    for token in ("Quintavious", "Zzyzxbarton", "A1c", "8.1", "scared"):
        assert token not in blob, (
            f"the audit trail carries {token!r} out of the message body")


def test_an_idempotent_replay_does_not_audit_a_second_write(app, client, step_up):
    """A replay returns the original message and writes nothing.

    An audit event for it would report a write that did not happen — the
    error-fidelity property, applied to the trail itself.

    MUTATION: move record_audit_event above the replay return -> red.
    """
    _clear(app)
    payload = {"tenant_id": _TENANT, "role": "user", "text": "hello",
               "agent_id": "a1", "request_id": "req-replay-1"}
    first = client.post("/command-center/api/conversations",
                        headers={"X-Step-Up-Token": step_up}, json=payload)
    second = client.post("/command-center/api/conversations",
                         headers={"X-Step-Up-Token": step_up}, json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["idempotent_replay"] is True
    assert len(_events(app, "ConversationMessage")) == 1


# --- tasks ------------------------------------------------------------------

def test_creating_and_updating_a_task_are_both_audited(app, client, step_up):
    """MUTATION: delete either record_audit_event in the task handlers -> red."""
    _clear(app)
    created = client.post("/command-center/api/tasks",
                          headers={"X-Step-Up-Token": step_up},
                          json={"tenant_id": _TENANT, "agent_id": "joe",
                                "title": "Check the potassium result"})
    assert created.status_code == 201, created.get_data(as_text=True)
    task_id = created.get_json()["id"]

    updated = client.patch(f"/command-center/api/tasks/{task_id}",
                           headers={"X-Step-Up-Token": step_up},
                           json={"status": "completed"})
    assert updated.status_code == 200, updated.get_data(as_text=True)

    actions = sorted(e["action"] for e in _events(app, "AgentTask"))
    assert actions == ["create", "update"], _events(app, "AgentTask")


def test_the_task_audit_never_carries_the_title(app, client, step_up):
    """A task title is caller-supplied free text and may name a condition.

    MUTATION: put `title` into the create detail -> red.
    """
    _clear(app)
    client.post("/command-center/api/tasks",
                headers={"X-Step-Up-Token": step_up},
                json={"tenant_id": _TENANT, "agent_id": "joe",
                      "title": _SECRET_TEXT})

    blob = json.dumps(_events(app, "AgentTask"))
    for token in ("Quintavious", "Zzyzxbarton", "A1c", "scared"):
        assert token not in blob, (
            f"the audit trail carries {token!r} out of a task title")


# --- the gap this file closes, stated as a property -------------------------

def test_a_refused_write_leaves_no_audit_event(app, client):
    """Evidence of a write that was refused would be worse than none.

    MUTATION: audit before the _authz_write check -> red.
    """
    _clear(app)
    resp = client.post("/command-center/api/tasks",
                       json={"tenant_id": _TENANT, "agent_id": "joe",
                             "title": "no credential"})
    assert resp.status_code in (401, 403), resp.get_data(as_text=True)
    assert _events(app) == []
