"""Plain words (calm hub spec section 7)."""

from __future__ import annotations

import pathlib
import re

from careagents import connectors
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture

_CA = pathlib.Path(__file__).resolve().parents[1] / "careagents"


def _banner(html):
    sentence = re.search(r'class="beta-banner"[^>]*>(.*?)</p>', html,
                         re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", "", sentence).split())


def test_the_hub_banner_matches_the_account(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: sample records. Things will break, tell us.")
    with c.session_transaction() as s:
        aid = s["account_id"]
    svc.add_connection(aid, "direct", "ca-real", "Upload", status="active")
    assert _banner(c.get("/home").get_data(as_text=True)) == (
        "Beta: your records are connected. Things will break, tell us.")


def test_the_landing_banner_keeps_its_words_without_the_em_dash(app):
    assert _banner(app.test_client().get("/").get_data(as_text=True)) == (
        "Beta: synthetic records only. Things will break, tell us.")


def test_the_stale_telegram_handler_is_gone():
    js = (_CA / "static" / "home.js").read_text()
    assert "tg-surface" not in js and "tg-state" not in js


def test_imessage_instructions_name_no_deployment():
    src = (_CA / "app.py").read_text()
    assert "iMessage isn't configured on this deployment" not in src


def test_a_new_provider_connection_is_named_in_plain_words(cfg):
    plan = connectors.start("fasten", None, cfg, FakeClient(),
                            real_records=True)
    assert plan["label"] == "Records from your doctor"
    # chat.html reads "Your records from {{ intake.provider }} haven't
    # arrived yet", so the provider must read after "from".
    assert plan["provider"] == "your doctor"


def test_the_sample_refresh_reason_has_no_em_dash(cfg):
    """Patient copy is em-dash-free (QA, calm hub PR 2)."""
    out = connectors.refresh("sample", "t-1", None, cfg, FakeClient())
    assert out["unsupported"] is True
    assert "—" not in out["reason"]
    assert out["reason"].startswith("Sample records are made up")


def test_the_waiting_chat_notice_reads_as_a_sentence(cfg, svc, monkeypatch):
    class _NotYet(FakeClient):
        def search(self, tenant, resource_type, params=None):
            return {"total": 0, "entry": []}

    from careagents.app import create_app
    a = create_app(config=cfg, client=_NotYet(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    _login(c, svc, monkeypatch)
    conn = c.post("/api/connections/fasten", json={"consent": True}).get_json()
    with c.session_transaction() as s:
        aid = s["account_id"]
    agent = svc.create_agent(aid, "Juniper", "calm", conn["id"])
    page = " ".join(c.get(f"/chat?agent={agent}").get_data(as_text=True).split())
    assert "Your records from your doctor haven't arrived yet" in page
    assert "from Records from" not in page
