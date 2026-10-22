"""Security probe for FHI-60: an exception message can carry the raw id.

SQLAlchemy errors include their bound parameters in str(exc) unless the
engine sets hide_parameters. A log line that hashes the tenant and then
appends `exc` writes the raw tenant anyway. Synthetic values only.
"""

from __future__ import annotations

import logging
import secrets
from unittest.mock import patch

from sqlalchemy import text

from models import db
from r6 import telegram_push


def test_a_db_error_in_the_telegram_lookup_does_not_log_the_raw_tenant(
        app, caplog, monkeypatch):
    token = secrets.token_urlsafe(32)

    def failing_lookup(tenant_id):
        db.session.execute(text("SELECT chat_id FROM no_such_table "
                                "WHERE tenant_id = :t"), {"t": tenant_id})

    monkeypatch.setattr(telegram_push, "is_enabled", lambda: True)
    with app.app_context(), caplog.at_level(logging.DEBUG), patch.object(
            telegram_push.TelegramBinding, "chat_ids_for_tenant",
            side_effect=failing_lookup):
        telegram_push.notify_tenant(token, "synthetic")
        db.session.rollback()
    assert token not in "\n".join(r.getMessage() for r in caplog.records)
