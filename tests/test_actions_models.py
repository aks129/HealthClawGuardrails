"""ProposedAction lifecycle model tests."""
import json
from datetime import datetime, timedelta, timezone

from r6.actions.models import ProposedAction, PROPOSAL_TTL_MINUTES


def test_create_defaults(app):
    with app.app_context():
        from models import db
        action = ProposedAction(
            tenant_id='test-tenant',
            kind='phone-call',
            payload={'to': 'CVS Pharmacy', 'phone': '617-555-0100',
                     'body': 'Refill script text'},
        )
        db.session.add(action)
        db.session.commit()

        assert action.id  # uuid assigned
        assert action.status == 'proposed'
        assert action.kind == 'phone-call'
        assert json.loads(action.payload_json)['phone'] == '617-555-0100'
        assert action.external_ref is None
        # expires ~30 min out
        delta = action.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
        assert timedelta(minutes=PROPOSAL_TTL_MINUTES - 1) < delta <= timedelta(minutes=PROPOSAL_TTL_MINUTES)


def test_is_expired(app):
    with app.app_context():
        from models import db
        action = ProposedAction(tenant_id='t', kind='sms', payload={'body': 'x'})
        action.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.session.add(action)
        db.session.commit()
        assert action.is_expired() is True


def test_invalid_kind_rejected(app):
    with app.app_context():
        import pytest
        with pytest.raises(ValueError):
            ProposedAction(tenant_id='t', kind='teleport', payload={})


def test_summary_has_no_payload(app):
    with app.app_context():
        from models import db
        action = ProposedAction(
            tenant_id='t', kind='phone-call',
            payload={'to': 'CVS', 'phone': '617-555-0100', 'body': 'SECRET SCRIPT'},
        )
        db.session.add(action)
        db.session.commit()
        s = action.summary()
        assert 'SECRET SCRIPT' not in json.dumps(s)
        assert '617-555-0100' not in json.dumps(s)
        assert s['kind'] == 'phone-call'
        assert s['to'] == 'CVS'
