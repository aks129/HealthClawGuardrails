"""GET /r6/ops/preflight — config preflight (spec W0 §preflight).

Each check returns {name, ok, detail, fatal}; the endpoint aggregates
{ok: all-fatal-ok, checks: [...]} and ALWAYS returns 200 — monitoring
reads the JSON verdict, not the status code.

Check functions are unit-tested directly (monkeypatching STEP_UP_SECRET
through the endpoint would break the auth gate the endpoint itself uses);
the endpoint tests cover auth, response shape, and fatal aggregation.
"""
from datetime import timedelta

import pytest

from r6.ops import checks

COMPOSE_DEFAULT = 'dev-step-up-secret-change-in-production'

_SHAPE_KEYS = {'name', 'ok', 'detail', 'fatal'}


def _assert_shape(result):
    assert set(result.keys()) == _SHAPE_KEYS
    assert isinstance(result['ok'], bool)
    assert isinstance(result['fatal'], bool)
    assert isinstance(result['name'], str)
    assert isinstance(result['detail'], str)


# ---------------------------------------------------------------- step_up

class TestStepUpSecret:
    def test_missing_is_fatal_red(self, monkeypatch):
        monkeypatch.delenv('STEP_UP_SECRET', raising=False)
        r = checks.check_step_up_secret()
        _assert_shape(r)
        assert r['name'] == 'step_up_secret'
        assert r['ok'] is False
        assert r['fatal'] is True

    def test_compose_default_is_red(self, monkeypatch):
        monkeypatch.setenv('STEP_UP_SECRET', COMPOSE_DEFAULT)
        r = checks.check_step_up_secret()
        assert r['ok'] is False
        assert r['fatal'] is True
        assert 'default' in r['detail']

    def test_short_secret_is_red(self, monkeypatch):
        monkeypatch.setenv('STEP_UP_SECRET', 'short')
        r = checks.check_step_up_secret()
        assert r['ok'] is False

    def test_good_secret_is_green(self, monkeypatch):
        monkeypatch.setenv('STEP_UP_SECRET', 'a-strong-production-secret')
        r = checks.check_step_up_secret()
        assert r['ok'] is True
        assert r['fatal'] is True


# ------------------------------------------------------- fasten webhook

class TestFastenWebhookSecret:
    def test_missing_is_fatal_red_and_explains_consequence(self, monkeypatch):
        monkeypatch.delenv('FASTEN_WEBHOOK_SECRET', raising=False)
        r = checks.check_fasten_webhook_secret()
        _assert_shape(r)
        assert r['name'] == 'fasten_webhook_secret'
        assert r['ok'] is False
        assert r['fatal'] is True
        # detail must explain the silent-failure consequence
        assert '401' in r['detail'] or 'reject' in r['detail'].lower()

    def test_set_is_green(self, monkeypatch):
        monkeypatch.setenv('FASTEN_WEBHOOK_SECRET', 'whsec_abc123')
        r = checks.check_fasten_webhook_secret()
        assert r['ok'] is True


# --------------------------------------------------- internal mint secret

class TestInternalMintSecret:
    def test_missing_outside_production_is_warning(self, monkeypatch):
        monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
        monkeypatch.delenv('FLASK_ENV', raising=False)
        r = checks.check_internal_mint_secret()
        _assert_shape(r)
        assert r['name'] == 'internal_mint_secret'
        assert r['ok'] is False
        assert r['fatal'] is False

    def test_missing_in_production_is_fatal(self, monkeypatch):
        monkeypatch.delenv('INTERNAL_TOKEN_MINT_SECRET', raising=False)
        monkeypatch.setenv('FLASK_ENV', 'production')
        r = checks.check_internal_mint_secret()
        assert r['ok'] is False
        assert r['fatal'] is True

    def test_set_is_green(self, monkeypatch):
        monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', 'mint-secret')
        r = checks.check_internal_mint_secret()
        assert r['ok'] is True


# ------------------------------------------------------- actions webhook

class TestActionsWebhook:
    def test_both_set_is_green(self, monkeypatch):
        monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'cb-secret')
        monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.example.org')
        r = checks.check_actions_webhook()
        _assert_shape(r)
        assert r['name'] == 'actions_webhook'
        assert r['ok'] is True
        assert r['fatal'] is True

    def test_missing_secret_is_fatal_red(self, monkeypatch):
        monkeypatch.delenv('ACTIONS_WEBHOOK_SECRET', raising=False)
        monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.example.org')
        r = checks.check_actions_webhook()
        assert r['ok'] is False
        assert r['fatal'] is True
        assert 'ACTIONS_WEBHOOK_SECRET' in r['detail']

    def test_missing_base_url_is_fatal_red(self, monkeypatch):
        monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'cb-secret')
        monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
        r = checks.check_actions_webhook()
        assert r['ok'] is False
        assert 'PUBLIC_BASE_URL' in r['detail']


# ------------------------------------------------------ executor env

class TestExecutorEnv:
    def test_one_check_per_registered_rail(self, action_registry):
        from r6.actions.registry import all_kinds
        results = checks.check_executor_env()
        assert {r['name'] for r in results} == {
            'rail:%s' % k for k in all_kinds()}
        for r in results:
            _assert_shape(r)
            assert r['fatal'] is False  # dark rail fails loud at execution

    def test_missing_env_is_red_with_var_named(self, action_registry,
                                               monkeypatch):
        monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
        results = {r['name']: r for r in checks.check_executor_env()}
        r = results['rail:phone-call']
        assert r['ok'] is False
        assert 'BLAND_AI_API_KEY' in r['detail']

    def test_present_env_is_green(self, action_registry, monkeypatch):
        monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'AC123')
        monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
        monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15550001111')
        results = {r['name']: r for r in checks.check_executor_env()}
        assert results['rail:sms']['ok'] is True


# ------------------------------------------------------------ database

class TestDatabase:
    def test_sqlite_outside_production_is_green_with_dialect(
            self, app, monkeypatch):
        monkeypatch.delenv('FLASK_ENV', raising=False)
        r = checks.check_database()
        _assert_shape(r)
        assert r['name'] == 'database'
        assert r['ok'] is True
        assert r['fatal'] is True
        assert 'sqlite' in r['detail']

    def test_sqlite_in_production_is_red(self, app, monkeypatch):
        monkeypatch.setenv('FLASK_ENV', 'production')
        r = checks.check_database()
        assert r['ok'] is False
        assert r['fatal'] is True
        assert 'sqlite' in r['detail']

    def test_query_error_is_fatal_red(self, app, monkeypatch):
        from models import db

        def boom(*a, **kw):
            raise RuntimeError('connection refused')

        monkeypatch.setattr(db.session, 'execute', boom)
        r = checks.check_database()
        assert r['ok'] is False
        assert r['fatal'] is True


# ------------------------------------------------------ telegram admin

class TestTelegramAdmin:
    def test_missing_is_nonfatal_red(self, monkeypatch):
        monkeypatch.delenv('TELEGRAM_ADMIN_CHAT_ID', raising=False)
        r = checks.check_telegram_admin()
        _assert_shape(r)
        assert r['name'] == 'telegram_admin'
        assert r['ok'] is False
        assert r['fatal'] is False

    def test_set_is_green(self, monkeypatch):
        monkeypatch.setenv('TELEGRAM_ADMIN_CHAT_ID', '12345')
        r = checks.check_telegram_admin()
        assert r['ok'] is True


# ---------------------------------------------------- reaper heartbeat

class TestReaperHeartbeat:
    def test_never_run(self, app):
        r = checks.check_reaper_heartbeat()
        _assert_shape(r)
        assert r['name'] == 'reaper_heartbeat'
        assert r['ok'] is False
        assert r['fatal'] is False
        assert 'never run' in r['detail']

    def test_recent_reaper_event_is_green_with_age(self, app):
        from models import db
        from r6.actions.events import ActionEvent
        db.session.add(ActionEvent(
            action_id='a1', from_status='executing', to_status='failed',
            actor='reaper'))
        db.session.commit()
        r = checks.check_reaper_heartbeat()
        assert r['ok'] is True
        assert 'ago' in r['detail']

    def test_stale_reaper_event_is_red(self, app):
        from models import db
        from r6.actions.events import ActionEvent
        from r6.actions.models import _utcnow
        db.session.add(ActionEvent(
            action_id='a1', from_status='executing', to_status='failed',
            actor='reaper', created_at=_utcnow() - timedelta(hours=2)))
        db.session.commit()
        r = checks.check_reaper_heartbeat()
        assert r['ok'] is False
        assert r['fatal'] is False

    def test_non_reaper_actors_do_not_count(self, app):
        from models import db
        from r6.actions.events import ActionEvent
        db.session.add(ActionEvent(
            action_id='a1', from_status='proposed',
            to_status='awaiting_confirmation', actor='commit-route'))
        db.session.commit()
        r = checks.check_reaper_heartbeat()
        assert r['ok'] is False
        assert 'never run' in r['detail']


# ------------------------------------------------------------ endpoint

@pytest.fixture
def green_fatal_env(monkeypatch):
    """Make every FATAL check green (STEP_UP_SECRET comes from conftest and
    is already a good non-default value; sqlite is fine outside production)."""
    monkeypatch.delenv('FLASK_ENV', raising=False)
    monkeypatch.setenv('FASTEN_WEBHOOK_SECRET', 'whsec_abc')
    monkeypatch.setenv('ACTIONS_WEBHOOK_SECRET', 'cb-secret')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.example.org')


class TestPreflightEndpoint:
    def test_requires_tenant_header(self, client):
        resp = client.get('/r6/ops/preflight')
        assert resp.status_code == 400

    def test_requires_step_up_token(self, client, tenant_headers):
        resp = client.get('/r6/ops/preflight', headers=tenant_headers)
        assert resp.status_code == 401

    def test_rejects_bad_token(self, client, tenant_id):
        resp = client.get('/r6/ops/preflight', headers={
            'X-Tenant-Id': tenant_id, 'X-Step-Up-Token': 'garbage.token'})
        assert resp.status_code == 401

    def test_rejects_foreign_tenant_token(self, client, tenant_id):
        from r6.stepup import generate_step_up_token
        resp = client.get('/r6/ops/preflight', headers={
            'X-Tenant-Id': tenant_id,
            'X-Step-Up-Token': generate_step_up_token('other-tenant')})
        assert resp.status_code == 401

    def test_response_shape_and_always_200(self, client, auth_headers,
                                           monkeypatch):
        # Deliberately break a fatal check — status code must STILL be 200.
        monkeypatch.delenv('FASTEN_WEBHOOK_SECRET', raising=False)
        resp = client.get('/r6/ops/preflight', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body.keys()) == {'ok', 'checks'}
        assert body['ok'] is False
        assert isinstance(body['checks'], list)
        for c in body['checks']:
            _assert_shape(c)
        names = {c['name'] for c in body['checks']}
        assert {'step_up_secret', 'fasten_webhook_secret',
                'internal_mint_secret', 'actions_webhook', 'database',
                'telegram_admin', 'reaper_heartbeat'} <= names
        assert any(n.startswith('rail:') for n in names)

    def test_nonfatal_failures_do_not_flip_overall_ok(
            self, client, auth_headers, green_fatal_env, monkeypatch):
        # Non-fatal reds: dark rail, no telegram admin, reaper never run.
        monkeypatch.delenv('BLAND_AI_API_KEY', raising=False)
        monkeypatch.delenv('TELEGRAM_ADMIN_CHAT_ID', raising=False)
        resp = client.get('/r6/ops/preflight', headers=auth_headers)
        body = resp.get_json()
        nonfatal_red = [c for c in body['checks']
                        if not c['fatal'] and not c['ok']]
        assert nonfatal_red  # the permutation really exercised the case
        assert body['ok'] is True

    def test_fatal_failure_flips_overall_ok(self, client, auth_headers,
                                            green_fatal_env, monkeypatch):
        monkeypatch.delenv('ACTIONS_WEBHOOK_SECRET', raising=False)
        resp = client.get('/r6/ops/preflight', headers=auth_headers)
        body = resp.get_json()
        assert resp.status_code == 200
        assert body['ok'] is False
