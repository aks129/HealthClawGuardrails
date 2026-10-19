"""Security review of the check-in emails (PR #992): gaps the mutation run
found. Synthetic data only."""

from __future__ import annotations

import hashlib
import hmac
import logging

from careagents import checkins, mail
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    cfg, svc)
from tests.test_careagents_checkins import _aged, _cfg


def test_a_send_that_raises_logs_no_address(svc, monkeypatch, caplog):  # noqa: F811
    """The `send raised` branch: the exception text carries the address."""
    caplog.set_level(logging.DEBUG)
    _aged(svc, monkeypatch, "raiser@example.com", 3)

    def boom(*_a, **_k):
        raise RuntimeError("could not send to raiser@example.com")
    monkeypatch.setattr(mail, "send_message", boom)
    assert checkins.send_due(svc.session, _cfg(svc))["not_sent"] == 1
    assert "checkin send raised" in caplog.text
    assert "@" not in caplog.text


def test_the_stop_key_is_not_the_session_secret(svc):  # noqa: F811
    """A plain HMAC of an account id under the session secret (as any
    other signer might make) is not a stop token."""
    c = _cfg(svc)
    plain = hmac.new(c.session_secret.encode(), b"acct_0123456789abcdef",
                     hashlib.sha256).hexdigest()
    assert checkins.read_token(c, f"acct_0123456789abcdef.{plain}") is None
    good = checkins.stop_token(c, "acct_0123456789abcdef")
    assert checkins.read_token(c, good) == "acct_0123456789abcdef"
