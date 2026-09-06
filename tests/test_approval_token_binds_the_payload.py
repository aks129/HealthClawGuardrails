"""The approval credential binds the action AND the bytes the person was
shown (#659, the push path's window left by #658).

On the Telegram/dashboard path nothing is minted until the confirm request
itself, so #658's confirmation digest was taken at tap time. The approval
token, minted when the surface is shown, now carries the keyed digest of
the payload as it stood then, inside the operation it is bound to; the
confirm route recomputes the operation from the payload about to execute.
A swap between the push and the tap fails the token's own operation check
before any claim, nonce or execution, and the action stays where it was.

MUTATION: r6/actions/routes.py, mint the approval token with
operation=action_id (unbound) -> the swap test goes red (the tap proceeds).
"""

import json

from models import db
from r6.actions.models import ProposedAction

from tests.test_actions_routes import PROPOSE_BODY, _propose

SECRET = 'mint-secret-659'


def _mint(client, tenant_headers, action_id, monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', SECRET)
    headers = dict(tenant_headers)
    headers['X-Internal-Secret'] = SECRET
    resp = client.post('/r6/actions/%s/approval-token' % action_id, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['token']


def _commit(client, auth_headers, action_id):
    resp = client.post('/r6/actions/%s/commit' % action_id, headers=auth_headers)
    assert resp.status_code == 202, resp.get_json()


def _confirm(client, tenant_headers, action_id, token):
    headers = dict(tenant_headers)
    headers['X-Step-Up-Token'] = token
    return client.post('/r6/actions/%s/confirm' % action_id, headers=headers, json={})


def test_a_payload_swapped_between_the_push_and_the_tap_fails_the_token(
        client, app, tenant_headers, auth_headers, tenant_id, monkeypatch):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    token = _mint(client, tenant_headers, action_id, monkeypatch)   # the push
    with app.app_context():
        forged = json.dumps(dict(PROPOSE_BODY['payload'], phone='617-555-0199'))
        ProposedAction.query.filter_by(id=action_id).update(
            {'payload_json': forged}, synchronize_session=False)
        db.session.commit()
    resp = _confirm(client, tenant_headers, action_id, token)          # the tap
    assert resp.status_code == 401
    body = resp.get_data(as_text=True)
    assert 'operation mismatch' in body.lower()
    assert '617-555-0199' not in body
    with app.app_context():
        assert ProposedAction.query.get(action_id).status == 'awaiting_confirmation'


def test_an_unchanged_payload_taps_through(client, app, tenant_headers, auth_headers, monkeypatch):
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    token = _mint(client, tenant_headers, action_id, monkeypatch)
    resp = _confirm(client, tenant_headers, action_id, token)
    assert resp.status_code != 401, resp.get_json()
    with app.app_context():
        assert ProposedAction.query.get(action_id).status != 'awaiting_confirmation'


def test_a_credential_bound_to_the_action_alone_no_longer_taps(
        client, app, tenant_headers, auth_headers, monkeypatch):
    """A token minted the old way (operation = action id) is refused: the
    binding is the point, and a fifteen-minute TTL is the whole exposure
    at deploy time."""
    from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE
    from r6.stepup import generate_step_up_token
    action_id = _propose(client, tenant_headers)
    _commit(client, auth_headers, action_id)
    legacy = generate_step_up_token(tenant_headers['X-Tenant-Id'],
                                    audience=ACTION_APPROVAL_AUDIENCE,
                                    operation=action_id)
    resp = _confirm(client, tenant_headers, action_id, legacy)
    assert resp.status_code == 401
