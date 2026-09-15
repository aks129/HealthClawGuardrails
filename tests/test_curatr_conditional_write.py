"""Exactly one authorised correction can win a record version (#413 P2).

The application-level version check (P1) compares on the loaded row; it says
nothing about a writer that commits between that load and our commit. The
write is now a database-enforced conditional UPDATE on
tenant/type/id/version: whoever commits first advances the version, and the
other statement matches no row and abandons its transaction — no mutation,
no Provenance, no audit, a `stale` answer.

The race tests need two independent database transactions, so they run only
where the test database is Postgres (the CI Postgres lane; locally with
SQLALCHEMY_DATABASE_URI set). SQLite's single shared connection cannot host
two writers. The non-racing checks run everywhere.
"""
import json
import os
import threading

import pytest

from models import db
from r6 import curatr as curatr_module
from r6.curatr import apply_fix
from r6.models import AuditEventRecord, R6Resource

RID = "c-race-1"
FIX_A = [{"field_path": "Condition.clinicalStatus.coding[0].code",
          "new_value": "resolved"}]
FIX_B = [{"field_path": "Condition.clinicalStatus.coding[0].code",
          "new_value": "inactive"}]

postgres_only = pytest.mark.skipif(
    not os.environ.get("SQLALCHEMY_DATABASE_URI", "").startswith("postgresql"),
    reason="two independent transactions need Postgres (CI postgres lane)")


def _record():
    return {"resourceType": "Condition", "id": RID,
            "subject": {"reference": "Patient/alice"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.fixture
def seeded(app, tenant_id):
    with app.app_context():
        db.session.add(R6Resource("Condition", json.dumps(_record()),
                                  resource_id=RID, tenant_id=tenant_id))
        db.session.commit()


def _state(app, tenant_id):
    with app.app_context():
        db.session.rollback()
        row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                         tenant_id=tenant_id).first()
        provs = R6Resource.query.filter_by(resource_type="Provenance",
                                           tenant_id=tenant_id).count()
        audits = AuditEventRecord.query.filter_by(tenant_id=tenant_id).filter(
            AuditEventRecord.detail.like("curatr-%")).count()
        return json.loads(row.resource_json), row.version_id, provs, audits


def _in_thread(app, fn):
    """Run fn inside its own app context — its own scoped session and its own
    database connection — and hand back its result or exception."""
    box = {}

    def run():
        with app.app_context():
            try:
                box["result"] = fn()
            except Exception as exc:  # noqa: BLE001 — surfaced to the test
                box["error"] = exc
            finally:
                db.session.remove()
    t = threading.Thread(target=run)
    t.start()
    return t, box


@postgres_only
def test_two_approved_corrections_against_one_version_exactly_one_wins(
        app, tenant_id, seeded, monkeypatch):
    """Both writers load version 1 and plan before either writes."""
    both_loaded = threading.Barrier(2, timeout=15)
    monkeypatch.setattr(curatr_module, "_between_load_and_write",
                        lambda: both_loaded.wait())

    ta, a = _in_thread(app, lambda: apply_fix("Condition", RID, FIX_A, "a",
                                              tenant_id, action_ref="act-a"))
    tb, b = _in_thread(app, lambda: apply_fix("Condition", RID, FIX_B, "b",
                                              tenant_id, action_ref="act-b"))
    ta.join(30)
    tb.join(30)
    assert "error" not in a and "error" not in b, (a, b)
    results = [a["result"], b["result"]]
    winners = [r for r in results if "error" not in r]
    losers = [r for r in results if r.get("stale")]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0]["current_version"] == 2

    record, version, provs, audits = _state(app, tenant_id)
    assert version == 2
    assert provs == 1
    assert audits == 2                        # the winner's two rows only
    won = record["clinicalStatus"]["coding"][0]["code"]
    assert won in ("resolved", "inactive")
    assert winners[0]["change_summary"].startswith("Condition.clinicalStatus")


@postgres_only
def test_an_ordinary_update_in_the_gap_makes_the_correction_stale(
        app, tenant_id, seeded, monkeypatch):
    """Not a competing correction: a plain resource update (the FHIR PUT
    path uses update_resource) that commits after our load and before our
    write. Our conditional write finds version 2 and matches nothing."""
    def ordinary_update():
        row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                         tenant_id=tenant_id).first()
        moved = json.loads(row.resource_json)
        moved["clinicalStatus"] = {"coding": [{"code": "remission"}]}
        row.update_resource(json.dumps(moved, separators=(",", ":"),
                                       sort_keys=True))
        db.session.commit()
        return row.version_id

    def in_the_gap():
        t, box = _in_thread(app, ordinary_update)
        t.join(30)
        assert box.get("result") == 2, box
    monkeypatch.setattr(curatr_module, "_between_load_and_write", in_the_gap)

    with app.app_context():
        result = apply_fix("Condition", RID, FIX_A, "a", tenant_id,
                           action_ref="act-a")
    assert result.get("stale") is True, result
    assert result["current_version"] == 2
    record, version, provs, audits = _state(app, tenant_id)
    assert record["clinicalStatus"]["coding"][0]["code"] == "remission"
    assert version == 2 and provs == 0 and audits == 0


def test_the_write_is_conditional_on_the_loaded_version(app, tenant_id, seeded,
                                                        monkeypatch):
    """Runs everywhere: the version the row carries when the UPDATE runs is
    not the one that was loaded, so the statement matches no row."""
    def bump_underneath():
        # Same session, so this is not a race — it is the predicate itself.
        R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                   tenant_id=tenant_id).update(
            {"version_id": R6Resource.version_id + 1},
            synchronize_session=False)
    monkeypatch.setattr(curatr_module, "_between_load_and_write", bump_underneath)
    with app.app_context():
        result = apply_fix("Condition", RID, FIX_A, "a", tenant_id)
    assert result.get("stale") is True
    record, version, provs, audits = _state(app, tenant_id)
    assert record == _record()
    assert provs == 0 and audits == 0


def test_a_winning_write_advances_the_version_the_way_update_resource_does(
        app, tenant_id, seeded):
    with app.app_context():
        result = apply_fix("Condition", RID, FIX_A, "a", tenant_id)
        assert "error" not in result
        row = R6Resource.query.filter_by(resource_type="Condition", id=RID,
                                         tenant_id=tenant_id).first()
        import hashlib
        assert row.version_id == 2
        assert row.sha256 == hashlib.sha256(
            row.resource_json.encode("utf-8")).hexdigest()
        assert result["updated_resource"]["meta"]["versionId"] == "2"
        assert result["updated_resource"]["clinicalStatus"]["coding"][0]["code"] == "resolved"
