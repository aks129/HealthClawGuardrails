"""The calm hub page (spec section 3, 4, 7 and 9)."""

from __future__ import annotations

import pathlib
import re

from careagents import connectors
from careagents import hub as hub_view
from tests.test_careagents import _beta_app, _login, _make_account
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

#: The CareAgents fixtures, re-exported under their own names.
cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture

_HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
            / "careagents" / "static" / "home.js").read_text()


def _signed_in(app, svc, monkeypatch, email="gene@example.com"):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with c.session_transaction() as s:
        return c, s["account_id"]


def _card(body, conn_id):
    at = body.index(f'data-conn="{conn_id}" data-kind')
    start = body.rindex('<div class="hub-card conn-card"', 0, at)
    return body[start:body.index("conn-refresh-msg", at)]


def _menu(body):
    start = body.index('id="connect-section"')
    return body[start:body.index("</section>", start)]


def test_the_waiting_line_starts_as_checking_never_zero(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    body = c.get("/home").get_data(as_text=True)
    assert 'id="waiting" data-state="checking"' in body
    assert "Checking for requests…" in body
    assert "Nothing yet" not in body
    # The browser keeps "unknown" apart from zero.
    assert "Couldn't check for requests." in _HOME_JS
    assert 'typeof d.count !== "number"' in _HOME_JS


def test_the_waiting_band_links_every_queue(app, svc, monkeypatch):
    """One link per assistant's queue when there are several, so a second
    assistant's requests are reachable from the hub (QA, calm hub PR 2)."""
    body = _HOME_JS.split("function showWaiting(")[1].split("\n  }\n")[0]
    assert "d.queues" in body
    assert "q.href" in body and "q.name" in body


def test_the_hub_shows_no_revoked_connection_outside_past_connections(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    live = svc.add_connection(aid, "direct", "ca-live", "Upload",
                              status="active")
    gone = svc.add_connection(aid, "direct", "ca-gone", "Old upload",
                              status="active")
    svc.revoke_connection(aid, gone)
    body = c.get("/home").get_data(as_text=True)
    past = body.index('id="past-connections"')
    assert live in body[:past]
    assert gone not in body[:past]
    assert gone in body[past:]


def test_every_status_word_renders_in_plain_language(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    for status in ("active", "pending", "empty"):
        svc.add_connection(aid, "direct", f"ca-{status}", f"R {status}",
                           status=status)
    body = c.get("/home").get_data(as_text=True)
    words = dict(re.findall(
        r'<span class="status status-(\w+)">([^<]*)</span>', body))
    assert words == {"active": "Connected", "pending": "Connecting…",
                     "empty": "No records yet"}
    assert ">revoked<" not in body and ">active<" not in body


def test_a_sample_card_offers_delete_only_with_badge_count_and_age(
        app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()["id"]
    card = _card(c.get("/home").get_data(as_text=True), conn)
    assert "Made-up records" in card
    assert "100 records" in card and "Updated today" in card
    assert "conn-delete" in card
    assert "conn-disconnect" not in card and "conn-refresh" not in card


def test_the_assistant_card_has_chat_brief_and_a_menu(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    body = c.get("/home").get_data(as_text=True)
    assert f'href="/chat?agent={agent}">Chat</a>' in body
    assert f'href="/brief?agent={agent}">Visit brief</a>' in body
    assert "Reads Sample records" in body
    assert 'class="agent-rename"' in body and 'class="agent-delete"' in body
    # One active connection: nothing to change records to.
    assert 'class="agent-move"' not in body
    svc.add_connection(aid, "direct", "ca-two", "Upload", status="active")
    assert 'class="agent-move"' in c.get("/home").get_data(as_text=True)


def test_the_hub_has_no_add_another_assistant_control(app, svc, monkeypatch):
    c, _ = _signed_in(app, svc, monkeypatch)
    c.post("/api/connections/sample")
    body = c.get("/home").get_data(as_text=True)
    assert 'id="new-agent-btn"' not in body
    assert 'id="agent-modal"' not in body
    assert 'id="start-chat"' not in body


def test_an_account_with_records_and_no_assistant_can_start_one(
        app, svc, monkeypatch):
    """Deleting Juniper must not strand the account: `first_agent_at` never
    fires again, so the hub offers the one way back (QA, calm hub PR 2)."""
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    c.delete(f"/api/agents/{conn['agent_id']}")
    body = c.get("/home").get_data(as_text=True)
    assert f'id="start-chat" data-conn="{conn["id"]}"' in body
    # The button's request, as home.js sends it, lands in a chat.
    js = _HOME_JS.split('const startChat = $("start-chat");')[1].split(
        "\n  });\n")[0]
    assert 'post("/api/agents"' in js and "startChat.dataset.conn" in js
    assert 'location.href = "/chat?agent=" + res.d.id' in js
    r = c.post("/api/agents", json={"name": "Juniper", "persona": "calm",
                                    "connection_id": conn["id"]})
    assert r.status_code == 200
    agent = r.get_json()["id"]
    assert c.get(f"/chat?agent={agent}").status_code == 200
    assert [a["id"] for a in svc.list_home(aid)["agents"]] == [agent]


def test_start_a_chat_cannot_use_another_accounts_records(
        app, svc, monkeypatch):
    """MUTATION: drop `account_id` from create_agent's connection lookup ->
    red. The Start a chat button carries a connection id the browser can
    change, so ownership is the server's to check."""
    c, aid = _signed_in(app, svc, monkeypatch)
    other = _make_account(svc, monkeypatch, "someone@example.org")
    theirs = svc.add_connection(other.id, "direct", "ca-theirs", "Theirs",
                                status="active")
    r = c.post("/api/agents", json={"name": "Juniper", "persona": "calm",
                                    "connection_id": theirs})
    assert r.status_code == 400
    assert svc.list_home(aid)["agents"] == []
    assert svc.list_home(other.id)["agents"] == []


def test_the_closed_menu_is_one_action_and_one_line(svc, monkeypatch):
    c = _beta_app(svc).test_client()
    _login(c, svc, monkeypatch, email="tester@example.org")
    menu = _menu(c.get("/home").get_data(as_text=True))
    assert menu.count('id="explore-sample"') == 1
    assert "Explore with made-up records" in menu
    assert ("Coming for invited testers: your doctor's records, Apple "
            "Health and wearables, uploading a file from your patient "
            "portal.") in menu
    assert "Coming soon" not in menu and "menu-group" not in menu
    assert "connector-row" not in menu and 'class="chip' not in menu


def test_the_closed_state_filters_every_coming_soon_source(cfg):
    """MUTATION: drop the closed-state filter in hub.menu_items -> red. The
    catalog still returns the coming-soon sources; the view drops them."""
    closed = connectors.catalog(cfg, real_records=False)
    assert any(m["tier"] == "soon" for m in closed)
    assert hub_view.menu_items(closed, real_open=False) == []
    opened = connectors.catalog(cfg, real_records=True)
    shown = hub_view.menu_items(opened, real_open=True)
    assert {m["id"] for m in shown} == {
        m["id"] for m in opened if m["id"] != "sample"}
    assert any(m["tier"] == "soon" for m in shown)


def test_the_open_menu_groups_sources_with_one_chip_each(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    menu = _menu(c.get("/home").get_data(as_text=True))
    assert "Start with sample records, or find your own." in menu
    for name in ("Find my records", "Record services", "Bring a file",
                 "Devices and apps"):
        assert f"<h3>{name}</h3>" in menu, name
    assert 'class="linkish sample-link' in menu
    assert 'id="explore-sample"' not in menu
    svc.add_connection(aid, "direct", "ca-up", "Upload", status="active")
    menu = _menu(c.get("/home").get_data(as_text=True))
    for cid, chip in (("direct", "Connected"), ("fasten", "Available"),
                      ("hbo", "Coming soon")):
        row = menu[menu.index(f'data-connector="{cid}"'):]
        assert re.search(r'<span class="chip[^"]*">([^<]+)</span>',
                         row).group(1) == chip, cid


def test_a_crowded_legacy_account_renders_every_row(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    samples = [svc.add_connection(aid, "sample", f"ca-s{i}", "Sample records")
               for i in range(3)]
    pending = svc.add_connection(aid, "fasten", "ca-f", "Clinic",
                                 status="pending")
    names = ["Juniper", "Ada", "Coach", "Scout"]
    for i, name in enumerate(names):
        svc.create_agent(aid, name, "calm", samples[i % 2])
    r = c.get("/home")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for name in names:
        assert f'<div class="hub-card-name">{name}</div>' in body
    for cid in samples + [pending]:
        assert f'data-conn="{cid}" data-kind' in body
