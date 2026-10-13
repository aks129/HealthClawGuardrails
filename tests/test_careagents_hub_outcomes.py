"""The hub around outcomes (hub critique, 2026-10): ask first, then what
waits, what's new, where to reach the assistant, and the sources last.

Pure view rules are tested on careagents.hub; the page tests read the
rendered /home. No PHI: synthetic labels and counts only.
"""

from __future__ import annotations

import html
import pathlib
import re
import time

from careagents import hub as hub_view
from careagents.healthclaw import HealthClawError
from careagents.tester_terms import CONSENT_VERSION
from tests.test_careagents import FakeClient, _login, consented
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
    assert sub("connecting...") == "" and sub("CONNECTED") == ""


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


# --- 6. empty uploads ----------------------------------------------------------

def test_legacy_empty_uploads_all_show_and_differ(app, svc, monkeypatch):
    """Nothing is hidden (patient tester on #944): a hidden twin made
    Delete look like it did nothing. The reuse in _start_connection means
    no new twins are made; old ones show, each with when it was added."""
    c, aid = _signed_in(app, svc, monkeypatch)
    first = _conn(svc, aid, "direct", "ca-d1", "Uploaded records", "empty",
                  age_days=6)
    second = _conn(svc, aid, "direct", "ca-d2", "Uploaded records", "empty",
                   age_days=4)
    body = c.get("/home").get_data(as_text=True)
    subs = [re.search(r'<div class="hub-card-sub">([^<]*)</div>',
                      _row(body, x)).group(1) for x in (first, second)]
    assert f'data-conn="{first}"' in _list(body)
    assert f'data-conn="{second}"' in _list(body)
    assert subs[0] != subs[1] and all("Added" in s for s in subs)


def test_a_second_upload_tap_reuses_the_empty_row(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    a = c.post("/api/connections/direct", json=consented()).get_json()
    b = c.post("/api/connections/direct", json=consented()).get_json()
    assert b["id"] == a["id"] and b["existing"] is True
    assert [x["kind"] for x in svc.list_home(aid)["connections"]] == [
        "direct"]
    # Consent is still asked for on the reused row.
    assert c.post("/api/connections/direct", json={}).status_code == 428


# --- 7. where you can reach your assistant ------------------------------------

def test_reach_says_web_and_points_imessage_at_settings(app, svc,
                                                        monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-r1", "Sample records", "active",
                   count=100)
    svc.create_agent(aid, "Juniper", "calm", sample)
    reach = _section(c.get("/home").get_data(as_text=True), "reach-section")
    # The same heading as Settings (patient tester on #944).
    assert "<h2>Where you can reach your assistant</h2>" in reach
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
#: "What changed since my last visit?" is gone: records hold no visit to
#: compare with, so it repeated the brief (patient tester on #944).
TUNED = ["Make my visit brief", "Explain my latest labs"]


def _starters(page):
    block = page[page.index('id="starters"'):]
    block = block[:block.index("</div>")]
    return re.findall(r'class="starter">([^<]*)<', block)


def _chat(app, svc, monkeypatch, totals, labs=1, kind="fasten"):
    """A chat whose engine answers counts only: `labs` is the count of
    Observations filed as laboratory (code=laboratory); absent from
    `totals` means the search fails."""
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = _conn(svc, aid, kind, "ca-c1", "Records from your doctor",
                 "active", count=sum(totals.values()) or None)
    agent = svc.create_agent(aid, "Rowan", "calm", conn)
    seen = []

    def search(self, tenant, rt, params=None):
        seen.append(dict(params or {}))
        if rt not in totals:
            raise HealthClawError("down", 503)
        assert (params or {}).get("_summary") == "count", \
            "only a total may cross for the starters"
        if (params or {}).get("code") == "laboratory":
            return {"total": labs}
        return {"total": totals[rt]}
    monkeypatch.setattr(FakeClient, "search", search)
    return c, agent, seen


def test_every_chat_offers_the_tuned_starters(app, svc, monkeypatch):
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 12})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED]
    assert "What changed since my last visit?" not in page


def test_no_observations_means_no_labs_starter(app, svc, monkeypatch):
    c, agent, seen = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 0})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, TUNED[0]]
    # Counted at zero: nothing more is asked.
    assert not any(p.get("code") == "laboratory" for p in seen)


def test_an_unknown_lab_count_hides_the_labs_starter(app, svc, monkeypatch):
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "Explain my latest labs" not in _starters(page)


def test_a_returning_chat_knows_its_labs_too(app, svc, monkeypatch):
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 12})
    monkeypatch.setattr(
        FakeClient, "recent_messages",
        lambda self, *a, **k: [{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "hello"}])
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED]


def test_empty_records_offer_only_the_intake_starter(app, svc, monkeypatch):
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 0, "MedicationRequest": 0, "Observation": 0})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE]


# --- QA #944 F2: labs means laboratory results, not vital signs --------------

def test_vital_signs_alone_offer_no_labs_starter(app, svc, monkeypatch):
    """150 heart rates and no Observation filed as a lab."""
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 0, "MedicationRequest": 0, "Observation": 150}, labs=0)
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, TUNED[0]]


def test_the_made_up_records_offer_labs(app, svc, monkeypatch):
    """The sample seed holds A1c and glucose results and files none by
    category, so the sample is not asked for a laboratory count."""
    c, agent, seen = _chat(app, svc, monkeypatch, {
        "Condition": 1, "MedicationRequest": 1, "Observation": 9}, labs=0,
        kind="sample")
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE, *TUNED]
    assert not any(p.get("code") == "laboratory" for p in seen)


# --- QA #944 F1: a second Upload tap goes to the row already there ----------

def test_the_upload_tile_opens_the_waiting_rows_picker_in_the_tap():
    """No consent card and no request when an empty upload row is shown:
    the tap is that row's Upload, so the picker opens inside it."""
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
          / "static" / "home.js").read_text()
    tile = js[js.index('document.querySelectorAll(".connector-tile")'):]
    tile = tile[:tile.index("showConsentCard")]
    assert ('.conn-card[data-kind="direct"][data-status="empty"] '
            '.conn-upload') in tile
    assert "waiting.click()" in tile


# --- QA #944 F3: one line per source, and a switch prompt that is stable ----

def test_two_new_sources_are_each_said_once(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-f1", "Sample records", "active",
                   count=100, age_days=30)
    old = _conn(svc, aid, "fasten", "ca-f2", "Records from your doctor",
                "active", count=40, age_days=5)
    new = _conn(svc, aid, "fasten", "ca-f3", "Records from your doctor",
                "active", count=7, age_days=1)
    svc.create_agent(aid, "Juniper", "calm", sample)
    body = c.get("/home").get_data(as_text=True)
    news = _section(body, "whats-new")
    # The switch question names the newest arrival; the older one gets
    # its own line; neither is said twice.
    assert f'data-conn="{new}"' in news
    assert news.count("7 records") == 1 and news.count("40 records") == 1
    assert old != new


def test_the_switch_prompt_picks_the_newest_real_source():
    now = time.time()
    home = {"agents": [{"id": "a1", "name": "Juniper",
                        "connection_id": "s"}],
            "connections": [
                {"id": "s", "kind": "sample", "status": "active",
                 "label": "Sample records", "connected_at": now - 50 * DAY},
                {"id": "old", "kind": "fasten", "status": "active",
                 "label": "R", "connected_at": now - 9 * DAY,
                 "last_count": 40},
                {"id": "new", "kind": "fasten", "status": "active",
                 "label": "R", "connected_at": now - DAY, "last_count": 7}]}
    assert hub_view.switch_prompt(home)["connection_id"] == "new"


# --- QA #944 F4: rows with the same name can be told apart ------------------

def test_rows_with_the_same_name_say_when_each_was_added(app, svc,
                                                            monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    a = _conn(svc, aid, "fasten", "ca-n1", "Records from your doctor",
              "active", count=40, age_days=12)
    b = _conn(svc, aid, "fasten", "ca-n2", "Records from your doctor",
              "pending", age_days=3)
    solo = _conn(svc, aid, "wearable", "ca-n3", "Oura", "active", count=4,
                 provider="Oura")
    body = c.get("/home").get_data(as_text=True)
    subs = [re.search(r'<div class="hub-card-sub">([^<]*)</div>',
                      _row(body, x)).group(1) for x in (a, b, solo)]
    assert subs[0].startswith("Added ") and subs[1].startswith(
        "Added ")
    assert subs[0] != subs[1]
    assert subs[2] == ""                # a unique name needs no date


def test_same_day_twins_still_differ():
    now = time.time()

    def row(i):
        return {"id": f"c{i}", "kind": "fasten", "status": "active",
                "label": "Records from your doctor", "tenant_id": "t",
                "connected_at": now - 3600 * i, "last_count": 5,
                "provider": "your doctor"}
    view = hub_view.build({"agents": [], "connections": [row(1), row(2)]},
                          now)
    subs = [r["sub"] for r in view["sources"]]
    assert len(set(subs)) == 2 and all(subs)


# --- patient tester on #944: a link session reaches sign-in, not a wall -----

_HOME_JS = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
            / "static" / "home.js").read_text()


def test_a_link_session_hub_marks_the_add_section(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    with c.session_transaction() as s:
        s["via_link"] = True
    sec = _section(c.get("/home").get_data(as_text=True), "connect-section")
    assert 'data-link-session="1"' in sec
    with c.session_transaction() as s:
        s.pop("via_link")
    sec = _section(c.get("/home").get_data(as_text=True), "connect-section")
    assert "data-link-session" not in sec


def test_a_real_tile_in_a_link_session_offers_the_code_sign_in():
    tile = _HOME_JS[_HOME_JS.index(
        'document.querySelectorAll(".connector-tile")'):]
    tile = tile[:tile.index("function showConsentCard")]
    # Before the consent card: no terms to read only to be refused.
    assert tile.index("linkSession") < tile.index("showConsentCard(")
    # #940's code-is-coming copy, and a same-tab link to the code sign-in.
    assert tile.count("sayNext(tile)") == 2
    assert "CODE_FIRST, CODE_SIGN_IN" in _HOME_JS[
        _HOME_JS.index("function sayNext("):]
    consts = _HOME_JS[_HOME_JS.index("const CODE_FIRST"):]
    consts = consts[:consts.index("};")]
    assert "We'll email you a code to sign in." in consts
    assert 'href: "/auth?connect=1"' in consts
    assert "Sign in again to connect your records" in consts
    assert "sameTab: true" in consts
    # And a refusal from a stale page gets the same way forward.
    assert 'res.d.error === "sign_in_again"' in tile


def test_say_opens_a_same_tab_link_in_the_same_tab():
    say = _HOME_JS[_HOME_JS.index("function say("):]
    say = say[:say.index("\n  }\n")]
    assert "link.sameTab" in say


# --- patient tester on #944: More and the drawer look tappable --------------

def test_menus_and_the_drawer_carry_a_chevron():
    css = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
           / "static" / "careagents.css").read_text()
    block = css[css.index("hub around outcomes"):]
    for sel in (".card-menu > summary::after",
                ".made-up-sources > summary::after",
                ".card-menu[open] > summary::after",
                ".made-up-sources[open] > summary::after"):
        assert sel in block, sel
    assert "::-webkit-details-marker" in block


# --- patient tester recheck on #944 ------------------------------------------

_CSS = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
        / "static" / "careagents.css").read_text()


def test_the_code_sign_in_line_is_a_next_step_not_an_error():
    """"Sign in again" is the normal way forward, so it reads in the
    next-step teal, and any other message under a tile drops that look."""
    tile = _HOME_JS[_HOME_JS.index(
        'document.querySelectorAll(".connector-tile")'):]
    tile = tile[:tile.index("function showConsentCard")]
    assert tile.count("sayNext(tile)") == 2
    assert 'classList.remove("is-next")' in tile
    helper = _HOME_JS[_HOME_JS.index("function sayNext("):]
    helper = helper[:helper.index("\n  }\n")]
    assert 'classList.add("is-next")' in helper
    rule = _CSS[_CSS.index(".inline-msg.is-next {"):]
    rule = rule[:rule.index("}")]
    assert "critical" not in rule and "--accent" in rule


def _ancestors_of(page, target_id):
    from html.parser import HTMLParser

    class P(HTMLParser):
        stack, found = [], None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if a.get("id") == target_id and self.found is None:
                self.found = [x for x in self.stack]
            if tag not in ("input", "br", "img", "meta", "link"):
                self.stack.append(a.get("id") or tag)

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()
    p = P()
    p.stack, p.found = [], None
    p.feed(page)
    return p.found


def test_the_starters_sit_above_the_composer_not_in_the_log(app, svc,
                                                            monkeypatch):
    """At 375x640 the starters were under the message box (patient tester
    recheck on #944): they now sit just above it, outside the scrolling
    log, so they show without scrolling."""
    c, agent, _ = _chat(app, svc, monkeypatch, {
        "Condition": 3, "MedicationRequest": 2, "Observation": 12})
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert "log" not in _ancestors_of(page, "starters")
    assert page.index('id="log"') < page.index('id="starters"') \
        < page.index('id="composer"')
    assert _starters(page) == [INTAKE, *TUNED]


def test_an_error_brings_the_starters_back_where_they_live():
    js = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
          / "static" / "chat.js").read_text()
    show = js[js.index("function showSuggestions()"):]
    show = show[:show.index("\n  }\n")]
    assert "n !== starters" in show
