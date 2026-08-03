"""
Security interim for #305 (finding S-2): the unauthenticated POST /fasten/demo
endpoint must persist NOTHING, while keeping its HTTP contract byte-identical.

The route is triggered on every real Stitch connection (r6-dashboard.js
onStitchComplete) and used to write a FastenConnection, a FastenJob, and four
R6Resource rows to a hardcoded `fasten-demo-tenant` — an unauthenticated write.
The frontend consumes only `data.steps`; the persisted rows were read back only
within the same request. So the persistence is removable without changing the
response.

Audit rows (append-only, PHI-free) are deliberately kept: step 5 claims
"N audit events recorded", so those events must really be recorded.

MUTATION: re-introduce the resource write in run_demo() — e.g. restore
`db.session.add(row)` + `db.session.commit()` for the four sample resources
(or a FastenConnection/FastenJob write). Then test_demo_persists_no_rows
reddens because a row for `fasten-demo-tenant` reappears. Verified 2026-08-03.
"""

from r6.fasten.models import FastenConnection, FastenJob
from r6.models import R6Resource

DEMO_TENANT = 'fasten-demo-tenant'

# Raw PHI that lives in the in-memory sample Patient and must never survive
# redaction into the step-4 payload.
RAW_PHI = ['DemoPatient', 'Jane', 'MRN-DEMO-001', '123 Main St', 'Springfield']


def _post_demo(client):
    resp = client.post('/fasten/demo')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_demo_response_shape_unchanged(client):
    """The steps contract the frontend reads stays identical."""
    body = _post_demo(client)

    assert body['demo'] == 'fasten_connect_e2e'
    assert body['org_connection_id']
    assert body['task_id']

    steps = body['steps']
    assert [s['step'] for s in steps] == [1, 2, 3, 4, 5]
    assert all(s['status'] == 'success' for s in steps)

    # Step 3: four resources "ingested"
    step3 = steps[2]
    assert len(step3['data']['ingested']) == 4
    assert step3['data']['resource_types'] == [
        'Patient', 'Observation', 'Condition', 'MedicationRequest'
    ]

    # Step 4: a redacted patient with no raw name / identifier / address
    step4 = steps[3]
    redacted = step4['data']['redacted_patient']
    assert redacted['resourceType'] == 'Patient'
    assert redacted['name'][0]['family'] == 'D.'
    assert redacted['name'][0]['given'] == ['J.']
    assert redacted['birthDate'] == '1985'
    import json as _json
    blob = _json.dumps(redacted)
    for raw in RAW_PHI:
        assert raw not in blob, f'raw PHI {raw!r} leaked into redacted patient'

    # Step 5: audit events really recorded
    step5 = steps[4]
    events = step5['data']['audit_events']
    assert len(events) >= 1
    assert all('event_type' in e for e in events)


def test_demo_persists_no_rows(client):
    """No connection / job / resource rows for fasten-demo-tenant afterward.

    Audit rows MAY exist (they are kept on purpose); the three write-side
    tables must be empty for the demo tenant.
    """
    _post_demo(client)

    conns = FastenConnection.query.filter_by(tenant_id=DEMO_TENANT).count()
    jobs = FastenJob.query.filter_by(tenant_id=DEMO_TENANT).count()
    resources = R6Resource.query.filter_by(tenant_id=DEMO_TENANT).count()

    assert conns == 0, f'{conns} FastenConnection rows persisted'
    assert jobs == 0, f'{jobs} FastenJob rows persisted'
    assert resources == 0, f'{resources} R6Resource rows persisted'

    # Sanity: the demo still worked (audit rows are allowed to exist).
    from r6.models import AuditEventRecord
    audits = AuditEventRecord.query.filter_by(tenant_id=DEMO_TENANT).count()
    assert audits >= 3, 'expected the three demo audit events to be recorded'
