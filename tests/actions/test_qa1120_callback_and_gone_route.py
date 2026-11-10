"""QA exploits for PR #1120 (free-text calls cannot dial).

1. The 410 route claims it answers before any body is read. The PR's own
   test posts `{}`, so a handler that parsed the body first still passed
   (QA mutation: `request.get_json(force=True)` at the top -> survived).
2. The callback now resolves texts only, but for a text it still stores the
   caller's free `summary` (a field Twilio never sends) and skips the
   provider-ref match when no MessageSid is given. A holder of the
   query-string secret can therefore write a `delivery_link` into an SMS
   action's outcome, which the consumer app and the review page render as
   a link. Pre-existing, not introduced here (#1123); pinned strict-xfail so
   the fix flips it.
"""
import pytest

from models import db
from r6.actions.models import ProposedAction


def test_the_gone_route_answers_410_before_parsing_a_malformed_body(client):
    """MUTATION: call `request.get_json(force=True)` at the top of
    propose_rx_transfer in r6/actions/routes.py -> red (400)."""
    resp = client.post('/r6/actions/rx-transfer/propose',
                       data='{"to_pharmacy":',
                       headers={'Content-Type': 'application/json'})
    assert resp.status_code == 410


def _sms_in_flight(app, tenant_id):
    with app.app_context():
        row = ProposedAction(tenant_id=tenant_id, kind='sms',
                             payload={'to': 'patient',
                                      'phone': '+15555550123',
                                      'body': 'Reading reminder'})
        row.status = 'executing'
        row.external_ref = 'SM-qa1120'
        db.session.add(row)
        db.session.commit()
        return row.id


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    '#1123, pre-existing: the Twilio-only callback still accepts the call '
    "provider's free-text `summary` and skips the ref match without a "
    'MessageSid'))
def test_a_forged_text_callback_cannot_write_a_link_into_the_outcome(
        client, app, tenant_id, monkeypatch):
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'hook-secret')
    action_id = _sms_in_flight(app, tenant_id)

    resp = client.post(
        '/r6/actions/callback/twilio?action_id=%s&secret=hook-secret'
        % action_id,
        json={'status': 'completed',
              'summary': '{"delivery_link": "https://example.invalid/x"}'})
    # The forged callback is accepted today; the defect is what it stores.
    # Not an assert: the xfail above expects AssertionError only, so a
    # refusal here turns the test red instead of xfailing for the wrong
    # reason.
    if resp.status_code != 200:
        raise RuntimeError('the callback answered %d; rewrite this pin'
                           % resp.status_code)

    with app.app_context():
        row = db.session.get(ProposedAction, action_id)
        assert 'delivery_link' not in (row.outcome_summary or '')
