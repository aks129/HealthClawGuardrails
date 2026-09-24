"""The dev mail stub's log line is an interface, not a message (#263).

With no RESEND_API_KEY, `careagents.mail.send_code` logs the one-time code
instead of sending it. That line is the only test-mode sign-in path, and three
consumers read it: the Playwright fixture (`e2e/tests/careagents-fixtures.ts`,
`codeFromLog`), `scripts/beta_acceptance.py` (`CODE_RE`), and people running
the stack locally. Reword it and the browser suite fails as a ten-second poll
timeout, not as a named failure. This file is the named failure.

`CODE_RE` is imported from the acceptance script rather than copied, so this
test and that consumer cannot drift apart. The Playwright pattern lives in
TypeScript and cannot be imported; `E2E_RE` below mirrors it and must be
edited together with `careagents-fixtures.ts`.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from beta_acceptance import CODE_RE  # noqa: E402

from careagents import mail  # noqa: E402

EMAIL = "format-guard@example.test"
CODE = "04918273"  # eight digits with a leading zero, as accounts.py mints


def _e2e_re(email):
    # Mirror of codeFromLog in e2e/tests/careagents-fixtures.ts:
    #   `DEV email — [^\\n]* for ${escapeRe(email)}: (\\d{8})`
    return re.compile(rf"DEV email — [^\n]* for {re.escape(email)}: (\d{{8}})")


def _logged_line(caplog, monkeypatch, purpose):
    def _no_network(*a, **kw):
        raise AssertionError("the dev stub must not call Resend")

    monkeypatch.setattr(requests, "post", _no_network)
    cfg = SimpleNamespace(resend_api_key=None, resend_from="x@example.test")
    with caplog.at_level(logging.WARNING, logger="careagents.mail"):
        outcome = mail.send_code(cfg, EMAIL, CODE, purpose)
    assert outcome == mail.SENT
    lines = [r.getMessage() for r in caplog.records
             if r.name == "careagents.mail"]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.parametrize("purpose", ["verify", "login"])
def test_dev_code_line_matches_beta_acceptance_code_re(
        caplog, monkeypatch, purpose):
    line = _logged_line(caplog, monkeypatch, purpose)
    m = CODE_RE.search(line)
    assert m, (f"scripts/beta_acceptance.py CODE_RE no longer matches the "
               f"dev mail log line {line!r} — update both together (#263)")
    assert m.group("email") == EMAIL
    assert m.group("code") == CODE


@pytest.mark.parametrize("purpose", ["verify", "login"])
def test_dev_code_line_matches_playwright_code_from_log(
        caplog, monkeypatch, purpose):
    line = _logged_line(caplog, monkeypatch, purpose)
    m = _e2e_re(EMAIL).search(line)
    assert m, (f"e2e/tests/careagents-fixtures.ts codeFromLog no longer "
               f"matches the dev mail log line {line!r} — the CareAgents "
               f"browser suite will time out waiting for a code (#263)")
    assert m.group(1) == CODE
