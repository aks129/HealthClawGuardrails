import json
from models import db
from r6.actions.events import ActionEvent


def test_action_event_row_persists(app):
    with app.app_context():
        ev = ActionEvent(action_id='a1', from_status='proposed',
                         to_status='awaiting_confirmation', actor='commit-route',
                         detail='submitted')
        db.session.add(ev); db.session.commit()
        got = ActionEvent.query.filter_by(action_id='a1').one()
        assert got.from_status == 'proposed'
        assert got.to_status == 'awaiting_confirmation'
        assert got.actor == 'commit-route'
        assert got.created_at is not None
