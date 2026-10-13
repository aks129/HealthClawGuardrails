"""The hub around outcomes (hub critique, 2026-10): ask first, then what
waits, what's new, where to reach the assistant, and the sources last.

Pure view rules are tested on careagents.hub; the page tests read the
rendered /home. No PHI: synthetic labels and counts only.
"""

from __future__ import annotations

import html
import re
import time

from careagents import hub as hub_view
from careagents.healthclaw import HealthClawError
from careagents.tester_terms import CONSENT_VERSION
from tests.test_careagents import FakeClient, _login
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture

DAY = 86400


def _signed_in(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    with c.session_transaction() as s:
        return c, s["account_id"]


def _conn(svc, aid, kind, tenant, label, status, count=None, age_days=30,
          provider=None):
    cid = svc.add_connection(
        aid, kind, tenant, label, status=status, provider=provider,
        consent_version=None if kind == "sample" else CONSENT_VERSION)
    from careagents.models import Connection
    with svc.session() as s:
        c = s.get(Connection, cid)
        c.connected_at = time.time() - age_days * DAY
        c.last_count = count
    return cid


def _section(body, sid):
    start = body.index(f'id="{sid}"')
    start = body.rindex("<section", 0, start)
    return body[start:body.index("</section>", start)]


def _row(body, conn_id):
    at = body.index(f'data-conn="{conn_id}" data-kind')
    start = body.rindex('<div class="hub-card conn-card"', 0, at)
    return body[start:body.index("conn-refresh-msg", at)]


def _list(body):
    """The visible source list: rows outside the made-up drawer."""
    sec = _section(body, "records-section")
    if 'class="made-up-sources"' in sec:
        sec = sec[:sec.index('class="made-up-sources"')]
    return sec


# --- 1. order ---------------------------------------------------------------

def test_the_hub_reads_ask_waiting_new_reach_sources_add(app, svc,
                                                         monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    real = _conn(svc, aid, "fasten", "ca-o1", "Records from your doctor",
                 "active", count=152, age_days=1)
    svc.create_agent(aid, "Rowan", "calm", real)
    body = c.get("/home").get_data(as_text=True)
    order = [body.index(f'id="{s}"') for s in (
        "ask-section", "waiting", "whats-new", "reach-section",
        "records-section", "connect-section")]
    assert order == sorted(order)
    assert "<h2>Ask your assistant</h2>" in body
    assert "<h2>Waiting for your OK</h2>" in body
    assert "<h2>Your sources</h2>" in body
    assert "<h2>Add a source</h2>" in body


# --- 2. what's new ----------------------------------------------------------

def test_whats_new_is_left_out_when_nothing_real_changed(app, svc,
                                                         monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    # Made-up records just arrived, and a real source that is old news.
    _conn(svc, aid, "sample", "ca-o2", "Sample records", "active",
          count=100, age_days=0)
    _conn(svc, aid, "fasten", "ca-o3", "Records from your doctor", "active",
          count=152, age_days=20)
    body = c.get("/home").get_data(as_text=True)
    assert 'id="whats-new"' not in body
    assert "What's new" not in body


def test_whats_new_names_a_new_real_source_and_its_count(app, svc,
                                                         monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    _conn(svc, aid, "fasten", "ca-o4", "Records from your doctor", "active",
          count=152, age_days=2)
    news = _section(c.get("/home").get_data(as_text=True), "whats-new")
    assert "Records from your doctor: 152 records arrived." in news


def test_whats_new_never_counts_a_source_still_connecting_or_empty(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    _conn(svc, aid, "fasten", "ca-o5", "Records from your doctor", "pending",
          age_days=0)
    _conn(svc, aid, "direct", "ca-o6", "Uploaded records", "empty",
          age_days=0)
    _conn(svc, aid, "direct", "ca-o7", "Uploaded records", "active",
          count=0, age_days=0)
    assert 'id="whats-new"' not in c.get("/home").get_data(as_text=True)


def test_the_switch_question_lives_in_whats_new_and_is_not_said_twice(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-o8", "Sample records", "active",
                   count=100)
    _conn(svc, aid, "fasten", "ca-o9", "Records from your doctor", "active",
          count=152, age_days=1)
    svc.create_agent(aid, "Juniper", "calm", sample)
    news = _section(c.get("/home").get_data(as_text=True), "whats-new")
    assert 'id="switch-prompt"' in news
    assert "152 records arrived" in news
    assert news.count("152 records") == 1


# --- 3. one status per source -------------------------------------------------

def test_status_chip_is_the_one_word_a_row_shows():
    active = {"status": "active", "kind": "fasten"}
    pending = {"status": "pending", "kind": "fasten"}
    assert hub_view.status_chip(active, None, False) == {
        "cls": "active", "word": "Connected"}
    assert hub_view.status_chip(pending, None, False) == {
        "cls": "pending", "word": "Connecting…"}
    # A failed import keeps the pending class (the poller watches it) but
    # never says "Connecting…".
    assert hub_view.status_chip(pending, {"state": "failed"}, False) == {
        "cls": "pending", "word": "Didn't finish"}
    assert hub_view.status_chip(active, None, True) == {
        "cls": "paused", "word": "Paused"}


def test_the_subtitle_is_never_a_status_word():
    def sub(provider, label="My ring"):
        return hub_view.subtitle({"provider": provider, "label": label})
    assert sub("Oura") == "Oura"
    assert sub("your doctor", "Records from your doctor") == ""
    for word in hub_view.STATUS_WORDS.values():
        assert sub(word) == ""


def test_every_source_row_carries_exactly_one_status(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    ids = [_conn(svc, aid, "fasten", "ca-s1", "Records from your doctor",
                 "active", count=152, provider="Connecting…"),
           _conn(svc, aid, "fasten", "ca-s2", "Records from your doctor",
                 "pending"),
           _conn(svc, aid, "direct", "ca-s3", "Uploaded records", "empty")]
    body = c.get("/home").get_data(as_text=True)
    for cid in ids:
        row = _row(body, cid)
        assert len(re.findall(r'class="status status-', row)) == 1
        text = re.sub(r"<[^>]+>", " ", row)
        assert not ("Connected" in text and "Connecting" in text), cid


def test_a_failed_import_row_never_says_connecting(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    cid = _conn(svc, aid, "fasten", "ca-s4", "Records from your doctor",
                "pending")
    monkeypatch.setattr(FakeClient, "fasten_import_state",
                        lambda self, t: "failed")
    row = html.unescape(_row(c.get("/home").get_data(as_text=True), cid))
    assert "Didn't finish" in row
    assert "Connecting" not in re.sub(r"<[^>]+>", " ", row)


# --- 4. actions live in a menu ------------------------------------------------

def test_refresh_disconnect_and_delete_live_in_the_rows_menu(app, svc,
                                                             monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    cid = _conn(svc, aid, "fasten", "ca-m1", "Records from your doctor",
                "active", count=152)
    row = _row(c.get("/home").get_data(as_text=True), cid)
    menu = row[row.index('<details class="card-menu source-menu"'):]
    menu = menu[:menu.index("</details>")]
    for cls in ("conn-refresh", "conn-disconnect", "conn-delete"):
        assert f'class="{cls}"' in menu
        assert row.count(f'class="{cls}"') == 1
    # The menu says whose it is, for a screen reader.
    assert 'aria-label="More for Records from your doctor"' in menu


def test_an_upload_row_keeps_its_one_job_outside_the_menu(app, svc,
                                                          monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    cid = _conn(svc, aid, "direct", "ca-m2", "Uploaded records", "empty")
    row = _row(c.get("/home").get_data(as_text=True), cid)
    assert row.index('class="conn-upload"') < row.index("source-menu")


# --- 5. made-up sources step aside --------------------------------------------

def test_samples_hide_once_a_real_source_has_records(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-h1", "Sample records", "active",
                   count=100)
    real = _conn(svc, aid, "fasten", "ca-h2", "Records from your doctor",
                 "active", count=152)
    body = c.get("/home").get_data(as_text=True)
    assert f'data-conn="{real}"' in _list(body)
    assert f'data-conn="{sample}"' not in _list(body)
    # Still reachable, quietly, so it can be deleted.
    drawer = _section(body, "records-section")
    drawer = drawer[drawer.index('class="made-up-sources"'):]
    assert f'data-conn="{sample}"' in drawer
    assert "Explore with made-up records" in body


def test_samples_stay_while_the_real_source_is_still_connecting(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-h3", "Sample records", "active",
                   count=100)
    _conn(svc, aid, "fasten", "ca-h4", "Records from your doctor", "pending")
    _conn(svc, aid, "direct", "ca-h5", "Uploaded records", "active", count=0)
    body = c.get("/home").get_data(as_text=True)
    assert f'data-conn="{sample}"' in _list(body)
    assert 'class="made-up-sources"' not in body


def test_the_sample_rule_on_the_view():
    now = time.time()

    def row(kind, status, count):
        return {"id": f"{kind}-{status}-{count}", "kind": kind,
                "status": status, "last_count": count, "label": kind,
                "tenant_id": "t", "connected_at": now, "provider": None}
    home = {"agents": [], "connections": [
        row("sample", "active", 100), row("fasten", "active", None)]}
    view = hub_view.build(home, now)
    # Active but not yet counted is still records: None is never zero.
    assert [r["kind"] for r in view["sources"]] == ["fasten"]
    assert [r["kind"] for r in view["made_up_sources"]] == ["sample"]


# --- 6. empty duplicate uploads ----------------------------------------------

def test_empty_duplicate_uploads_show_once(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    first = _conn(svc, aid, "direct", "ca-d1", "Uploaded records", "empty",
                  age_days=6)
    second = _conn(svc, aid, "direct", "ca-d2", "Uploaded records", "empty",
                   age_days=4)
    body = c.get("/home").get_data(as_text=True)
    assert f'data-conn="{first}"' in _list(body)
    assert f'data-conn="{second}"' not in body


def test_an_empty_upload_an_assistant_reads_is_kept(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    _conn(svc, aid, "direct", "ca-d3", "Uploaded records", "empty",
          age_days=6)
    read = _conn(svc, aid, "direct", "ca-d4", "Uploaded records", "empty",
                 age_days=4)
    svc.create_agent(aid, "Rowan", "calm", read)
    assert f'data-conn="{read}"' in _list(c.get("/home").get_data(
        as_text=True))


# --- 7. where you can reach me ------------------------------------------------

def test_reach_says_web_and_points_imessage_at_settings(app, svc,
                                                        monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-r1", "Sample records", "active",
                   count=100)
    svc.create_agent(aid, "Juniper", "calm", sample)
    reach = _section(c.get("/home").get_data(as_text=True), "reach-section")
    assert "<h2>Where you can reach me</h2>" in reach
    assert "Here on the web" in reach
    assert 'href="/settings#reach"' in reach
    # No email channel exists, so none is offered.
    assert "mailto:" not in reach and "Email" not in reach
    # The hub never binds the Settings control twice.
    assert 'id="im-surface"' not in reach


def test_reach_shows_a_connected_phone_masked(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-r2", "Sample records", "active",
                   count=100)
    agent = svc.create_agent(aid, "Juniper", "calm", sample)
    from careagents.models import Surface
    with svc.session() as s:
        s.add(Surface(account_id=aid, agent_id=agent, kind="imessage",
                      handle="+15550104321", status="active"))
    reach = _section(c.get("/home").get_data(as_text=True), "reach-section")
    assert "+15550104321" not in reach
    assert "4321" in reach


# --- 8. chat starters tuned to the records ------------------------------------

INTAKE = "Fill out my intake form for a new doctor"
TUNED = ["What changed since my last visit?", "Make my visit brief",
         "Explain my latest labs"]


def _starters(page):
    block = page[page.index('id="starters"'):]
    block = block[:block.index("</div>")]
    return re.findall(r'class="starter">([^<]*)<', block)


def _chat(app, svc, monkeypatch, totals):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = _conn(svc, aid, "fasten", "ca-c1", "Records from your doctor",
                 "active", count=sum(totals.values()) or None)
    agent = svc.create_agent(aid, "Rowan", "calm", conn)

    def search(self, tenant, rt, params=None):
        if rt not in totals:
            raise HealthClawError("down", 503)
        return {"total": totals[rt]}
    monkeypatch.setattr(FakeClient, "search", search)
    return c, agent


def test_every_chat_offers_the_three_tuned_starters(app, svc, monkeypatch):
    c, agent = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 12})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED]


def test_no_labs_means_no_labs_starter(app, svc, monkeypatch):
    c, agent = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 0})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED[:2]]


def test_an_unknown_lab_count_hides_the_labs_starter(app, svc, monkeypatch):
    c, agent = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "Explain my latest labs" not in _starters(page)


def test_a_returning_chat_knows_its_labs_too(app, svc, monkeypatch):
    c, agent = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 12})
    monkeypatch.setattr(
        FakeClient, "recent_messages",
        lambda self, *a, **k: [{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "hello"}])
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED]


def test_empty_records_offer_only_the_intake_starter(app, svc, monkeypatch):
    c, agent = _chat(app, svc, monkeypatch, {
        "Condition": 0, "MedicationRequest": 0, "Observation": 0})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE]
