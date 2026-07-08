"""Real-signature tests for the Fasten Standard-Webhooks verifier.

Until now the only 'coverage' stubbed verify_webhook out entirely — the
header names, signed-content format, and v1 signature wire format were
unpinned (audit finding 2026-07-08). These compute real signatures.
"""

import base64
import hashlib
import hmac
import time

from r6.fasten.verify import verify_webhook

_SECRET_RAW = b"0123456789abcdef0123456789abcdef"
_SECRET = "whsec_" + base64.b64encode(_SECRET_RAW).decode()


def _sign(msg_id, ts, body: bytes, secret_raw=_SECRET_RAW):
    signed = f"{msg_id}.{ts}.{body.decode()}".encode()
    sig = base64.b64encode(
        hmac.new(secret_raw, signed, hashlib.sha256).digest()).decode()
    return f"v1,{sig}"


def _headers(msg_id="msg_1", ts=None, sig=None, body=b'{"type":"webhook.test"}'):
    ts = ts if ts is not None else str(int(time.time()))
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sig if sig is not None else _sign(msg_id, ts, body),
    }


def test_valid_signature_passes(monkeypatch):
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET", _SECRET)
    body = b'{"type":"patient.connection_success"}'
    assert verify_webhook(_headers(body=body), body) is True


def test_tampered_body_fails(monkeypatch):
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET", _SECRET)
    body = b'{"type":"patient.connection_success"}'
    headers = _headers(body=body)
    assert verify_webhook(headers, b'{"type":"forged"}') is False


def test_expired_timestamp_fails(monkeypatch):
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET", _SECRET)
    body = b"{}"
    old = str(int(time.time()) - 600)  # beyond the 300s replay window
    assert verify_webhook(_headers(ts=old, body=body), body) is False


def test_missing_headers_fail(monkeypatch):
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET", _SECRET)
    assert verify_webhook({}, b"{}") is False


def test_wrong_secret_fails(monkeypatch):
    monkeypatch.setenv("FASTEN_WEBHOOK_SECRET",
                       "whsec_" + base64.b64encode(b"x" * 32).decode())
    body = b"{}"
    assert verify_webhook(_headers(body=body), body) is False


def test_no_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("FASTEN_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FASTEN_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    body = b"{}"
    assert verify_webhook(_headers(body=body), body) is False


def test_explicit_dev_override_allows_unsigned(monkeypatch):
    monkeypatch.delenv("FASTEN_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("FASTEN_ALLOW_UNSIGNED_WEBHOOKS", "true")
    assert verify_webhook({}, b"{}") is True
