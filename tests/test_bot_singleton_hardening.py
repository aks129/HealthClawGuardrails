"""
tests/test_bot_singleton_hardening.py

W0 item 6 — mid-run poller singleton hardening.

_preflight_singleton_check() (openclaw/bot.py) only guards STARTUP: it
probes getUpdates once before the bot begins polling. If a second poller
starts LATER — a stale Railway container still shutting down, a forgotten
copy left running on the Mac mini or an SSH box — this process keeps
polling and Telegram thrashes both pollers with repeated
'Conflict: terminated by other getUpdates request' errors, silently
dropping/duplicating messages.

These tests cover the mid-run detector wired through python-telegram-bot's
error-handler hook (_on_error): a sliding window of Conflict-error
timestamps that declares a STORM (>= CONFLICT_STORM_THRESHOLD errors within
CONFLICT_STORM_WINDOW_SECONDS) and, on a storm, logs CRITICAL, sends a
best-effort admin alert, and exits (SystemExit) so the process supervisor
surfaces the failure instead of the poller quietly thrashing forever.

openclaw/bot.py imports the telegram SDK (python-telegram-bot), which is a
dev-only test dependency here (see pyproject.toml [dependency-groups.dev])
— production installs it via openclaw/Dockerfile. TELEGRAM_BOT_TOKEN must
be set before the module is imported (bot.py reads it at import time).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token-for-singleton-hardening')

sys.path.insert(0, str(Path(__file__).parent.parent / "openclaw"))

import bot  # noqa: E402
from telegram.error import Conflict, TimedOut  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_conflict_window():
    """Every test starts with a clean sliding window."""
    bot._conflict_timestamps.clear()
    yield
    bot._conflict_timestamps.clear()


def _run(coro):
    return asyncio.run(coro)


def _ctx_with_error(error):
    return SimpleNamespace(error=error)


# ---------------------------------------------------------------------------
# Pure sliding-window logic
# ---------------------------------------------------------------------------

class TestConflictWindow:

    def test_single_conflict_does_not_declare_storm(self):
        assert bot._record_conflict_and_check_storm(now=1000.0) is False
        assert len(bot._conflict_timestamps) == 1

    def test_three_within_window_declares_storm(self):
        assert bot._record_conflict_and_check_storm(now=1000.0) is False
        assert bot._record_conflict_and_check_storm(now=1010.0) is False
        assert bot._record_conflict_and_check_storm(now=1020.0) is True

    def test_three_spread_over_more_than_60s_does_not_declare_storm(self):
        # Each pair is > 60s apart, so the window never holds more than one
        # timestamp at a time.
        assert bot._record_conflict_and_check_storm(now=1000.0) is False
        assert bot._record_conflict_and_check_storm(now=1070.0) is False
        assert bot._record_conflict_and_check_storm(now=1140.0) is False
        assert len(bot._conflict_timestamps) == 1

    def test_window_resets_after_storm_verdict(self):
        assert bot._record_conflict_and_check_storm(now=1000.0) is False
        assert bot._record_conflict_and_check_storm(now=1010.0) is False
        assert bot._record_conflict_and_check_storm(now=1020.0) is True
        assert len(bot._conflict_timestamps) == 0

        # A single Conflict right after the storm verdict is, on its own,
        # not itself a new storm — the window was cleared.
        assert bot._record_conflict_and_check_storm(now=1020.5) is False
        assert len(bot._conflict_timestamps) == 1

    def test_old_timestamps_are_pruned_before_the_new_one_is_added(self):
        assert bot._record_conflict_and_check_storm(now=1000.0) is False
        assert bot._record_conflict_and_check_storm(now=1005.0) is False
        # This one is 61s after the first — the first should be pruned,
        # leaving only 2 timestamps (not a storm).
        assert bot._record_conflict_and_check_storm(now=1061.0) is False
        assert len(bot._conflict_timestamps) == 2


# ---------------------------------------------------------------------------
# Error-handler wiring (_on_error)
# ---------------------------------------------------------------------------

class TestOnErrorHandler:

    def test_single_conflict_error_does_not_exit(self):
        _run(bot._on_error(None, _ctx_with_error(Conflict('terminated by other getUpdates request'))))
        assert len(bot._conflict_timestamps) == 1

    def test_storm_exits_with_systemexit_and_logs_critical(self, caplog):
        with caplog.at_level(logging.CRITICAL, logger='openclaw'):
            with patch.object(bot, '_send_admin_alert') as mock_alert:
                with pytest.raises(SystemExit) as exc_info:
                    for _ in range(3):
                        _run(bot._on_error(
                            None, _ctx_with_error(Conflict('terminated by other getUpdates request'))))
                assert exc_info.value.code != 0
        assert any('CONFLICT STORM' in rec.message for rec in caplog.records)
        mock_alert.assert_called_once()

    def test_three_conflicts_spread_over_more_than_60s_do_not_exit(self):
        timestamps = [1000.0, 1070.0, 1140.0]
        with patch.object(bot.time, 'time', side_effect=timestamps):
            for _ in timestamps:
                _run(bot._on_error(
                    None, _ctx_with_error(Conflict('terminated by other getUpdates request'))))
        # No SystemExit was raised (loop completed) and the window holds
        # only the most recent timestamp.
        assert len(bot._conflict_timestamps) == 1

    def test_window_resets_after_storm_so_a_lone_conflict_after_exit_does_not_reexit(self):
        with patch.object(bot, '_send_admin_alert'):
            with pytest.raises(SystemExit):
                for _ in range(3):
                    _run(bot._on_error(
                        None, _ctx_with_error(Conflict('terminated by other getUpdates request'))))

        # Window was cleared by the storm verdict — a single subsequent
        # Conflict must NOT immediately re-trigger a SystemExit.
        _run(bot._on_error(None, _ctx_with_error(Conflict('terminated by other getUpdates request'))))
        assert len(bot._conflict_timestamps) == 1

    def test_non_conflict_error_is_logged_and_does_not_touch_the_window(self, caplog):
        with caplog.at_level(logging.ERROR, logger='openclaw'):
            _run(bot._on_error(None, _ctx_with_error(TimedOut())))
        assert len(bot._conflict_timestamps) == 0
        assert any('Unhandled error' in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Admin alert (best-effort, optional channel)
# ---------------------------------------------------------------------------

class TestAdminAlert:

    def test_no_admin_chat_configured_logs_and_skips(self, caplog, monkeypatch):
        monkeypatch.setattr(bot, 'TELEGRAM_ADMIN_CHAT_ID', '')
        with caplog.at_level(logging.WARNING, logger='openclaw'):
            with patch.object(bot.requests, 'post') as mock_post:
                bot._send_admin_alert('test message')
        mock_post.assert_not_called()
        assert any('no TELEGRAM_ADMIN_CHAT_ID' in rec.message for rec in caplog.records)

    def test_admin_chat_configured_sends_best_effort_message(self, monkeypatch):
        monkeypatch.setattr(bot, 'TELEGRAM_ADMIN_CHAT_ID', '999888777')
        with patch.object(bot.requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            bot._send_admin_alert('test message')
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert '/sendMessage' in args[0]
        assert kwargs['json']['chat_id'] == '999888777'
        assert kwargs['json']['text'] == 'test message'

    def test_admin_alert_network_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(bot, 'TELEGRAM_ADMIN_CHAT_ID', '999888777')
        with patch.object(bot.requests, 'post', side_effect=bot.requests.RequestException('boom')):
            bot._send_admin_alert('test message')  # must not raise
