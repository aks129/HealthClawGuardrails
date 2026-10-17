"""
Telegram push helper.

When an event happens server-side that a Telegram user should know about
(e.g., a Fasten ingest finished, a Curatr scan surfaced issues), Flask
calls Telegram's Bot API directly using the same TELEGRAM_BOT_TOKEN
OpenClaw uses for polling. No IPC with OpenClaw is required — the bot
token is the auth, and Telegram delivers the message regardless of which
process is polling.

We never send PHI through this channel. Messages are summary-level only:
counts, status, fixed identifiers (tenant id, task id), and prompts to
run a command (e.g. "type /summary").
"""

import logging
import os

import requests

from models import db
from r6.log_safety import loggable_id
from r6.models import TelegramBinding

logger = logging.getLogger(__name__)

_TELEGRAM_API = 'https://api.telegram.org'
_TIMEOUT = 8.0


def _bot_token() -> str | None:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    return token or None


def is_enabled() -> bool:
    return bool(_bot_token())


def send_to_chat(chat_id: int, message: str, parse_mode: str = 'Markdown') -> bool:
    """Send a single message to a chat. Returns True on Telegram 200."""
    token = _bot_token()
    if not token:
        logger.info('telegram push skipped (no TELEGRAM_BOT_TOKEN)')
        return False
    url = f'{_TELEGRAM_API}/bot{token}/sendMessage'
    try:
        resp = requests.post(
            url,
            json={
                'chat_id': chat_id,
                'text': message,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        logger.warning(
            'telegram send failed chat=%s status=%s body=%s',
            loggable_id(str(chat_id)), resp.status_code, resp.text[:200],
        )
        return False
    except requests.RequestException as exc:
        # The class only: a requests error names the URL, which carries the
        # bot token (FHI-60).
        logger.warning('telegram send error chat=%s: %s',
                       loggable_id(str(chat_id)), type(exc).__name__)
        return False


def notify_tenant(tenant_id: str, message: str, parse_mode: str = 'Markdown') -> int:
    """
    Push a message to every chat bound to a tenant.
    Returns the number of successful sends.
    """
    if not is_enabled():
        return 0
    try:
        chat_ids = TelegramBinding.chat_ids_for_tenant(tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        # The class only: a DB error's text carries its bound parameters.
        logger.warning('telegram tenant lookup failed tenant=%s: %s',
                       loggable_id(tenant_id), type(exc).__name__)
        return 0
    if not chat_ids:
        logger.info('telegram notify skipped — no bindings for tenant=%s',
                    loggable_id(tenant_id))
        return 0
    sent = 0
    for chat_id in chat_ids:
        if send_to_chat(chat_id, message, parse_mode=parse_mode):
            sent += 1
    return sent


def bind(tenant_id: str, chat_id: int, username: str | None = None) -> TelegramBinding:
    """Idempotent bind via the model helper; commits the session."""
    row = TelegramBinding.bind(tenant_id=tenant_id, chat_id=chat_id, username=username)
    db.session.commit()
    return row
