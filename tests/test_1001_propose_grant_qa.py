"""QA on #1001 / PR #1092: the approval push carries no raw `to`.

The PR's tests and the security tester's check audit detail and the list
summaries. Commit builds the push a patient receives ("Approval needed")
as its own string from summary()['to'], and no passing test read that string.

Synthetic data only: 555-01xx numbers are fictional.
"""

import json

RAW_TO = 'Jane Doe 617-555-0142'
BODY = {'kind': 'phone-call',
        'payload': {'to': RAW_TO, 'phone': '+16175550142',
                    'body': 'Calling about a refill.'}}


def test_the_approval_push_names_the_kind_label_not_the_raw_to(
        client, tenant_headers, monkeypatch):
    """MUTATION: in commit_action, build `label` from
    action.payload.get('to') instead of action.summary().get('to'). This
    goes red; the rest of the suite stays green (checked 2026-11-03)."""
    from r6.stepup import generate_step_up_token
    sent = []
    monkeypatch.setattr('r6.actions.routes.notify_tenant',
                        lambda tenant, message, **k: sent.append(message) or 1)
    tenant = tenant_headers['X-Tenant-Id']
    h = dict(tenant_headers, **{
        'X-Step-Up-Token': generate_step_up_token(tenant)})
    resp = client.post('/r6/actions/propose', json=BODY, headers=h)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    action_id = resp.get_json()['id']
    resp = client.post(f'/r6/actions/{action_id}/commit', headers=h)
    assert resp.status_code == 202, resp.get_data(as_text=True)

    assert sent, 'commit sent no approval push'
    blob = json.dumps(sent)
    assert 'Jane' not in blob and '555-0142' not in blob, sent
    assert any('to a phone number' in m for m in sent), sent
