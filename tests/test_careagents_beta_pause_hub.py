"""What a paused, invited or already-connected person sees on the hub
(#856 patient-tester review, V4/G7)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from careagents import beta
from careagents.app import contact_links, create_app
from tests.careagents_stage1_helpers import allowlist_cfg
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)

EMAIL = "gene@example.com"
MAILTO = '<a href="mailto:contactus@healthclaw.io">contactus@healthclaw.io</a>'
HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "static" / "home.js")


def _hub(cfg, svc, monkeypatch):  # noqa: F811
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=EMAIL)
    return c


def _stage1(monkeypatch, **env):
    from careagents.accounts import AccountService
    stage1 = allowlist_cfg(**env)
    accounts = AccountService(stage1)
    return _hub(stage1, accounts, monkeypatch), accounts


def test_a_paused_hub_says_so_and_offers_no_refresh_or_upload(
        cfg, svc, monkeypatch):  # noqa: F811
    c = _hub(cfg, svc, monkeypatch)
    c.post("/api/connections/fasten", json={"consent": True})
    c.post("/api/connections/direct", json={"consent": True})
    for conn in svc.list_home(_acct(svc))["connections"]:
        svc.set_connection_status(conn["tenant_id"], "active")
    before = c.get("/home").get_data(as_text=True)
    assert 'id="hub-paused"' not in before
    assert 'class="conn-refresh"' in before and 'class="conn-upload"' in before

    svc.set_paused(EMAIL, True)
    page = c.get("/home").get_data(as_text=True)
    assert 'id="hub-paused"' in page
    assert "Your records are paused for now." in page
    assert MAILTO in page
    assert 'class="conn-refresh"' not in page
    assert 'class="conn-upload"' not in page
    assert "status-paused" in page and "status-active" not in page
    # Leaving stays possible while paused.
    assert 'class="conn-disconnect"' in page and 'class="conn-delete"' in page


def _acct(svc):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=EMAIL).one().id


def test_an_invited_person_waiting_on_the_terms_is_told_so(monkeypatch):
    c, accounts = _stage1(monkeypatch)
    accounts.invite_real_records(EMAIL, "ops-1")
    page = c.get("/home").get_data(as_text=True)
    assert "You're invited. This opens soon." in page.replace("&#39;", "'")
    assert "Coming later in the beta" not in page


def test_someone_with_real_records_is_not_promised_them_again(monkeypatch):
    """Connected earlier (say, on the environment list), not open now."""
    c, accounts = _stage1(monkeypatch)
    accounts.add_connection(_acct(accounts), "direct", "t-early",
                            "Uploaded records", status="active",
                            consent_version="2026-08-01")
    page = c.get("/home").get_data(as_text=True)
    assert "Coming later in the beta" not in page
    assert "Adding more records isn" in page


@pytest.mark.parametrize("status", ["pending", "empty"])
def test_a_real_connection_with_no_records_yet_is_not_promised_again(
        monkeypatch, status):
    """#856 sign-off F3: a real connection still connecting, or connected
    with nothing in it yet, is a real connection all the same."""
    c, accounts = _stage1(monkeypatch)
    accounts.add_connection(_acct(accounts), "fasten", "t-waiting",
                            "Your doctor's records", status=status,
                            consent_version="2026-08-01")
    page = c.get("/home").get_data(as_text=True)
    assert "Coming later in the beta" not in page
    assert "Adding more records isn" in page
    # The beta banner still says "connected" only for an active one.
    assert "your records are connected" not in page


def test_an_uninvited_newcomer_still_sees_what_is_coming(monkeypatch):
    c, _ = _stage1(monkeypatch)
    assert "Coming later in the beta" in c.get("/home").get_data(
        as_text=True)


def test_a_paused_hub_shows_no_terms_prompt_and_no_invite_line(monkeypatch):
    c, accounts = _stage1(monkeypatch)
    accounts.invite_real_records(EMAIL, "ops-1")
    accounts.set_paused(EMAIL, True)
    page = c.get("/home").get_data(as_text=True).replace("&#39;", "'")
    assert "You're invited" not in page
    assert "Coming later in the beta" not in page
    assert 'data-reconsent=' not in page


def test_a_paused_hub_offers_no_made_up_records(monkeypatch):
    """#856 sign-off F2: the pause line says nothing new can be added, so
    the hub offers no sample to add either."""
    c, accounts = _stage1(monkeypatch)
    before = c.get("/home").get_data(as_text=True)
    assert 'id="explore-sample"' in before
    accounts.set_paused(EMAIL, True)
    page = c.get("/home").get_data(as_text=True)
    assert 'id="hub-paused"' in page
    assert "Explore with made-up records" not in page
    assert 'data-connector="sample"' not in page
    # Nothing is listed under "Add records", so no heading over nothing.
    assert 'id="connect-section"' in before and "Add records" in before
    assert 'id="connect-section"' not in page
    assert "Add records" not in page
    # And no line pointing at the section that is not there.
    assert "Add some below" in before
    assert "Add some below" not in page and "No records yet." in page


def test_contact_links_escapes_everything_but_the_address():
    out = str(contact_links("<b>x</b> write to contactus@healthclaw.io."))
    assert out == "&lt;b&gt;x&lt;/b&gt; write to " + MAILTO + "."


def test_refresh_shows_the_sentence_not_the_code():
    src = HOME_JS.read_text()
    handler = src.split('document.querySelectorAll(".conn-refresh")', 1)[1]
    handler = handler.split("});\n  });", 1)[0]
    assert "res.d.message || res.d.error" in handler
    assert "report(res.d.error ||" not in handler


_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cut(name) {
  const m = src.match(new RegExp('const ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
const f = new Function(cut('UPLOAD_MSG') + cut('UPLOAD_SUPPORT')
  + cut('uploadErrorLine') + 'return uploadErrorLine;')();
process.stdout.write(JSON.stringify({
  paused: f('records_paused', ''),
  pausedCode: f('records_paused', 'c-1'),
  unknown: f('weird_code', ''),
  unknownCode: f('weird_code', 'c-2'),
  commit: f('commit_failed', ''),
  commitCode: f('commit_failed', 'c-3'),
  tooBig: f('payload_too_large', 'c-4'),
}));
"""


def _upload_lines() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", _HARNESS, "--", str(HOME_JS)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_upload_while_paused_says_paused():
    got = _upload_lines()
    assert got["paused"] == beta.PAUSED_RECORDS_TEXT
    assert got["pausedCode"] == beta.PAUSED_RECORDS_TEXT


def test_no_upload_line_promises_a_code_it_does_not_show():
    got = _upload_lines()
    for key in ("unknown", "commit"):
        assert "code" not in got[key], got[key]
        assert got[key].endswith("write to contactus@healthclaw.io.")
    assert got["unknownCode"].endswith("quote this code: c-2.")
    assert got["commitCode"].endswith("quote this code: c-3.")
    assert "c-4" not in got["tooBig"]
