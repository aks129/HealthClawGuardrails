"""Exception text never carries a caller's value into the log (FHI-60).

Hashing an id in the format string does nothing if the same line then
appends `exc`: SQLAlchemy puts bound parameters in str(exc) and tracebacks,
and requests puts the URL (with the Telegram bot token in it) in its error.
So the engine sets `hide_parameters` on every database, and the r6 sites
that logged an exception log its class name instead. Synthetic values only.
"""

from __future__ import annotations

import logging
import os
import secrets
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import text

from models import db
from r6.log_safety import id_hash, vendor_request_id


def _logged(caplog):
    out = []
    for record in caplog.records:
        out.append(record.getMessage())
        if record.exc_info:
            out.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(out)


# --- hide_parameters --------------------------------------------------------

def test_the_engine_hides_bound_parameters(app):
    token = secrets.token_urlsafe(32)
    with app.app_context():
        assert db.engine.hide_parameters is True
        with pytest.raises(Exception) as caught:
            db.session.execute(text("SELECT 1 FROM no_such_table "
                                    "WHERE tenant_id = :t"), {"t": token})
        db.session.rollback()
    assert token not in str(caught.value)


def test_supplied_engine_options_still_hide_parameters():
    """The Postgres branch used to be the only one with engine options; a
    caller-supplied set must not drop the one that matters here."""
    from main import create_app
    app = create_app({"TESTING": True,
                      "SQLALCHEMY_DATABASE_URI":
                          os.environ["SQLALCHEMY_DATABASE_URI"],
                      "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
                      "LEGACY_BOOT_ON_CREATE": False})
    with app.app_context():
        assert db.engine.hide_parameters is True


# --- sites that logged the exception ----------------------------------------

def test_a_failed_fasten_revoke_logs_the_class_not_the_message(
        client, caplog, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "exc-text-secret")
    token = secrets.token_urlsafe(32)
    with patch("r6.fasten.revoke.revoke_tenant",
               side_effect=RuntimeError(f"bound value {token}")), \
            caplog.at_level(logging.DEBUG):
        r = client.post("/r6/fhir/internal/fasten-revoke",
                        json={"tenant_id": "exc-text-tenant"},
                        headers={"X-Internal-Secret": "exc-text-secret"})

    assert r.status_code == 500
    logged = _logged(caplog)
    assert token not in logged
    assert "fasten revoke failed for" in logged and "RuntimeError" in logged


def test_a_telegram_send_error_never_logs_the_bot_token(caplog, monkeypatch):
    from r6 import telegram_push
    bot = "123456:" + secrets.token_urlsafe(24)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot)

    def fail(url, **kwargs):
        raise requests.ConnectionError(f"failed to reach {url}")

    monkeypatch.setattr(telegram_push.requests, "post", fail)
    with caplog.at_level(logging.DEBUG):
        assert telegram_push.send_to_chat(424242, "synthetic") is False

    logged = _logged(caplog)
    assert bot not in logged
    assert "424242" not in logged
    assert "ConnectionError" in logged


def test_a_failed_telegram_lookup_logs_the_class_not_the_message(
        app, caplog, monkeypatch):
    """Not only a DB error: any exception text could carry the tenant."""
    from r6 import telegram_push
    token = secrets.token_urlsafe(32)
    monkeypatch.setattr(telegram_push, "is_enabled", lambda: True)
    with app.app_context(), caplog.at_level(logging.DEBUG), patch.object(
            telegram_push.TelegramBinding, "chat_ids_for_tenant",
            side_effect=RuntimeError(f"no binding for {token}")):
        assert telegram_push.notify_tenant(token, "synthetic") == 0

    logged = _logged(caplog)
    assert token not in logged
    assert "RuntimeError" in logged


@pytest.mark.parametrize("path", ["/shc/medent/callback",
                                  "/shc/hbo/callback"])
def test_an_oauth_callback_error_value_is_not_logged(client, caplog, path):
    value = secrets.token_urlsafe(24)
    with caplog.at_level(logging.DEBUG):
        client.get(f"{path}?error={value}")
    assert value not in _logged(caplog)


# --- Fasten's request id: the security ruling -------------------------------

_UUID = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"


@pytest.mark.parametrize("value,expected", [
    (_UUID, _UUID),
    (_UUID.upper(), _UUID.upper()),
    (None, "-"),
    ("", "-"),
])
def test_a_canonical_uuid_is_logged_as_sent(value, expected):
    assert vendor_request_id(value) == expected


@pytest.mark.parametrize("value", [
    _UUID.replace("-", ""),             # undashed 32-hex
    "req_01J8XKQ2M4YB3",
    pytest.param(secrets.token_urlsafe(32), id="random-urlsafe"),
    _UUID + "x",
    "{" + _UUID + "}",
    " " + _UUID,
    _UUID + "\n",
    12345,
])
def test_anything_else_is_hashed(value):
    assert vendor_request_id(value) == id_hash(value)


def _diagnostic(client, request_id):
    return client.post("/r6/fhir/internal/connect-diagnostic",
                       json={"payload": {"event_type": "widget.config_error",
                                         "error": "config_error",
                                         "request_id": request_id}},
                       headers={"X-Tenant-Id": "desktop-demo"})


def test_connect_diagnostic_logs_a_uuid_request_id_for_support(client, caplog):
    with caplog.at_level(logging.WARNING):
        assert _diagnostic(client, _UUID).status_code == 202
    assert f"request_id={_UUID} " in _logged(caplog)


def test_connect_diagnostic_hashes_any_other_request_id(client, caplog):
    for value in (_UUID.replace("-", ""), secrets.token_urlsafe(32)):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _diagnostic(client, value)
        logged = _logged(caplog)
        assert value not in logged
        assert f"request_id={id_hash(value)} " in logged
