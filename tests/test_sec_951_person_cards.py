"""Security tester's exploit tests for PR #951 (hub person cards).

V1: a nickname is the holder's own words: escaped on every page and JSON,
and never handed to the engine, the model request or the logs.
V6: account B never sees account A's people, labels or assistants, and an
assistant never moves to another person's records (spec section 9).
Flag: POST /api/people is 404 with CARE_PEOPLE off.

Synthetic data only. The canary nickname is made up.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import logging

import pytest

from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _turn, cfg, svc)

CANARY = "Qzvrandt"          # a made-up nickname, searched for everywhere
APOS = "Mo'm' onx='1"        # breaks a single-quoted attribute if unescaped
TAGS = '<img src=x onerror=alert(1)>'[:24]


def _app(cfg, svc, people=True, hc=None):
    from careagents.app import create_app
    cfg.people_enabled = people
    hc = hc or FakeClient()
    app = create_app(config=cfg, client=hc, accounts=svc)
    app.config["TESTING"] = True
    return app, hc


def _signed_in(app, svc, monkeypatch, email):
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    return c


def _holder_and_mom(c, nickname=CANARY):
    mine = c.post("/api/connections/sample").get_json()
    mom = c.post("/api/people", json={"relationship": "parent",
                                      "nickname": nickname})
    assert mom.status_code == 200, mom.get_data(as_text=True)
    return mine, mom.get_json()


def _agent_conn(svc, account_id, agent_id):
    return next(a["connection_id"] for a in svc.list_home(account_id)["agents"]
                if a["id"] == agent_id)


# --- V6: an assistant never moves to another person's records ----------------

def test_change_records_refuses_the_other_persons_records(
        cfg, svc, monkeypatch):
    app, _ = _app(cfg, svc)
    c = _signed_in(app, svc, monkeypatch, "holder@example.com")
    with c.session_transaction() as s:
        acct = s["account_id"]
    mine, mom = _holder_and_mom(c)
    my_agent, my_conn = mine["agent_id"], mine["id"]
    mom_agent, mom_conn = mom["agent_id"], mom["connection_id"]

    # Mom's assistant onto the holder's records, and the reverse.
    for agent, conn in ((mom_agent, my_conn), (my_agent, mom_conn)):
        r = c.post(f"/api/agents/{agent}/connection",
                   json={"connection_id": conn})
        assert r.status_code == 404, (agent, conn, r.get_data(as_text=True))
        r = c.post("/api/hub/switch-prompt",
                   json={"answer": "switch", "agent_id": agent,
                         "connection_id": conn})
        assert r.status_code == 404, (agent, conn)
    assert _agent_conn(svc, acct, mom_agent) == mom_conn
    assert _agent_conn(svc, acct, my_agent) == my_conn

    # The hub never offers the other person's records on a card.
    home = c.get("/home").get_data(as_text=True)
    ask = home[home.index('id="ask-section"'):]
    ask = ask[:ask.index("</section>")]
    for card in ask.split('data-agent="')[1:]:
        agent = card.split('"', 1)[0]
        other = mom_conn if agent == my_agent else my_conn
        assert other not in card, agent


# --- V6: account B sees nothing of account A ----------------------------------

def test_another_account_gets_nothing_of_a_person_or_their_assistant(
        cfg, svc, monkeypatch):
    app, _ = _app(cfg, svc)
    a = _signed_in(app, svc, monkeypatch, "holder-a@example.com")
    with a.session_transaction() as s:
        acct_a = s["account_id"]
    mine_a, mom_a = _holder_and_mom(a)
    b = _signed_in(app, svc, monkeypatch, "holder-b@example.com")
    with b.session_transaction() as s:
        acct_b = s["account_id"]
    assert acct_a != acct_b
    mine_b = b.post("/api/connections/sample").get_json()

    a_ids = (mom_a["agent_id"], mom_a["connection_id"], mom_a["person_id"],
             mine_a["agent_id"], mine_a["id"])
    # B's own pages and JSON carry nothing of A's.
    for path in ("/home", "/settings", "/api/approvals/count",
                 f"/chat?agent={mine_b['agent_id']}",
                 f"/agents/{mine_b['agent_id']}/approvals"):
        body = b.get(path).get_data(as_text=True)
        assert CANARY not in body, path
        for i in a_ids:
            assert i not in body, (path, i)

    # A's assistant by id, from B: never a 200 with A's label.
    for path in (f"/chat?agent={mom_a['agent_id']}",
                 f"/agents/{mom_a['agent_id']}/approvals",
                 f"/brief?agent={mom_a['agent_id']}"):
        r = b.get(path)
        body = r.get_data(as_text=True)
        assert CANARY not in body, path
        assert "records</div>" not in body or "chat-whose" not in body, path

    # Every write on A's assistant, from B: 404, and nothing changed.
    aid = mom_a["agent_id"]
    assert b.post(f"/api/agents/{aid}/connection",
                  json={"connection_id": mine_b["id"]}).status_code == 404
    assert b.post(f"/api/agents/{aid}/rename",
                  json={"name": "owned"}).status_code == 404
    assert b.post(f"/api/agents/{aid}/settings",
                  json={"persona": "warm"}).status_code == 404
    assert b.delete(f"/api/agents/{aid}").status_code == 404
    assert b.delete(f"/api/agents/{mine_a['agent_id']}").status_code == 404
    # B's own assistant onto A's (or A's Mom's) records.
    for conn in (mom_a["connection_id"], mine_a["id"]):
        assert b.post(f"/api/agents/{mine_b['agent_id']}/connection",
                      json={"connection_id": conn}).status_code == 404
    home_a = svc.list_home(acct_a)
    assert {x["id"] for x in home_a["agents"]} == {aid, mine_a["agent_id"]}
    assert next(x for x in home_a["agents"] if x["id"] == aid)["name"] != "owned"


# --- the flag ------------------------------------------------------------------

def test_with_the_flag_off_people_cannot_be_added(cfg, svc, monkeypatch):
    app, _ = _app(cfg, svc, people=False)
    c = _signed_in(app, svc, monkeypatch, "flagoff@example.com")
    with c.session_transaction() as s:
        acct = s["account_id"]
    c.post("/api/connections/sample")
    r = c.post("/api/people", json={"relationship": "parent",
                                    "nickname": CANARY})
    assert r.status_code == 404
    assert [p for p in svc.list_home(acct)["people"] if p["kind"] == "other"] == []
    home = c.get("/home").get_data(as_text=True)
    assert "add-person" not in home and "Add someone you care for" not in home


# --- V1: the nickname's shape and every sink ----------------------------------

@pytest.mark.parametrize("nick", [
    "x" * 25, "Mom\nDad", "Mom\tx", "Mom‮evil", "Mo\x00m", "​Mom",
    ["Mom"], 7, {"a": 1}])
def test_a_nickname_outside_the_shape_is_refused(cfg, svc, monkeypatch, nick):
    app, _ = _app(cfg, svc)
    c = _signed_in(app, svc, monkeypatch, "shape@example.com")
    r = c.post("/api/people", json={"relationship": "parent",
                                    "nickname": nick})
    assert r.status_code == 400, (repr(nick), r.status_code)


@pytest.mark.parametrize("nick", [APOS, TAGS, '"><svg/onload=1>'])
def test_a_hostile_nickname_is_text_on_every_page_and_json(
        cfg, svc, monkeypatch, nick):
    app, _ = _app(cfg, svc)
    c = _signed_in(app, svc, monkeypatch, "xss@example.com")
    _, mom = _holder_and_mom(c, nickname=nick)
    aid = mom["agent_id"]
    for path in ("/home", f"/chat?agent={aid}", f"/agents/{aid}/approvals",
                 "/settings", f"/brief?agent={aid}"):
        page = c.get(path).get_data(as_text=True)
        assert nick not in page, path
        assert "<img src=x" not in page and "<svg/onload" not in page, path
        assert "Mo'm' onx" not in page, path
    # The count JSON carries the words as data; home.js sets textContent.
    d = c.get("/api/approvals/count").get_json()
    for q in d.get("queues", []):
        if q.get("agent_id") == aid:
            assert q["whose"] == f"{nick}'s records"


# --- V1: the nickname never reaches the engine, the model or the logs ---------

def test_a_chat_turn_for_someone_you_care_for_never_sends_the_nickname(
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
    seen = []

    class _Turn:
        text, tool_calls, raw_tool_calls = "ok", [], []
        model, tier, escalated, advisor_calls, advisor_errors = "", 0, False, 0, 0

    def complete(_cfg, system, history, tools):
        seen.append(repr((system, history, tools)))
        return _Turn()
    monkeypatch.setattr("careagents.worker.llm.complete", complete)
    caplog.set_level(logging.DEBUG)

    app, _ = _app(cfg, svc, hc=hc)
    c = _signed_in(app, svc, monkeypatch, "spy@example.com")
    _, mom = _holder_and_mom(c)
    aid = mom["agent_id"]
    for path in ("/home", f"/chat?agent={aid}", f"/agents/{aid}/approvals",
                 "/api/approvals/count", "/settings", f"/brief?agent={aid}"):
        c.get(path)
    r = _turn(c, aid, "what are my labs?")
    assert r.status_code == 200
    assert seen, "the model was never asked: the turn did not run"
    assert not [s for s in seen if CANARY in s]
    assert calls and not [x for x in calls if CANARY in "".join(x)]
    assert CANARY not in caplog.text


# --- the primary has no Remove: the server refuses it too (SEC951-L1) ---------

def test_the_primary_assistant_cannot_be_removed_by_a_direct_delete(
        cfg, svc, monkeypatch):
    from tests.careagents_legacy_helpers import legacy_agent
    app, _ = _app(cfg, svc)
    c = _signed_in(app, svc, monkeypatch, "primary@example.com")
    mine = c.post("/api/connections/sample").get_json()
    r = c.delete(f"/api/agents/{mine['agent_id']}")
    assert r.status_code == 409
    assert " " in r.get_json()["error"]          # a sentence, not a code
    assert c.get(f"/chat?agent={mine['agent_id']}").status_code == 200
    # A legacy extra is still removable.
    extra = legacy_agent(svc, mine["id"])
    assert c.delete(f"/api/agents/{extra}").status_code == 200
    # Someone you care for's one assistant is their primary too.
    mom = c.post("/api/people", json={"relationship": "parent"}).get_json()
    assert c.delete(f"/api/agents/{mom['agent_id']}").status_code == 409
    # Deleting the account still removes everything.
    assert svc.delete_account(svc_account_id(svc, "primary@example.com"))


def svc_account_id(svc, email):
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id
