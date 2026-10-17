"""The hub renders whose each assistant is (one assistant per person spec
PR #943, sections 4, 5 and 10; data from #947).

Each card says whose it is, its tone, and "Focuses on" when routines are
set. An older account's extra assistants each name the records they read
and offer Remove; the primary never does. Adding someone you care for is
behind CARE_PEOPLE. A nickname is the person's own words: escaped on every
page, and never handed to the model, the logs or the engine.

Synthetic data only.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import logging
import pathlib
import re

from careagents import hub as hub_view
from tests.careagents_legacy_helpers import legacy_agent
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, cfg, svc)

_HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
            / "careagents" / "static" / "home.js").read_text()

HOSTILE = '<b id="x">Mo\'m"</b>'


def _client(cfg, svc, monkeypatch, people=False, email="pat@example.com",
            hc=None):
    from careagents.app import create_app
    cfg.people_enabled = people
    hc = hc or FakeClient()
    app = create_app(config=cfg, client=hc, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with c.session_transaction() as s:
        return c, s["account_id"], hc


def _ask(body: str) -> str:
    """The Ask your assistant section, where the cards live."""
    start = body.index('id="ask-section"')
    return body[start:body.index("</section>", start)]


def _cards(body: str) -> dict:
    """Each assistant card's markup, by agent id."""
    ask = _ask(body)
    out = {}
    for m in re.finditer(r'<div class="hub-card agent-card[^"]*" '
                         r'data-agent="([^"]+)"', ask):
        end = ask.find('class="agent-msg', m.end())
        out[m.group(1)] = ask[m.start():end]
    return out


# --- labels, one place (hub.py) ---------------------------------------------

def test_the_account_holder_has_no_label_until_someone_else_is_here():
    me = {"id": "p1", "kind": "self", "relationship": None, "nickname": None}
    assert hub_view.for_line(me, several=False) == ""
    assert hub_view.for_line(me, several=True) == "For you"
    assert hub_view.whose_records(me) == ""


def test_someone_you_care_for_is_named_by_nickname_or_relationship():
    mom = {"id": "p2", "kind": "other", "relationship": "parent",
           "nickname": "Mom"}
    assert hub_view.for_line(mom, several=True) == "For Mom"
    assert hub_view.whose_records(mom) == "Mom's records"
    kid = {"id": "p3", "kind": "other", "relationship": "child",
           "nickname": None}
    assert hub_view.for_line(kid, several=True) == "For your child"
    assert hub_view.whose_records(kid) == "Your child's records"
    # "other" with no nickname never reads "your other".
    anyone = {"id": "p4", "kind": "other", "relationship": "other",
              "nickname": None}
    assert hub_view.for_line(anyone, several=True) == (
        "For someone you care for")
    assert "your other" not in hub_view.whose_records(anyone).lower()
    # An unknown relationship is never shown raw.
    odd = {"id": "p5", "kind": "other", "relationship": "boss",
           "nickname": None}
    assert "boss" not in hub_view.for_line(odd, several=True)
    assert "boss" not in hub_view.whose_records(odd)


# --- 1. each card says whose it is, its tone, and what it focuses on --------

def test_a_new_account_card_shows_tone_and_no_person_label(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    cards = _cards(c.get("/home").get_data(as_text=True))
    assert list(cards) == [agent]
    card = cards[agent]
    # Plain words, not the persona's label (patient tester on #951, F2).
    assert "Tone: calm" in card and "Calm Guide" not in card
    assert "For you" not in card and "For " not in card
    assert "Focuses on" not in card
    assert 'href="/settings#assistant-settings"' in card


def test_a_card_says_what_it_focuses_on_once_routines_are_set(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    c.post(f"/api/agents/{agent}/settings",
           json={"persona": "direct", "routines": ["refills", "care-gaps"]})
    card = _cards(c.get("/home").get_data(as_text=True))[agent]
    assert "Focuses on: care gaps, refills" in card
    assert "Tone: direct" in card and "Straight Shooter" not in card
    assert "Watching" not in card          # owner decision D3


def test_someone_you_care_for_has_their_own_labelled_card(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    mine = c.post("/api/connections/sample").get_json()["agent_id"]
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": "Mom"}).get_json()
    cards = _cards(c.get("/home").get_data(as_text=True))
    assert set(cards) == {mine, mom["agent_id"]}
    assert "For Mom" in cards[mom["agent_id"]]
    assert "For you" in cards[mine]
    # The holder's card comes first.
    assert list(cards)[0] == mine


# --- 2. legacy accounts: each names its records, extras offer Remove ---------

def test_a_legacy_account_names_records_and_offers_remove_on_extras_only(
        cfg, svc, monkeypatch):
    c, aid, _ = _client(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    primary = conn["agent_id"]
    up = svc.add_connection(aid, "direct", "t-legacy-up", "Uploaded records",
                            status="active")
    extras = [legacy_agent(svc, conn["id"], name="Coach", persona="direct"),
              legacy_agent(svc, up, name="Helper", persona="sunny"),
              legacy_agent(svc, conn["id"], name="Juniper")]
    svc.set_agent_settings(aid, extras[0], "direct", ["refills"])
    body = c.get("/home").get_data(as_text=True)
    cards = _cards(body)
    assert set(cards) == {primary, *extras}
    for aid_, card in cards.items():
        assert "Reads " in card, aid_
    assert "Reads uploaded records" in cards[extras[1]]
    assert "Focuses on: refills" in cards[extras[0]]
    for e in extras:
        assert f'class="agent-remove" data-agent="{e}"' in cards[e]
    assert "agent-remove" not in cards[primary]
    assert "agent-delete" not in cards[primary]
    # The primary is listed first, the extras under it.
    assert list(cards)[0] == primary


def test_a_single_assistant_offers_no_remove(cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    agent = c.post("/api/connections/sample").get_json()["agent_id"]
    card = _cards(c.get("/home").get_data(as_text=True))[agent]
    assert "agent-remove" not in card and "agent-delete" not in card


def test_remove_asks_in_the_page_not_a_browser_dialog(cfg, svc,
                                                      monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    legacy_agent(svc, conn["id"])
    body = c.get("/home").get_data(as_text=True)
    assert 'id="agent-delete-modal"' in body
    js = _HOME_JS.split('querySelectorAll(".agent-remove")')[1][:600]
    assert "askToRemoveAgent" in js
    assert "confirm(" not in _HOME_JS


# --- 3. empty state ------------------------------------------------------------

def test_the_empty_hub_says_your_assistant_is_waiting_for_records(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch)
    ask = _ask(c.get("/home").get_data(as_text=True))
    assert "Your assistant, waiting for records" in ask


# --- 4. chat header and the waiting band name the person ----------------------

def test_the_chat_header_names_someone_you_care_for(cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    mine = c.post("/api/connections/sample").get_json()["agent_id"]
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": "Mom"}).get_json()
    head = c.get(f"/chat?agent={mom['agent_id']}").get_data(as_text=True)
    head = head[head.index('class="chat-head"'):head.index('id="log"')]
    assert "Mom&#39;s records" in head      # escaped, as every label is
    own = c.get(f"/chat?agent={mine}").get_data(as_text=True)
    own = own[own.index('class="chat-head"'):own.index('id="log"')]
    assert "records</div>" not in own and "chat-whose" not in own


def test_the_approvals_page_names_someone_you_care_for(cfg, svc,
                                                       monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    kid = c.post("/api/people", json={"relationship": "child"}).get_json()
    page = c.get(f"/agents/{kid['agent_id']}/approvals").get_data(
        as_text=True)
    # Whose records, not the assistant's name (patient tester on #951, F3).
    page = " ".join(page.split())
    assert "Requests for your child&#39;s records." in page
    assert "Requests proposed on" not in page
    own = c.post("/api/connections/sample").get_json()["agent_id"]
    mine = " ".join(c.get(f"/agents/{own}/approvals").get_data(
        as_text=True).split())
    assert "Requests for your records." in mine


def test_each_waiting_queue_says_whose_records_it_is(cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    mine = c.post("/api/connections/sample").get_json()["agent_id"]
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": "Mom"}).get_json()
    d = c.get("/api/approvals/count").get_json()
    by_agent = {q.get("agent_id"): q for q in d["queues"]}
    assert by_agent[mom["agent_id"]]["whose"] == "Mom's records"
    assert by_agent[mine].get("whose", "") == ""
    band = _HOME_JS.split("function showWaiting(")[1].split("\n  }\n")[0]
    assert "whose" in band


# --- 5. add someone you care for: behind CARE_PEOPLE, made-up only ------------

def test_the_add_someone_entry_is_absent_with_the_flag_off(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=False)
    c.post("/api/connections/sample")
    body = c.get("/home").get_data(as_text=True)
    assert 'id="add-person"' not in body
    assert "Add someone you care for" not in body


def test_the_add_someone_entry_is_on_the_hub_with_the_flag_on(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    ask = _ask(c.get("/home").get_data(as_text=True))
    assert 'id="add-person"' in ask
    assert "Add someone you care for" in ask
    # A relationship picker over the fixed set, none chosen for them.
    for rel in ("parent", "child", "partner", "other"):
        assert f'name="relationship" value="{rel}"' in ask
    assert "checked" not in ask.split('id="add-person"')[1]
    assert 'id="add-person-nickname"' in ask and 'maxlength="24"' in ask
    assert "made-up records" in " ".join(
        ask.split('id="add-person"')[1].split())


def test_add_someone_waits_until_you_have_your_own_assistant(
        cfg, svc, monkeypatch):
    """Patient tester on #951: offered first, it came before the holder had
    seen their own assistant."""
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    assert 'id="add-person"' not in c.get("/home").get_data(as_text=True)
    c.post("/api/connections/sample")
    assert 'id="add-person"' in c.get("/home").get_data(as_text=True)


# --- patient tester on #951 ------------------------------------------------------

def _source_rows(body: str) -> dict:
    """Each source row's markup in Your sources, by connection id."""
    start = body.index('id="records-section"')
    sec = body[start:body.index("</section>", start)]
    out = {}
    for m in re.finditer(r'<div class="hub-card conn-card" data-tenant="[^"]*"'
                         r'\s+data-conn="([^"]+)"', sec):
        out[m.group(1)] = sec[m.start():sec.find("conn-refresh-msg",
                                                 m.end())]
    return out


def test_f1_each_source_says_whose_records_once_someone_is_added(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    mine = c.post("/api/connections/sample").get_json()["id"]
    rows = _source_rows(c.get("/home").get_data(as_text=True))
    # One person: nothing new on the row.
    assert "Your records" not in rows[mine]
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": "Mom"}).get_json()
    rows = _source_rows(c.get("/home").get_data(as_text=True))
    assert set(rows) == {mine, mom["connection_id"]}
    assert ('class="agent-for source-whose">Your records</div>'
            in rows[mine])
    assert ('class="agent-for source-whose">Mom&#39;s records</div>'
            in rows[mom["connection_id"]])
    # The person line tells the two "Sample records" apart: no "(2)".
    assert "(2)" not in rows[mine] + rows[mom["connection_id"]]
    # Delete names whose records it deletes.
    assert 'data-whose="Mom&#39;s records"' in rows[mom["connection_id"]]
    assert 'data-whose="Your records"' in rows[mine]


def test_f1_twins_of_one_person_are_still_numbered():
    """Two of the holder's own same-day samples still need "(2)"."""
    home = {"people": [{"id": "me", "kind": "self", "relationship": None,
                        "nickname": None},
                       {"id": "mom", "kind": "other",
                        "relationship": "parent", "nickname": "Mom"}],
            "agents": [],
            "connections": [
                {"id": f"c{i}", "kind": "sample", "label": "Sample records",
                 "status": "active", "provider": "CareAgents sample",
                 "connected_at": 1000.0 + i, "last_synced_at": None,
                 "last_count": 100, "tenant_id": f"t{i}", "person_id": pid}
                for i, pid in enumerate(("me", "me", "mom"))]}
    rows = {r["id"]: r for r in hub_view.build(home, 2000.0)["sources"]}
    assert rows["c1"]["sub"].endswith("(2)")
    assert "(2)" not in rows["c2"]["sub"] and "Added" not in rows["c2"]["sub"]
    assert rows["c2"]["whose"] == "Mom's records"
    assert rows["c0"]["whose"] == "Your records"


def test_f1_the_delete_dialog_is_given_whose_records():
    body = _HOME_JS.split('document.querySelectorAll(".conn-delete")')[1][:600]
    assert ('btn.dataset.whose ? label + " (" + btn.dataset.whose + ")" '
            ': label') in body
    # The browser check (375px run) reads the dialog's own text too.


def test_f4_someone_you_care_for_gets_a_different_default_name(
        cfg, svc, monkeypatch):
    """Never the nickname: the name goes into the model's prompt."""
    from careagents.models import Agent
    c, aid, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    a = c.post("/api/people", json={"relationship": "parent",
                                    "nickname": "Zorblax"}).get_json()
    b = c.post("/api/people", json={"relationship": "child"}).get_json()
    with svc.session() as s:
        names = {x.id: x.name for x in s.query(Agent).filter_by(
            account_id=aid)}
    assert names[a["agent_id"]] != "Juniper"
    assert "Zorblax" not in names[a["agent_id"]]
    assert len(set(names.values())) == 3
    assert names[b["agent_id"]] not in ("Juniper", names[a["agent_id"]])


def test_a_paused_account_is_not_offered_to_add_someone(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True,
                      email="paused@example.com")
    c.post("/api/connections/sample")
    assert 'id="add-person"' in c.get("/home").get_data(as_text=True)
    svc.set_paused("paused@example.com", True)
    assert 'id="add-person"' not in c.get("/home").get_data(as_text=True)


def test_the_server_refuses_a_long_nickname_or_unknown_relationship(
        cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    assert c.post("/api/people", json={"relationship": "parent",
                                       "nickname": "x" * 25}).status_code == 400
    assert c.post("/api/people",
                  json={"relationship": "boss"}).status_code == 400


def test_settings_names_someone_you_care_for_as_the_hub_does(
        cfg, svc, monkeypatch):
    """A nickname-less "other" is "someone you care for" in Settings too,
    never "your other" (hub.for_line, one rule for both pages)."""
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    c.post("/api/people", json={"relationship": "other"})
    c.post("/api/people", json={"relationship": "parent", "nickname": "Mom"})
    page = " ".join(c.get("/settings").get_data(as_text=True).split())
    assert "your other" not in page.lower()
    assert "For someone you care for" in page
    assert "For Mom" in page


def test_picking_a_relationship_clears_the_choose_message():
    """"Choose who they are to you." goes as soon as a relationship is
    picked, not only on the next submit."""
    body = _HOME_JS.split('const addPerson = $("add-person-form");')[1]
    assert 'addPerson.addEventListener("change"' in body
    change = body.split('addPerson.addEventListener("change"')[1][:300]
    assert '$("add-person-msg").hidden = true' in change


# --- the nickname: escaped everywhere, never to the model, logs or engine -----

def test_a_nickname_is_escaped_on_every_page(cfg, svc, monkeypatch):
    c, _, _ = _client(cfg, svc, monkeypatch, people=True)
    c.post("/api/connections/sample")
    r = c.post("/api/people", json={"relationship": "parent",
                                    "nickname": HOSTILE})
    assert r.status_code == 200
    agent = r.get_json()["agent_id"]
    for path in ("/home", f"/chat?agent={agent}",
                 f"/agents/{agent}/approvals", "/settings"):
        page = c.get(path).get_data(as_text=True)
        assert HOSTILE not in page, path
        assert '<b id="x">' not in page, path
    assert "&lt;b id=" in c.get("/home").get_data(as_text=True)


def test_a_nickname_never_reaches_the_engine_or_the_logs(
        cfg, svc, monkeypatch, caplog):
    hc = FakeClient()
    calls = []
    for name in dir(hc):
        fn = getattr(hc, name)
        if name.startswith("_") or not callable(fn):
            continue

        def spy(*a, _fn=fn, _name=name, **k):
            calls.append((_name, repr(a), repr(k)))
            return _fn(*a, **k)
        setattr(hc, name, spy)
    caplog.set_level(logging.DEBUG)
    c, _, _ = _client(cfg, svc, monkeypatch, people=True, hc=hc)
    c.post("/api/connections/sample")
    agent = c.post("/api/people", json={"relationship": "parent",
                                        "nickname": "Zorblax"}
                   ).get_json()["agent_id"]
    for path in ("/home", f"/chat?agent={agent}",
                 f"/agents/{agent}/approvals", "/api/approvals/count"):
        c.get(path)
    assert calls, "the spy saw no engine call at all"
    assert not [x for x in calls if "Zorblax" in "".join(x)]
    assert "Zorblax" not in caplog.text


def test_the_waiting_band_line_names_whose_records_under_node():
    """The band's words, run under node from home.js itself (the
    test_careagents_calm_hub_js approach): a queue for someone you care
    for says whose records it is; the holder's says nothing new."""
    import json
    import shutil
    import subprocess

    import pytest
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    harness = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cut(name) {
  const m = src.match(new RegExp('const ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
const { orphanLine } = new Function(
  cut('requests') + cut('orphanLine') + 'return { orphanLine };')();
process.stdout.write(JSON.stringify({
  mom: orphanLine({ name: 'Sample records', count: 1, whose: "Mom's records" }),
  mine: orphanLine({ name: 'Sample records', count: 1 }),
}));
"""
    home_js = (pathlib.Path(__file__).resolve().parents[1]
               / "careagents" / "static" / "home.js")
    out = subprocess.run(["node", "-e", harness, "--", str(home_js)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["mom"].startswith(
        "1 request waiting on Sample records (Mom's records). ")
    assert got["mine"].startswith("1 request waiting on Sample records. ")
