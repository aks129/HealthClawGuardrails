"""A Curatr fix, its Provenance and its audit rows commit together (#413 P1-A).

apply_fix committed the record and the Provenance, then audited each in a
separate post-commit savepoint: an audit that failed left a changed record
with no trail, and a result that could not be persisted invited a second
application. Now apply_fix owns one transaction — record, Provenance, both
mandatory audit rows — and the rail consults the committed Provenance that
names its action before it ever applies again.

Runs on SQLite and on the Postgres lane (.github/workflows/ci.yml).
"""
import json

import pytest

from models import db
from r6 import curatr as curatr_module
from r6.actions import errors
from r6.actions.models import ProposedAction
from r6.actions.registry import get_executor
from r6.curatr import apply_fix, find_fix_provenance
from r6.models import AuditEventRecord, R6Resource
from tests.approval_helpers import approval_headers

RID = "c-txn-1"
FIXES = [{"field_path": "Condition.clinicalStatus.coding[0].code",
          "new_value": "resolved"}]


def _record():
    return {"resourceType": "Condition", "id": RID,
            "subject": {"reference": "Patient/alice"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.fixture
def seeded(app, tenant_id, action_registry, monkeypatch):
    monkeypatch.setenv("CURATR_FIX_RAIL_ENABLED", "1")
    with app.app_context():
        db.session.add(R6Resource("Condition", json.dumps(_record()),
                                  resource_id=RID, tenant_id=tenant_id))
        db.session.commit()


def _state(app, tenant_id):
    """(record, version, provenance count, audit rows about this fix)."""
    with app.app_context():
        db.session.rollback()
        row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                         tenant_id=tenant_id).first()
        provs = R6Resource.query.filter_by(resource_type="Provenance",
                                           tenant_id=tenant_id).count()
        audits = [a for a in AuditEventRecord.query.filter_by(
            tenant_id=tenant_id).all()
            if (a.detail or "").startswith("curatr-")]
        return json.loads(row.resource_json), row.version_id, provs, audits


def _untouched(app, tenant_id):
    record, version, provs, audits = _state(app, tenant_id)
    assert record == _record()
    assert version == 1
    assert provs == 0
    assert audits == []


def test_success_commits_record_provenance_and_both_audits_together(
        app, tenant_id, seeded):
    with app.app_context():
        result = apply_fix("Condition", RID, FIXES, "test", tenant_id,
                           action_ref="act-1")
    assert "error" not in result
    record, version, provs, audits = _state(app, tenant_id)
    assert record["clinicalStatus"]["coding"][0]["code"] == "resolved"
    assert version == 2 and provs == 1
    kinds = sorted((a.event_type, a.resource_type) for a in audits)
    assert kinds == [("create", "Provenance"), ("update", "Condition")]
    with app.app_context():
        assert find_fix_provenance(tenant_id, "act-1")["id"] == result["provenance"]["id"]
        assert find_fix_provenance(tenant_id, "act-2") is None


def test_an_audit_that_cannot_be_written_takes_the_fix_down_with_it(
        app, tenant_id, seeded, monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("audit store down")
    monkeypatch.setattr("r6.audit.add_audit_event", refuse)
    with app.app_context():
        with pytest.raises(RuntimeError) as exc:
            apply_fix("Condition", RID, FIXES, "test", tenant_id)
    assert "audit store down" not in str(exc.value)   # class only
    _untouched(app, tenant_id)


def test_a_commit_that_fails_leaves_no_partial_change(
        app, tenant_id, seeded, monkeypatch):
    real_commit = db.session.commit
    calls = {"n": 0}

    def fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection lost at commit")
        return real_commit()
    with app.app_context():
        monkeypatch.setattr(db.session, "commit", fail_once)
        with pytest.raises(RuntimeError):
            apply_fix("Condition", RID, FIXES, "test", tenant_id)
        monkeypatch.setattr(db.session, "commit", real_commit)
    _untouched(app, tenant_id)


def test_the_rail_reports_a_failed_transaction_truthfully_with_no_mutation(
        client, app, tenant_headers, auth_headers, tenant_id, seeded, monkeypatch):
    action_id = _staged(client, tenant_headers, auth_headers)

    def refuse(*a, **k):
        raise RuntimeError("audit store down")
    monkeypatch.setattr("r6.audit.add_audit_event", refuse)
    r = client.post("/r6/actions/%s/confirm" % action_id, json={},
                    headers=approval_headers(app, auth_headers, action_id))
    assert r.status_code == 502
    assert r.get_json()["error_code"] == errors.PROVIDER_ERROR
    _untouched(app, tenant_id)
    with app.app_context():
        assert ProposedAction.query.get(action_id).status == "failed"


def test_a_lost_result_after_the_commit_is_recovered_not_reapplied(
        client, app, tenant_headers, auth_headers, tenant_id, seeded, monkeypatch):
    """The commit went through; persisting the action's outcome did not. The
    action row still says executing. Both the reaper's reconcile() and a
    repeated execute() must find the Provenance and report completed —
    and the record must have changed exactly once."""
    action_id = _staged(client, tenant_headers, auth_headers)
    import r6.actions.routes as routes_module
    real = routes_module.transition_action

    def lose_the_result(*a, **k):
        if k.get("to_state") == "completed":
            raise RuntimeError("result store unavailable")
        return real(*a, **k)
    monkeypatch.setattr(routes_module, "transition_action", lose_the_result)
    with pytest.raises(RuntimeError):
        client.post("/r6/actions/%s/confirm" % action_id, json={},
                    headers=approval_headers(app, auth_headers, action_id))
    monkeypatch.setattr(routes_module, "transition_action", real)

    record, version, provs, _ = _state(app, tenant_id)
    assert version == 2 and provs == 1
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        assert action.status == "executing"          # the lost result
        ex = get_executor("curatr-fix")
        again = ex.reconcile(action)
        assert again.status == "completed"
        assert again.outcome["recovered"] is True
        assert again.outcome["fields"] == ["Condition.clinicalStatus.coding[0].code"]
        rerun = ex.execute(action)
        assert rerun.status == "completed"
        assert rerun.outcome["recovered"] is True
        assert rerun.provider_ref == again.provider_ref
    record, version, provs, _ = _state(app, tenant_id)
    assert version == 2 and provs == 1               # applied exactly once


def test_reconcile_with_nothing_committed_never_applies(
        client, app, tenant_headers, auth_headers, tenant_id, seeded):
    action_id = _staged(client, tenant_headers, auth_headers)
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        verdict = get_executor("curatr-fix").reconcile(action)
    assert verdict.status == "needs_review"
    _untouched(app, tenant_id)


def test_the_finder_matches_the_whole_action_id_not_a_prefix(app, tenant_id, seeded):
    with app.app_context():
        apply_fix("Condition", RID, FIXES, "test", tenant_id, action_ref="act-10")
        assert find_fix_provenance(tenant_id, "act-1") is None
        assert find_fix_provenance(tenant_id, "act-10") is not None
        assert find_fix_provenance("other-tenant", "act-10") is None


def _staged(client, tenant_headers, auth_headers):
    body = {"kind": "curatr-fix",
            "payload": {"to": "Condition/%s" % RID,
                        "body": "Mark this condition as resolved.",
                        "curatr_fix": {"resource_type": "Condition",
                                       "resource_id": RID, "record_version": 1,
                                       "patient_intent": "it cleared up",
                                       "fixes": FIXES}}}
    r = client.post("/r6/actions/propose", json=body, headers=tenant_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    action_id = r.get_json()["id"]
    c = client.post("/r6/actions/%s/commit" % action_id, headers=auth_headers)
    assert c.status_code == 202
    return action_id


def test_module_uses_the_in_transaction_audit_primitive():
    src = open(curatr_module.__file__).read()
    assert "record_audit_event" not in src
