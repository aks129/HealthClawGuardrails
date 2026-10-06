"""iMessage, text first: a stranger's text gets a sign-in link, a bound
handle gets its assistant, and STOP / HELP / START behave as a phone
user expects. The core is careagents.imessage.handle_inbound; these tests
drive it through the relay's routes, as the Mac relay does.

Synthetic handles only (555 numbers, example.com addresses).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from careagents import beta, imessage
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, _login, cfg, svc)

HDRS = {"X-Internal-Secret": "mint-secret"}
PHONE = "+15550100123"
OTHER_EMAIL = "other@example.com"


def _inbound(c, handle, text, **extra):
    return c.post("/api/surfaces/imessage/inbound", headers=HDRS,
                  json={"handle": handle, "text": text, **extra})


def _link_token(reply: str) -> str:
    m = re.search(r"/link/([A-Za-z0-9_-]+)", reply)
    assert m, reply
    return m.group(1)


def _pair(c, agent_id, handle=PHONE):
    """Bind `handle` the settings way: a code, texted as `care <code>`."""
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    return _inbound(c, handle, f"care {code}")


def _acct_id(svc, email):  # noqa: F811
    from careagents.models import Account
    with svc.session() as s:
        return s.query(Account).filter_by(email=email).one().id


def _run(app):
    from careagents.worker import RunWorker
    rt = app.extensions["careagents_runtime"]
    RunWorker(rt["config"], rt["client"], rt["accounts"], "im-worker"
              ).run_once()


# --- parsing --------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("+15550100123", "+15550100123"),
    ("(555) 010-0123", "+15550100123"),
    ("555.010.0123", "+15550100123"),
    ("1 555 010 0123", "+15550100123"),
    ("tel:+44 20 7946 0000", "+442079460000"),
    ("  Person@Example.COM ", "person@example.com"),
    ("mailto:Person@Example.com", "person@example.com"),
    ("12345", None),          # a short code: nothing to text back
    ("+1000", None),
    ("not a handle", None),
    ("", None),
])
def test_handles_are_normalized(raw, want):
    assert imessage.normalize_handle(raw) == want


@pytest.mark.parametrize("text,want", [
    ("STOP", "stop"), (" stop ", "stop"), ("Stop!", "stop"),
    ("help", "help"), ("Start.", "start"),
    ("stop texting me", None), ("please help", None), ("", None),
])
def test_keywords_are_whole_message_and_case_insensitive(text, want):
    assert imessage.keyword(text) == want


def test_mask_never_shows_the_whole_handle():
    assert "0100123" not in imessage.mask(PHONE)
    assert imessage.mask("person@example.com") == "p***@example.com"


# --- a stranger gets a link, not silence -----------------------------------

def test_an_unbound_handle_gets_a_one_time_link_stored_hashed(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import ImessageLink
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    r = _inbound(c, "(555) 010-0199", "hi")
    assert r.status_code == 200
    body = r.get_json()
    assert "run_id" not in body
    assert body["reply"].startswith("Hi, this is CareAgents.")
    token = _link_token(body["reply"])
    assert f"{cfg.origin}/link/{token}" in body["reply"]
    with svc.session() as s:
        rows = s.query(ImessageLink).all()
        assert [x.handle for x in rows] == ["+15550100199"]
        assert rows[0].token_hash == imessage.hash_token(token)
        assert token not in {rows[0].token_hash, rows[0].id}


def test_links_per_sender_are_limited(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    for _ in range(imessage.LINKS_PER_WINDOW):
        assert "reply" in _inbound(c, PHONE, "hi").get_json()
    over = _inbound(c, PHONE, "hi")
    assert over.status_code == 200 and over.get_json() == {}


def test_an_untextable_handle_is_not_answered(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    r = _inbound(c, "12345", "hi")
    assert r.status_code == 404 and "reply" not in r.get_json()


def test_the_link_signs_in_then_binds_and_works_once(
        cfg, svc, monkeypatch):  # noqa: F811
    app, owner, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(owner, PHONE, "hi").get_json()["reply"])

    phone = app.test_client()                     # the texter's browser
    r = phone.get(f"/link/{token}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth")
    _login(phone, svc, monkeypatch, email="texter@example.com")
    r = phone.get("/home")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/link/done")
    done = phone.get("/link/done")
    assert done.status_code == 200
    assert "You're connected." in done.get_data(as_text=True)

    surface = svc.find_surface_by_handle(PHONE)
    acct_id = _acct_id(svc, "texter@example.com")
    assert surface and surface["account_id"] == acct_id
    # Spent: the same link again is refused, signed in or not.
    again = app.test_client().get(f"/link/{token}")
    assert again.status_code == 410
    assert "expired" in again.get_data(as_text=True)


def test_an_expired_link_is_refused(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import ImessageLink
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(c, PHONE, "hi").get_json()["reply"])
    with svc.session() as s:
        for x in s.query(ImessageLink).all():
            x.exp = 1.0
    assert app.test_client().get(f"/link/{token}").status_code == 410


def test_a_signed_in_person_binds_straight_away(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(c, PHONE, "hi").get_json()["reply"])
    r = c.get(f"/link/{token}")
    assert r.headers["Location"].endswith("/link/done")
    assert "You're connected." in c.get("/link/done").get_data(as_text=True)
    assert svc.find_surface_by_handle(PHONE)


# --- the welcome ------------------------------------------------------------

def test_the_first_reply_after_a_link_bind_is_the_welcome(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch,
                                           reply="Your A1c is in range.")
    token = _link_token(_inbound(c, PHONE, "hi").get_json()["reply"])
    c.get(f"/link/{token}")
    c.get("/link/done")

    first = _inbound(c, PHONE, "how is my a1c?")
    assert first.status_code == 202
    body = first.get_json()
    assert body["reply"] == imessage.WELCOME_TEXT and body["run_id"]
    for phrase in ("health records", "lab results", "visit", "link",
                   "STOP"):
        assert phrase in imessage.WELCOME_TEXT
    _run(app)
    answer = c.get(f"/api/surfaces/imessage/runs/{body['run_id']}",
                   headers=HDRS, query_string={"handle": PHONE})
    assert answer.get_json()["reply"] == "Your A1c is in range."
    second = _inbound(c, PHONE, "thanks")
    assert second.status_code == 202 and "reply" not in second.get_json()


def test_pairing_with_a_code_answers_with_the_welcome(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    r = _pair(c, agent_id, handle="555-010-0123")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "reply": imessage.WELCOME_TEXT}
    # Bound under the normalized handle, so any spelling finds it.
    assert svc.find_surface_by_handle(PHONE)["agent_id"] == agent_id
    nxt = _inbound(c, "(555) 010-0123", "hello")
    assert nxt.status_code == 202 and "reply" not in nxt.get_json()


def test_a_new_account_without_an_assistant_is_told_what_to_do(
        cfg, svc, monkeypatch):  # noqa: F811
    app, owner, *_ = _chat_app(cfg, svc, monkeypatch)
    token = _link_token(_inbound(owner, PHONE, "hi").get_json()["reply"])
    phone = app.test_client()
    phone.get(f"/link/{token}")
    _login(phone, svc, monkeypatch, email="new@example.com")
    phone.get("/link/done")
    r = _inbound(owner, PHONE, "what are my labs?")
    assert r.status_code == 200
    assert r.get_json() == {"reply": imessage.no_agent_text(cfg.origin)}


def test_the_older_relay_bind_route_still_pairs(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    r = c.post("/api/surfaces/imessage/bind", headers=HDRS,
               json={"code": code, "handle": PHONE})
    assert r.status_code == 200 and r.get_json()["reply"]
    assert svc.find_surface_by_handle(PHONE)


# --- keywords -----------------------------------------------------------------

def test_stop_unbinds_and_then_only_start_gets_an_answer(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    r = _inbound(c, PHONE, "Stop")
    assert r.status_code == 200
    assert r.get_json() == {"reply": imessage.STOP_TEXT}
    assert svc.find_surface_by_handle(PHONE) is None
    assert _inbound(c, PHONE, "hello?").get_json() == {}
    assert _inbound(c, PHONE, "STOP").get_json() == {}   # said once
    assert _inbound(c, PHONE, "help").get_json() == {
        "reply": imessage.HELP_TEXT}
    start = _inbound(c, PHONE, "start").get_json()
    assert "/link/" in start["reply"]
    # Opted back in: a plain text is answered again.
    assert "/link/" in _inbound(c, PHONE, "hi").get_json()["reply"]


def test_stop_from_a_stranger_is_confirmed_and_kept(cfg, svc, monkeypatch):  # noqa: F811
    app, c, *_ = _chat_app(cfg, svc, monkeypatch)
    assert _inbound(c, PHONE, "STOP").get_json() == {
        "reply": imessage.STOP_TEXT}
    assert _inbound(c, PHONE, "hi").get_json() == {}


def test_stop_inside_a_sentence_is_just_a_message(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    r = _inbound(c, PHONE, "should I stop my statin?")
    assert r.status_code == 202 and r.get_json()["run_id"]
    assert svc.find_surface_by_handle(PHONE)


def test_help_is_one_line_with_the_contact_address(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    r = _inbound(c, PHONE, "HELP")
    assert r.get_json() == {"reply": imessage.HELP_TEXT}
    assert "contactus@healthclaw.io" in imessage.HELP_TEXT
    assert "\n" not in imessage.HELP_TEXT


def test_start_while_connected_just_says_so(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    assert _inbound(c, PHONE, "start").get_json() == {
        "reply": imessage.START_BOUND_TEXT}


# --- one handle, one account ---------------------------------------------------

def test_a_handle_bound_elsewhere_is_not_taken_over(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    owner_id = svc.find_surface_by_handle(PHONE)["account_id"]

    other = app.test_client()
    _login(other, svc, monkeypatch, email=OTHER_EMAIL)
    conn = other.post("/api/connections/sample").get_json()["id"]
    other_agent = other.post("/api/agents", json={
        "name": "B", "persona": "calm", "connection_id": conn}
    ).get_json()["id"]
    r = _pair(other, other_agent)
    assert r.status_code == 409
    assert r.get_json()["reply"] == imessage.TAKEN_TEXT
    assert svc.find_surface_by_handle(PHONE)["account_id"] == owner_id

    # The link route refuses the same way.
    link_id = svc.issue_imessage_link(PHONE)
    assert link_id
    assert svc.claim_imessage_link(
        svc.peek_imessage_link(link_id),
        _acct_id(svc, OTHER_EMAIL)) == "taken"

    # Once the owner texts STOP, the other account may pair.
    _inbound(c, PHONE, "stop")
    assert _pair(other, other_agent).status_code == 200
    assert svc.find_surface_by_handle(PHONE)["agent_id"] == other_agent


def test_repairing_on_the_same_account_moves_the_handle(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import Surface
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    assert _pair(c, agent_id).status_code == 200
    with svc.session() as s:
        assert s.query(Surface).filter_by(
            handle=PHONE, status="active").count() == 1


# --- pairing codes expire and are not guessable by brute force --------------

def test_an_expired_code_does_not_pair(cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import Surface
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    with svc.session() as s:
        s.query(Surface).filter_by(handle=code).one().code_exp = 1.0
    r = _inbound(c, PHONE, f"care {code}")
    assert r.status_code == 404
    assert r.get_json()["reply"] == imessage.CODE_FAILED_TEXT


def test_a_pending_code_from_before_expiry_existed_fails_closed(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.models import Surface
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    with svc.session() as s:
        s.query(Surface).filter_by(handle=code).one().code_exp = None
    assert _inbound(c, PHONE, f"care {code}").status_code == 404


def test_wrong_codes_lock_the_sender_out(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    for i in range(imessage.BIND_ATTEMPTS):
        assert _inbound(c, PHONE, f"care wrongcode{'a' * i}"
                        ).status_code == 404
    locked = _inbound(c, PHONE, f"care {code}")
    assert locked.status_code == 429
    assert locked.get_json()["reply"] == imessage.CODE_LOCKED_TEXT
    assert svc.find_surface_by_handle(PHONE) is None
    # Another sender is not locked by this one's misses.
    assert _inbound(c, "+15550100124", f"care {code}").status_code == 200


# --- refusals reach the texter ---------------------------------------------

def test_the_burst_limit_answers_in_words(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    monkeypatch.setattr(cfg, "chat_turns_per_window", 1)
    assert _inbound(c, PHONE, "one").status_code == 202
    r = _inbound(c, PHONE, "two")
    assert r.status_code == 200
    assert r.get_json() == {"reply": imessage.BUSY_TEXT}


def test_no_workers_answers_in_words(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    deps = app.extensions["careagents_imessage"]
    monkeypatch.setattr(deps, "workers_ready", lambda: False)
    r = _inbound(c, PHONE, "hi")
    assert r.status_code == 503
    assert r.get_json()["reply"] == imessage.UNAVAILABLE_TEXT


def test_a_paused_account_hears_so_through_the_runs_endpoint(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    _pair(c, agent_id)
    assert svc.set_paused("gene@example.com", True) is True
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: pytest.fail("paused turn hit a model"))
    run_id = _inbound(c, PHONE, "hi").get_json()["run_id"]
    _run(app)
    r = c.get(f"/api/surfaces/imessage/runs/{run_id}", headers=HDRS,
              query_string={"handle": PHONE})
    assert r.get_json()["reply"] == beta.PAUSED_TEXT


# --- disconnect from the web ---------------------------------------------------

def test_settings_disconnects_imessage(cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    page = c.get("/settings").get_data(as_text=True)
    assert 'id="im-disconnect"' not in page
    _pair(c, agent_id)
    page = c.get("/settings").get_data(as_text=True)
    assert 'id="im-disconnect"' in page and "connected" in page
    r = c.post("/api/surfaces/imessage/disconnect")
    assert r.status_code == 200 and r.get_json()["removed"] == 1
    assert svc.find_surface_by_handle(PHONE) is None
    # Not opted out: texting again offers a fresh link.
    assert "/link/" in _inbound(c, PHONE, "hi").get_json()["reply"]
    assert app.test_client().post(
        "/api/surfaces/imessage/disconnect").status_code == 401


# --- the Mac relay -------------------------------------------------------------

def _relay(monkeypatch):
    path = (Path(__file__).resolve().parents[1]
            / "deploy" / "careagents" / "imessage_relay.py")
    spec = importlib.util.spec_from_file_location("imessage_relay_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []
    monkeypatch.setattr(mod, "_send_imessage",
                        lambda h, t: sent.append((h, t)))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod, sent


def test_relay_sends_the_inbound_reply_and_then_the_answer(monkeypatch):
    mod, sent = _relay(monkeypatch)
    monkeypatch.setattr(mod, "_post", lambda p, b: {
        "reply": "welcome", "run_id": "run-1"})
    polls = iter([(202, {}), (503, {}), (200, {"reply": "answer"})])
    monkeypatch.setattr(mod, "_get", lambda p, q: next(polls))
    mod._handle_message(PHONE, "hi", 7)
    assert sent == [(PHONE, "welcome"), (PHONE, "answer")]


def test_relay_sends_a_refusal_reply_with_no_run(monkeypatch):
    mod, sent = _relay(monkeypatch)
    monkeypatch.setattr(mod, "_post", lambda p, b: {"reply": "One moment"})
    monkeypatch.setattr(mod, "_get",
                        lambda p, q: pytest.fail("nothing to poll"))
    mod._handle_message(PHONE, "hi")
    assert sent == [(PHONE, "One moment")]


def test_relay_says_so_when_a_run_times_out(monkeypatch):
    mod, sent = _relay(monkeypatch)
    monkeypatch.setattr(mod, "_post", lambda p, b: {"run_id": "run-1"})
    monkeypatch.setattr(mod, "_get", lambda p, q: (202, {}))
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    mod._handle_message(PHONE, "hi")
    assert sent == [(PHONE, mod.TIMEOUT_TEXT)]
    assert mod.TIMEOUT_TEXT.startswith("That took too long.")


def test_relay_masks_handles_in_its_log(monkeypatch, capsys):
    import subprocess
    mod, _ = _relay(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "imessage_relay_t2", mod.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    def _fail(*a, **k):
        raise subprocess.CalledProcessError(1, "osascript", stderr=b"x")
    monkeypatch.setattr(fresh.subprocess, "run", _fail)
    fresh._send_imessage(PHONE, "private words")
    err = capsys.readouterr().err
    assert PHONE not in err and "private words" not in err


def test_relay_keeps_one_senders_messages_in_order(monkeypatch):
    mod, _ = _relay(monkeypatch)
    seen = []
    monkeypatch.setattr(mod, "_handle_message",
                        lambda h, t, r=0: seen.append((h, t)))
    d = mod._Dispatcher(workers=2)
    for i in range(20):
        d.submit(PHONE, f"m{i}", i + 1)
        d.submit("+15550100124", f"n{i}", i + 100)
    d._pool.shutdown(wait=True)
    assert [t for h, t in seen if h == PHONE] == [f"m{i}" for i in range(20)]
    assert len(seen) == 40
