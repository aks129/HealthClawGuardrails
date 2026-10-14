"""Real mail leaves only production (#874 round 3, item 12).

The `flask` CLI loads the repository's .env, which can hold a real Resend
key, and local runs emailed real addresses three times. Outside
production the mail module logs "mail suppressed (not production)" and
never reaches the provider, unless CARE_ALLOW_REAL_MAIL=1.
"""

from __future__ import annotations

import logging

import pytest

from careagents import mail
from careagents.config import Config

_BASE = {"CARE_RP_ID": "localhost", "CARE_ORIGIN": "http://localhost",
         "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "m",
         "RESEND_API_KEY": "re_real_looking_key"}


@pytest.fixture
def calls(monkeypatch):
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):
        out.append(json["to"])

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(mail.requests, "post", fake_post)
    return out


def _send_all(cfg):
    return (mail.send_code(cfg, "a@example.com", "12345678", "verify"),
            mail.send_notice(cfg, "a@example.com", "S", "line"),
            mail.send_message(cfg, "a@example.com", "S", "<p>x</p>", "x"))


@pytest.mark.parametrize("env", [{}, {"CARE_ENV": "development"},
                                 {"CARE_ENV": "staging"},
                                 {"CARE_ALLOW_REAL_MAIL": "true"},
                                 {"CARE_ALLOW_REAL_MAIL": "0"}])
def test_outside_production_nothing_reaches_the_provider(calls, caplog,
                                                          env):
    caplog.set_level(logging.WARNING, logger="careagents.mail")
    code, notice, message = _send_all(Config(env={**_BASE, **env}))
    assert calls == []
    # The code is still usable locally: it is in the log, as without a key.
    assert code == mail.SENT and "12345678" in caplog.text
    assert notice == mail.NOT_SENT and message == mail.NOT_SENT
    assert caplog.text.count("mail suppressed (not production)") == 3


def test_the_override_lets_a_local_run_send(calls):
    _send_all(Config(env={**_BASE, "CARE_ALLOW_REAL_MAIL": "1"}))
    assert len(calls) == 3


def test_production_sends(calls):
    class Prod:                    # Config(production) also demands secrets
        app_env = "production"
        resend_api_key = "re_real_looking_key"
        resend_from = "CareAgents <hello@careagents.cloud>"
    _send_all(Prod())
    assert len(calls) == 3


def test_unset_is_not_production():
    assert Config(env=_BASE).app_env == "development"
    assert not mail.real_mail_allowed(Config(env=_BASE))
