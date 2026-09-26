"""A failed database write logs the exception class, never its message.

str() on a SQLAlchemy DBAPIError serialises the failing statement and its
bound parameters. On a FHIR create or update those parameters are the
resource JSON, so `{e}` in a log line copies the record into the logs.
#306 fixed this shape in SHC ingest (tests/test_shc_bridge.py); these pin
the same rule on the FHIR write paths, the seed insert and the Telegram
bind. The bind's audit row is pinned too: audit detail stays PII-free, so
it names what happened, not which chat.

MUTATION (each turns exactly one test red):
- r6/routes.py create_resource: log `{e}` instead of `type(e).__name__`.
- r6/routes.py update_resource: the same.
- r6/routes.py bind_telegram_chat: log `exc` instead of its class name.
- r6/routes.py bind_telegram_chat: put `chat_id=`/`username=` back in detail.
- r6/seed.py seed_demo_data: log `e` instead of its class name.
"""

import json
import logging
from unittest.mock import patch

from models import db
from r6.models import AuditEventRecord

# Synthetic. Each stands in for a value that sits in a bound parameter.
MARKER = 'Kowalski-PHI-LEAK-1971-03-02'
CHAT_ID = 918273645
USERNAME = 'leaky_handle_77'


class PoisonedStatementError(Exception):
    """Stands in for a DBAPIError whose str() echoes the bound parameters."""


def _poisoned(*_args, **_kwargs):
    raise PoisonedStatementError(
        '(psycopg2.errors.StringDataRightTruncation) INSERT INTO '
        f"resources (...) VALUES (...) -- parameters: {{'json': '{MARKER}'}}"
    )


def _patient():
    return {
        'resourceType': 'Patient',
        'id': 'db-error-patient',
        'name': [{'family': MARKER}],
    }


def test_failed_create_logs_the_class_not_the_record(client, auth_headers, caplog):
    caplog.set_level(logging.DEBUG)
    with patch.object(db.session, 'commit', side_effect=_poisoned):
        resp = client.post('/r6/fhir/Patient', data=json.dumps(_patient()),
                           content_type='application/json', headers=auth_headers)
    assert resp.status_code == 500
    assert MARKER not in caplog.text
    assert 'PoisonedStatementError' in caplog.text


def test_failed_update_logs_the_class_not_the_record(client, auth_headers, caplog):
    body = _patient()
    body['name'] = [{'family': 'Synthetic'}]
    created = client.post('/r6/fhir/Patient', data=json.dumps(body),
                           content_type='application/json', headers=auth_headers)
    assert created.status_code == 201

    caplog.set_level(logging.DEBUG)
    with patch.object(db.session, 'commit', side_effect=_poisoned):
        resp = client.put(f'/r6/fhir/Patient/{body["id"]}',
                          data=json.dumps(_patient()),
                          content_type='application/json', headers=auth_headers)
    assert resp.status_code == 500
    assert MARKER not in caplog.text
    assert 'PoisonedStatementError' in caplog.text


def test_failed_seed_insert_logs_the_class_not_the_record(app, caplog):
    from r6.seed import seed_demo_data

    caplog.set_level(logging.DEBUG)
    with app.app_context():
        with patch.object(db.session, 'flush', side_effect=_poisoned):
            seed_demo_data('seed-db-error', resources=[_patient()])
    assert MARKER not in caplog.text
    assert 'PoisonedStatementError' in caplog.text


def _bind(client, tenant_id, step_up_token):
    return client.post('/r6/fhir/internal/bind-telegram', json={
        'tenant_id': tenant_id,
        'chat_id': CHAT_ID,
        'username': USERNAME,
        'step_up_token': step_up_token,
    })


def test_bind_audit_detail_names_no_chat_or_user(client, app, tenant_id, step_up_token):
    resp = _bind(client, tenant_id, step_up_token)
    assert resp.status_code == 201

    with app.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type='TelegramBinding').all()
    assert len(rows) == 1
    detail = rows[0].detail or ''
    assert detail, 'the bind row should still say what happened'
    assert str(CHAT_ID) not in detail
    assert USERNAME not in detail


def test_failed_bind_logs_the_class_not_the_chat(client, tenant_id, step_up_token, caplog):
    def _boom(**_kwargs):
        raise PoisonedStatementError(
            f"INSERT INTO telegram_bindings -- parameters: "
            f"{{'chat_id': {CHAT_ID}, 'username': '{USERNAME}'}}"
        )

    caplog.set_level(logging.DEBUG)
    with patch('r6.telegram_push.bind', side_effect=_boom):
        resp = _bind(client, tenant_id, step_up_token)
    assert resp.status_code == 500
    assert str(CHAT_ID) not in caplog.text
    assert USERNAME not in caplog.text
    assert 'PoisonedStatementError' in caplog.text
