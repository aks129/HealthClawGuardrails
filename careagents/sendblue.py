"""Sendblue's HTTP API, and nothing else: send a text, show typing.

The surface logic (webhook, delivery) is careagents/sendblue_surface.py.
Every call has a timeout. A 429, a 5xx or a network error is retried once
after a pause, never more, so a throttled line does not become a retry
storm. A 400 is not retried: 4007-4010 are Sendblue's limits on a contact
who has not replied yet, and asking again does not lift them.

Logs carry the status, Sendblue's error code and a masked number. Never the
content, the full number, or Sendblue's error_message (it can echo the
number).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from careagents.imessage import mask

logger = logging.getLogger(__name__)

#: Sendblue's limit on one message body.
MAX_CONTENT = 18996
#: Sendblue's error codes for a contact who has not replied yet.
PRE_REPLY_LIMIT_CODES = frozenset({4007, 4008, 4009, 4010})
TIMEOUT_SECONDS = 10
RETRY_PAUSE_SECONDS = 2.0


@dataclass
class SendResult:
    ok: bool
    status: int                    # HTTP status; 0 for a network failure
    error_code: int | None = None
    retried: bool = False


def _error_code(body: dict) -> int | None:
    try:
        return int(body.get("error_code"))
    except (TypeError, ValueError):
        return None


class Client:
    def __init__(self, cfg, http=None, sleep=time.sleep):
        self.cfg = cfg
        self.http = http or requests.Session()
        self.sleep = sleep

    def _headers(self) -> dict:
        return {"sb-api-key-id": self.cfg.sendblue_api_key_id,
                "sb-api-secret-key": self.cfg.sendblue_api_secret,
                "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        """(status, body). A network failure is (0, {})."""
        try:
            r = self.http.post(f"{self.cfg.sendblue_api_base}{path}",
                               json=payload, headers=self._headers(),
                               timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("sendblue %s failed: %s", path, type(exc).__name__)
            return 0, {}
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}

    def send_message(self, number: str, content: str) -> SendResult:
        payload = {"number": number,
                   "from_number": self.cfg.sendblue_from_number,
                   "content": content[:MAX_CONTENT]}
        retried = False
        while True:
            status, body = self._post("/api/send-message", payload)
            code = _error_code(body)
            if 200 <= status < 300 and str(
                    body.get("status") or "").upper() != "ERROR":
                return SendResult(True, status, None, retried)
            transient = status == 0 or status == 429 or status >= 500
            if transient and not retried:
                logger.info("sendblue send to %s got %s; retrying once",
                            mask(number), status or "no answer")
                self.sleep(RETRY_PAUSE_SECONDS)
                retried = True
                continue
            if code in PRE_REPLY_LIMIT_CODES:
                logger.warning("sendblue send to %s refused: pre-reply limit "
                               "(error %s)", mask(number), code)
            else:
                logger.warning("sendblue send to %s failed: HTTP %s error %s",
                               mask(number), status, code)
            return SendResult(False, status, code, retried)

    def send_typing(self, number: str) -> bool:
        """Best effort, iMessage only. Never retried, never raises."""
        status, _ = self._post("/api/send-typing-indicator", {
            "number": number,
            "from_number": self.cfg.sendblue_from_number,
            "state": "start"})
        return 200 <= status < 300
