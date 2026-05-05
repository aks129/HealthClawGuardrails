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
        # First call: contact creation. Second call: welcome email (always 202).
        with patch('app.httpx.post', side_effect=[_resp(201, {"id": "ct_1"}), _resp(202, {"id": "em_1"})]) as mock_post:
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["email"] == "ok@example.com"

        # Confirm we hit the right Resend endpoints in order.
        contacts_call = mock_post.call_args_list[0]
        assert "/audiences/aud_test/contacts" in contacts_call.args[0]
        assert contacts_call.kwargs["headers"]["Authorization"] == "Bearer re_test_key"

    def test_form_data_also_works(self, client, resend_env):
        with patch('app.httpx.post', side_effect=[_resp(201), _resp(202)]):
            r = client.post('/api/subscribe', data={"email": "form@example.com"})
        assert r.status_code == 200
        assert r.get_json()["email"] == "form@example.com"

    def test_duplicate_treated_as_success(self, client, resend_env):
        body = {"name": "validation_error", "message": "Contact already exists"}
        # Duplicates do NOT trigger the welcome email — single Resend call only.
        with patch('app.httpx.post', return_value=_resp(422, body)) as mock_post:
            r = client.post('/api/subscribe', json={"email": "dup@example.com"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["already_subscribed"] is True
        assert mock_post.call_count == 1


class TestWelcomeEmail:

    def _captured_calls(self, client, resend_env):
        captured: list = []

        def fake_post(url, **kwargs):
            captured.append((url, kwargs))
            if "/contacts" in url:
                return _resp(201, {"id": "ct_1"})
            return _resp(202, {"id": "em_1"})

        with patch('app.httpx.post', side_effect=fake_post):
            r = client.post('/api/subscribe', json={"email": "welcome@example.com"})
        return r, captured

    def test_welcome_email_sent_after_create(self, client, resend_env):
        r, calls = self._captured_calls(client, resend_env)
        assert r.status_code == 200
        # 1st call = contacts, 2nd = emails
        assert len(calls) == 2
        assert "/audiences/aud_test/contacts" in calls[0][0]
        assert calls[1][0] == "https://api.resend.com/emails"

    def test_welcome_email_payload_shape(self, client, resend_env):
        _, calls = self._captured_calls(client, resend_env)
        body = calls[1][1]["json"]
        assert body["to"] == ["welcome@example.com"]
        assert "HealthClaw" in body["subject"]
        assert "@healthclaw.io" in body["from"]
        # PDF attached when the artifact exists in static/
        from pathlib import Path
        pdf_path = Path(__file__).parent.parent / "static" / "healthclaw-quickstart.pdf"
        if pdf_path.is_file():
            atts = body.get("attachments", [])
            assert atts and atts[0]["filename"] == "healthclaw-quickstart.pdf"
            assert atts[0]["content"]  # base64 string, non-empty
        # HTML + text fallback both present
        assert "html" in body and "<html>" in body["html"].lower()
        assert "text" in body and "Welcome to HealthClaw" in body["text"]

    def test_welcome_email_failure_does_not_break_signup(self, client, resend_env):
        """Email send failures are logged, never surfaced — contact is the
        load-bearing thing and is already saved by the time the email fires."""
        def fake_post(url, **kwargs):
            if "/contacts" in url:
                return _resp(201, {"id": "ct_1"})
            return _resp(500, {"error": "email service down"})

        with patch('app.httpx.post', side_effect=fake_post):
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_welcome_email_network_failure_does_not_break_signup(self, client, resend_env):
        import httpx as _httpx

        def fake_post(url, **kwargs):
            if "/contacts" in url:
                return _resp(201, {"id": "ct_1"})
            raise _httpx.ConnectError("welcome host unreachable")

        with patch('app.httpx.post', side_effect=fake_post):
            r = client.post('/api/subscribe', json={"email": "ok@example.com"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


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
