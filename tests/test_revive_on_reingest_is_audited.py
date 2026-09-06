"""Un-deleting a record on re-ingest leaves a trace (#558).

Both ingest paths flip is_deleted back to false when the same
(tenant, type, id) arrives again: r6/fasten/ingester.py always did, and
r6/context_builder.py does since #547. Neither said so in the audit trail,
so a record that was deleted and then restored looked identical to one
written once, and "who un-deleted this, and when" had no answer.

Each revive now records an 'update' AuditEvent whose detail names the
lifted tombstone; a never-deleted upsert does not; the row carries no
patient data. Same-transaction (add_audit_event), so the revive and its
trace commit or roll back together.

MUTATION: delete the add_audit_event call on either revive branch -> that
path's revive test goes red; the never-deleted and no-PHI tests stay green.
"""

import json

from models import db
from r6.models import AuditEventRecord, R6Resource

PATH = '/r6/fhir/Bundle/$ingest-context'
REVIVE = 'tombstone lifted on re-ingest'
SURNAME = 'Quuxleyburgh'


def _patient(rid):
    return {'resourceType': 'Patient', 'id': rid, 'gender': 'female',
            'name': [{'family': SURNAME, 'given': ['Zed']}]}


def _tombstone(app, tenant_id, resource_type, rid):
    with app.app_context():
        row = R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type=resource_type, id=rid).first()
        assert row is not None
        row.is_deleted = True
        db.session.commit()


def _revive_rows(app, tenant_id, rid):
    with app.app_context():
        return [r for r in AuditEventRecord.query.filter_by(
                    tenant_id=tenant_id, resource_id=rid).all()
                if r.detail and REVIVE in r.detail]


def _no_phi_in_audit(app, tenant_id):
    with app.app_context():
        for r in AuditEventRecord.query.filter_by(tenant_id=tenant_id).all():
            blob = ' '.join(str(v) for v in (r.detail, r.resource_id,
                                              r.agent_id, r.outcome_detail_code))
            assert SURNAME not in blob and 'Zed' not in blob


def _post(client, headers, rid):
    return client.post(PATH, data=json.dumps({
        'resourceType': 'Bundle', 'type': 'collection',
        'entry': [{'resource': _patient(rid)}]}),
        content_type='application/json', headers=headers)


# --- context builder path ------------------------------------------------

def test_context_builder_revive_is_audited(client, app, tenant_headers, tenant_id):
    rid = 'revive-cb-558'
    assert _post(client, tenant_headers, rid).status_code == 201
    assert _revive_rows(app, tenant_id, rid) == []
    _tombstone(app, tenant_id, 'Patient', rid)
    assert _post(client, tenant_headers, rid).status_code == 201
    rows = _revive_rows(app, tenant_id, rid)
    assert len(rows) == 1
    assert rows[0].event_type == 'update'
    assert rows[0].resource_type == 'Patient'
    with app.app_context():
        assert R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type='Patient', id=rid
        ).first().is_deleted is False
    _no_phi_in_audit(app, tenant_id)


def test_context_builder_never_deleted_upsert_is_not_called_a_revive(
        client, app, tenant_headers, tenant_id):
    rid = 'upsert-cb-558'
    assert _post(client, tenant_headers, rid).status_code == 201
    assert _post(client, tenant_headers, rid).status_code == 201
    assert _revive_rows(app, tenant_id, rid) == []


# --- Fasten ingester path ------------------------------------------------

def _ingest(app, tenant_id, rid):
    from r6.fasten.ingester import _ingest_one
    with app.app_context():
        result, got = _ingest_one(_patient(rid), tenant_id)
        db.session.commit()
        return result, got


def test_fasten_ingester_revive_is_audited(app, tenant_id):
    rid = 'revive-fi-558'
    assert _ingest(app, tenant_id, rid) == ('ok', rid)
    assert _revive_rows(app, tenant_id, rid) == []
    _tombstone(app, tenant_id, 'Patient', rid)
    assert _ingest(app, tenant_id, rid) == ('ok', rid)
    rows = _revive_rows(app, tenant_id, rid)
    assert len(rows) == 1
    assert rows[0].event_type == 'update'
    # The caller's provenance survives beside the revive note.
    assert 'Fasten' in rows[0].detail
    _no_phi_in_audit(app, tenant_id)


def test_fasten_ingester_never_deleted_upsert_is_not_called_a_revive(app, tenant_id):
    rid = 'upsert-fi-558'
    assert _ingest(app, tenant_id, rid) == ('ok', rid)
    assert _ingest(app, tenant_id, rid) == ('ok', rid)
    assert _revive_rows(app, tenant_id, rid) == []
