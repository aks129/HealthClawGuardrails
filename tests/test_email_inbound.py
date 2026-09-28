"""Inbound email webhook (Resend -> forward to maintainer Gmail).

support@/privacy@healthclaw.io are received by Resend (MX) which POSTs an
email.received webhook here; we verify the svix signature and forward the
message via the Resend send API. Fail-closed: no secret configured -> 503,
bad signature -> 403.
"""

import base64
import hashlib
import hmac
import json
import time

SECRET_B64 = base64.b64encode(b"test-webhook-secret-32-bytes!!").decode()


def _sign(payload: bytes, msg_id="msg_1", secret_b64=SECRET_B64, ts=None):
    ts = ts or str(int(time.time()))
    signed = f"{msg_id}.{ts}.{payload.decode()}".encode()
    sig = base64.b64encode(
        hmac.new(base64.b64decode(secret_b64), signed, hashlib.sha256).digest()
    ).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


def _event():
    return json.dumps({
        "type": "email.received",
        "data": {"email_id": "em_123", "from": "someone@example.com",
                 "to": ["support@healthclaw.io"], "subject": "Help please"},
    }).encode()


def test_no_secret_configured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("RESEND_INBOUND_WEBHOOK_SECRET", raising=False)
    r = client.post("/email/inbound", data=_event(),
                    content_type="application/json")
    assert r.status_code == 503


def test_bad_signature_403(client, monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    body = _event()
    headers = _sign(body)
    headers["svix-signature"] = "v1,AAAA"
    r = client.post("/email/inbound", data=body, headers=headers,
                    content_type="application/json")
    assert r.status_code == 403


def test_valid_signature_forwards(client, monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        class R:
            status_code = 200
            def json(self):
                return {"id": "fwd_1"}
        return R()

    def fake_get(url, headers=None, timeout=None):
        class R:
            status_code = 200
            def json(self):
                return {"from": "someone@example.com", "subject": "Help please",
                        "text": "My question...", "html": None}
        return R()

    import r6.email_inbound as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod.requests, "get", fake_get)

    body = _event()
    r = client.post("/email/inbound", data=body, headers=_sign(body),
                    content_type="application/json")
    assert r.status_code == 200
    assert sent["url"].endswith("/emails")
    assert sent["json"]["to"] == ["eugene.vestel@gmail.com"]
    assert "Help please" in sent["json"]["subject"]
    assert sent["json"]["reply_to"] == "someone@example.com"


def test_non_received_events_ignored(client, monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    body = json.dumps({"type": "email.sent", "data": {}}).encode()
    r = client.post("/email/inbound", data=body, headers=_sign(body),
                    content_type="application/json")
    assert r.status_code == 200


def _stub_resend(monkeypatch, get_status=200, get_body=None):
    """Stub the Resend API: record the fetch URL and the forward payload."""
    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post_json"] = json
        class R:
            status_code = 200
        return R()

    def fake_get(url, headers=None, timeout=None):
        calls["get_url"] = url
        class R:
            status_code = get_status
            def json(self):
                return get_body or {}
        return R()

    import r6.email_inbound as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod.requests, "get", fake_get)
    return calls


def test_fetches_body_from_receiving_api(client, monkeypatch):
    # Received mail lives under /emails/receiving/{id}; /emails/{id} is the
    # *sent*-email endpoint and has no body for an inbound message.
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = _stub_resend(monkeypatch, get_body={
        "from": "someone@example.com", "subject": "Help please",
        "text": "Synthetic body line", "html": "<p>Synthetic body line</p>"})

    body = _event()
    r = client.post("/email/inbound", data=body, headers=_sign(body),
                    content_type="application/json")
    assert r.status_code == 200
    assert calls["get_url"] == "https://api.resend.com/emails/receiving/em_123"
    assert "Synthetic body line" in calls["post_json"]["text"]
    assert calls["post_json"]["html"] == "<p>Synthetic body line</p>"
    assert "could not be retrieved" not in calls["post_json"]["text"]


def test_failed_fetch_still_forwards_with_notice(client, monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = _stub_resend(monkeypatch, get_status=404)

    body = _event()
    r = client.post("/email/inbound", data=body, headers=_sign(body),
                    content_type="application/json")
    assert r.status_code == 200
    text = calls["post_json"]["text"]
    assert "could not be retrieved" in text
    assert "em_123" in text
    assert "Help please" in calls["post_json"]["subject"]


def test_fetch_exception_still_forwards_with_notice(client, monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", f"whsec_{SECRET_B64}")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = _stub_resend(monkeypatch)
    import r6.email_inbound as mod

    def boom(url, headers=None, timeout=None):
        raise mod.requests.ConnectionError("unreachable")
    monkeypatch.setattr(mod.requests, "get", boom)

    body = _event()
    r = client.post("/email/inbound", data=body, headers=_sign(body),
                    content_type="application/json")
    assert r.status_code == 200
    assert "could not be retrieved" in calls["post_json"]["text"]
    assert "em_123" in calls["post_json"]["text"]
