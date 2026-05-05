"""
tests/test_subscribe.py

POST /api/subscribe — newsletter sign-up backed by the Resend Audiences API.
We mock httpx so tests never reach the network.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


def _resp(status: int, body: dict | None = None) -> MagicMock:
    """Build a fake httpx.Response."""
    r = MagicMock()
    r.status_code = status
    r.headers = {"content-type": "application/json"}
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    return r


@pytest.fixture
def resend_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_AUDIENCE_ID", "aud_test")


class TestSubscribeValidation:

    def test_missing_email_returns_400(self, client, resend_env):
        r = client.post('/api/subscribe', json={})
        assert r.status_code == 400
        assert b"email is required" in r.data

    def test_empty_email_returns_400(self, client, resend_env):
        r = client.post('/api/subscribe', json={"email": "   "})
        assert r.status_code == 400

    def test_invalid_email_returns_400(self, client, resend_env):
        r = client.post('/api/subscribe', json={"email": "not-an-email"})
        assert r.status_code == 400
        assert b"invalid email" in r.data


class TestSubscribeConfig:

    def test_missing_resend_key_returns_503(self, client, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("RESEND_AUDIENCE_ID", raising=False)
        r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 503
        assert b"not configured" in r.data

    def test_missing_audience_id_returns_503(self, client, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
        monkeypatch.delenv("RESEND_AUDIENCE_ID", raising=False)
        r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 503


class TestSubscribeHappyPath:

    def test_201_response_returns_ok(self, client, resend_env):
        with patch('app.httpx.post', return_value=_resp(201, {"id": "ct_1"})) as mock_post:
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["email"] == "ok@example.com"

        # Confirm we hit the right Resend endpoint with the right auth header.
        url = mock_post.call_args.args[0]
        assert "/audiences/aud_test/contacts" in url
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer re_test_key"

    def test_form_data_also_works(self, client, resend_env):
        with patch('app.httpx.post', return_value=_resp(201)):
            r = client.post('/api/subscribe', data={"email": "form@example.com"})
        assert r.status_code == 200
        assert r.get_json()["email"] == "form@example.com"

    def test_duplicate_treated_as_success(self, client, resend_env):
        body = {"name": "validation_error", "message": "Contact already exists"}
        with patch('app.httpx.post', return_value=_resp(422, body)):
            r = client.post('/api/subscribe', json={"email": "dup@example.com"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["already_subscribed"] is True


class TestSubscribeFailures:

    def test_resend_5xx_returns_502(self, client, resend_env):
        with patch('app.httpx.post', return_value=_resp(500, {"error": "server"})):
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 502
        assert b"could not save" in r.data

    def test_network_error_returns_502(self, client, resend_env):
        import httpx
        with patch('app.httpx.post', side_effect=httpx.ConnectError("dns fail")):
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 502
        assert b"could not reach" in r.data

    def test_other_422_falls_through_to_502(self, client, resend_env):
        # 422 that's NOT a duplicate should still be a server-side error.
        body = {"name": "validation_error", "message": "email malformed by API"}
        with patch('app.httpx.post', return_value=_resp(422, body)):
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 502


class TestSubscribeFormOnLandingPage:
    """Make sure the landing page actually exposes the form so end-users can use it."""

    def test_form_renders(self, client):
        r = client.get('/')
        html = r.data.decode()
        assert 'id="subscribe-form"' in html
        assert 'name="email"' in html
        assert '/api/subscribe' in html  # JS posts here

    def test_eyebrow_and_resend_credit_present(self, client):
        html = client.get('/').data.decode()
        assert "Stay in the loop" in html
        assert "resend.com" in html.lower()
