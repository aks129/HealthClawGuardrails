"""
Tests for the Telegram ↔ Fasten connect flow:

  POST /r6/fhir/internal/bind-telegram        (binds chat_id → tenant)
  GET  /connect/<tenant_id>                   (renders the TEFCA Stitch page)
  r6.telegram_push.notify_tenant              (back-channel push helper)
"""

from unittest.mock import patch, MagicMock

import pytest

from r6.models import TelegramBinding


# ── /internal/bind-telegram ───────────────────────────────────────────────

class TestBindTelegram:
    def test_rejects_missing_fields(self, client, step_up_token, tenant_id):
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={'tenant_id': tenant_id, 'step_up_token': step_up_token},
        )
        assert resp.status_code == 400
        assert 'chat_id' in resp.get_json()['error']

    def test_rejects_non_integer_chat_id(self, client, step_up_token, tenant_id):
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': tenant_id,
                'chat_id': 'not-a-number',
                'step_up_token': step_up_token,
            },
        )
        assert resp.status_code == 400

    def test_rejects_invalid_tenant_format(self, client, step_up_token):
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': 'has spaces and $ymbols',
                'chat_id': 123,
                'step_up_token': step_up_token,
            },
        )
        assert resp.status_code == 400

    def test_rejects_missing_step_up(self, client, tenant_id):
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={'tenant_id': tenant_id, 'chat_id': 12345},
        )
        assert resp.status_code == 401

    def test_rejects_cross_tenant_step_up(self, client, tenant_id):
        """Step-up token for tenant A cannot bind chats to tenant B."""
        from r6.stepup import generate_step_up_token
        other_token = generate_step_up_token('a-different-tenant')
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': tenant_id,
                'chat_id': 12345,
                'step_up_token': other_token,
            },
        )
        assert resp.status_code == 401

    def test_binds_chat_to_tenant(self, client, step_up_token, tenant_id, app):
        resp = client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': tenant_id,
                'chat_id': 999_001,
                'username': 'evestel',
                'step_up_token': step_up_token,
            },
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body['tenant_id'] == tenant_id
        assert body['chat_id'] == 999_001
        assert body['binding_id']
        assert body['bound_at']

        with app.app_context():
            assert TelegramBinding.chat_ids_for_tenant(tenant_id) == [999_001]

    def test_bind_is_idempotent(self, client, step_up_token, tenant_id, app):
        for _ in range(3):
            resp = client.post(
                '/r6/fhir/internal/bind-telegram',
                json={
                    'tenant_id': tenant_id,
                    'chat_id': 999_002,
                    'username': 'evestel',
                    'step_up_token': step_up_token,
                },
            )
            assert resp.status_code == 201

        with app.app_context():
            chats = TelegramBinding.chat_ids_for_tenant(tenant_id)
            assert chats == [999_002]


# ── /connect/<tenant_id> page ─────────────────────────────────────────────

class TestConnectPage:
    def test_invalid_tenant_returns_400(self, client):
        resp = client.get('/connect/has spaces')
        assert resp.status_code == 400

    def test_renders_with_tefca_when_key_set(self, client, monkeypatch):
        monkeypatch.setenv('FASTEN_PUBLIC_KEY', 'public_test_XYZ')
        monkeypatch.setenv('FASTEN_TEFCA_MODE', 'true')
        resp = client.get('/connect/ev-personal')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'public-id="public_test_XYZ"' in html
        assert 'tefca-mode="true"' in html
        assert 'ev-personal' in html

    def test_omits_tefca_attribute_when_disabled(self, client, monkeypatch):
        monkeypatch.setenv('FASTEN_PUBLIC_KEY', 'public_test_XYZ')
        monkeypatch.setenv('FASTEN_TEFCA_MODE', 'false')
        resp = client.get('/connect/ev-personal')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'tefca-mode="true"' not in html
        assert 'public-id="public_test_XYZ"' in html

    def test_shows_warning_when_key_missing(self, client, monkeypatch):
        monkeypatch.delenv('FASTEN_PUBLIC_KEY', raising=False)
        resp = client.get('/connect/ev-personal')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'FASTEN_PUBLIC_KEY is not set' in html


# ── r6.telegram_push.notify_tenant ────────────────────────────────────────

class TestNotifyTenant:
    def test_returns_zero_when_no_bot_token(self, client, tenant_id, step_up_token, monkeypatch):
        # Bind a chat first
        client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': tenant_id,
                'chat_id': 12345,
                'step_up_token': step_up_token,
            },
        )
        monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
        from r6.telegram_push import notify_tenant
        sent = notify_tenant(tenant_id, 'hello')
        assert sent == 0

    def test_returns_zero_when_no_bindings(self, monkeypatch):
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-token-for-test')
        from r6.telegram_push import notify_tenant
        sent = notify_tenant('tenant-with-no-bindings', 'hello')
        assert sent == 0

    def test_sends_to_each_bound_chat(self, client, tenant_id, step_up_token, monkeypatch):
        # Bind two chats to the same tenant
        for cid in (777_001, 777_002):
            client.post(
                '/r6/fhir/internal/bind-telegram',
                json={
                    'tenant_id': tenant_id,
                    'chat_id': cid,
                    'step_up_token': step_up_token,
                },
            )

        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-token-for-test')

        with patch('r6.telegram_push.requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            from r6.telegram_push import notify_tenant
            sent = notify_tenant(tenant_id, '*Records ready*')

        assert sent == 2
        # Two calls to Telegram Bot API, one per chat
        assert mock_post.call_count == 2
        urls_called = [c.args[0] for c in mock_post.call_args_list]
        assert all('/bot' in u and '/sendMessage' in u for u in urls_called)
        chat_ids_called = sorted(c.kwargs['json']['chat_id'] for c in mock_post.call_args_list)
        assert chat_ids_called == [777_001, 777_002]

    def test_treats_telegram_4xx_as_failure(self, client, tenant_id, step_up_token, monkeypatch):
        client.post(
            '/r6/fhir/internal/bind-telegram',
            json={
                'tenant_id': tenant_id,
                'chat_id': 12345,
                'step_up_token': step_up_token,
            },
        )
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-token-for-test')

        with patch('r6.telegram_push.requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 403  # bot blocked by user
            mock_resp.text = 'Forbidden: bot was blocked by the user'
            mock_post.return_value = mock_resp

            from r6.telegram_push import notify_tenant
            sent = notify_tenant(tenant_id, 'hi')
        assert sent == 0
