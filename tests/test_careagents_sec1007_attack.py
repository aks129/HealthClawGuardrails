"""Security tester probes for #1007 (a STOP always wins, #1005).

The PR makes bind, STOP and disconnect take the handle's state row first
(SELECT ... FOR UPDATE). These probe the lock order it introduced and the
paths it leaves open. Each race is made deterministic by holding one side
at a fixed point while the other runs, not by luck of timing.

Postgres-only probes skip without CARE_TEST_DATABASE_URL (CI's Postgres
lane sets it). Synthetic handles only (555 numbers, example.com).
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from careagents import accounts as A
from careagents.accounts import AccountService
from careagents.config import Config
from careagents.models import Account, Base, ImessageLink, make_engine
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _chat_app, cfg, svc)

PG = os.environ.get("CARE_TEST_DATABASE_URL", "")
pg_only = pytest.mark.skipif(not PG.startswith("postgres"),
                             reason="needs a database with row locks")
PHONE = "+15550100177"


def _pg_svc():
    engine = make_engine(PG)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return AccountService(Config(env={
        "CARE_DATABASE_URL": PG, "CARE_RP_ID": "localhost",
        "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
        "HEALTHCLAW_MINT_SECRET": "m"}))


def _acct(svc, email="sec1007@example.com"):  # noqa: F811
    with svc.session() as s:
        a = Account(email=email)
        s.add(a)
        s.flush()
        return a.id


def _agent(svc, aid):  # noqa: F811
    cid = svc.add_connection(aid, "fhir", f"t-{aid}", "Synthetic")
    return svc.create_agent(aid, "Helper", "default", cid)


def _both(first, second, second_after=0.1):
    """Run `first` now and `second` a beat later, in two threads; return
    (results, exceptions). 30s join: a hang shows as a missing result."""
    out, errs = {}, []

    def go(name, f, delay):
        time.sleep(delay)
        try:
            out[name] = f()
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
    ts = [threading.Thread(target=go, args=("first", first, 0)),
          threading.Thread(target=go, args=("second", second, second_after))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return out, errs


_HOLD = threading.local()


def _hold_after_state_lock(monkeypatch, seconds):
    """Make the calling thread sleep right after it takes (and, first time,
    inserts) the handle's state row, when _HOLD.on is set for it."""
    real = AccountService._locked_handle_state

    def held(self, s, handle, create=True):
        st = real(self, s, handle, create)
        if getattr(_HOLD, "on", False):
            time.sleep(seconds)
        return st
    monkeypatch.setattr(AccountService, "_locked_handle_state", held)


def _held(f):
    def g():
        _HOLD.on = True
        try:
            return f()
        finally:
            _HOLD.on = False
    return g


# --- F1: new lock-order inversion, link issue vs STOP (regression) ----------

@pg_only
def test_stop_racing_a_link_issue_for_the_same_number_does_not_deadlock(
        monkeypatch):
    """A returning number whose last link expired texts "hi" and then STOP.
    The issue prunes the expired link (row lock on it), then updates the
    state row; STOP now locks the state row first, then deletes the link.
    Opposite orders: Postgres kills one. Base (ac1d9651) took links then
    state in both, and never deadlocked here."""
    svc = _pg_svc()  # noqa: F811
    svc.issue_imessage_link(PHONE)
    with svc.session() as s:
        s.query(ImessageLink).filter_by(handle=PHONE).update(
            {"exp": time.time() - 5})
    real_prune = A._prune_links

    def slow_prune(s):
        n = real_prune(s)
        if getattr(_HOLD, "on", False):
            time.sleep(0.3)
        return n
    monkeypatch.setattr(A, "_prune_links", slow_prune)
    out, errs = _both(_held(lambda: svc.issue_imessage_link(PHONE)),
                      lambda: svc.imessage_stop(PHONE))
    try:
        assert errs == [], errs
        assert svc.imessage_opted_out(PHONE)
        with svc.session() as s:
            assert s.query(ImessageLink).filter_by(handle=PHONE).count() == 0
    finally:
        svc.engine.dispose()


# --- F3: first-ever text, the state-row insert collides ---------------------

@pg_only
def test_stop_racing_a_first_ever_pairing_bind_still_wins(monkeypatch):
    svc = _pg_svc()  # noqa: F811
    aid = _acct(svc)
    sid = svc.add_surface(aid, _agent(svc, aid), "imessage", None)
    _hold_after_state_lock(monkeypatch, 0.3)
    try:
        out, errs = _both(
            _held(lambda: svc.bind_imessage_handle(
                aid, PHONE, pending_surface_id=sid, also=PHONE)),
            lambda: svc.imessage_stop(PHONE, also=PHONE))
        assert errs == [], errs
        assert svc.find_surface_by_handle(PHONE) is None
        assert svc.imessage_opted_out(PHONE)
    finally:
        svc.engine.dispose()


# --- F5: disconnect acts on rows read before it took the lock ---------------

@pg_only
def test_disconnect_racing_a_rebind_never_reports_a_bound_number_removed(
        monkeypatch):
    svc = _pg_svc()  # noqa: F811
    aid = _acct(svc)
    agent = _agent(svc, aid)
    assert svc.bind_imessage_handle(aid, PHONE) == "connected"
    old = svc.find_surface_by_handle(PHONE)["id"]
    pending = svc.add_surface(aid, agent, "imessage", None)
    _hold_after_state_lock(monkeypatch, 0.3)
    try:
        out, errs = _both(
            _held(lambda: svc.bind_imessage_handle(
                aid, PHONE, pending_surface_id=pending, also=PHONE)),
            lambda: svc.disconnect_imessage(aid, old))
        assert errs == [], errs
        assert not (out.get("second") == 1
                    and svc.find_surface_by_handle(PHONE) is not None)
    finally:
        svc.engine.dispose()


# --- F2: pairing-code path, a STOP after the code text still loses ----------

def test_stop_processed_before_a_code_bind_it_followed_still_wins(
        cfg, svc, monkeypatch):  # noqa: F811
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    real = svc.bind_imessage_handle

    def stop_lands_first(*a, **kw):
        # The code text was received; the STOP texted right after it is
        # processed (Sendblue delivers webhooks concurrently) first.
        svc.imessage_stop(PHONE, also=PHONE)
        return real(*a, **kw)
    monkeypatch.setattr(svc, "bind_imessage_handle", stop_lands_first)
    r = c.post("/api/surfaces/imessage/inbound",
               headers={"X-Internal-Secret": "mint-secret"},
               json={"handle": PHONE, "text": f"care {code}"})
    assert svc.find_surface_by_handle(PHONE) is None
    assert svc.imessage_opted_out(PHONE)
    # Nothing more is texted to a number that just opted out.
    assert r.status_code == 200 and not r.get_json().get("reply")


def test_a_stop_before_the_code_is_parsed_still_beats_that_code(
        cfg, svc, monkeypatch):  # noqa: F811
    """The receive time is taken when the text arrives, not when
    bind_by_code starts: a STOP processed while the code text is still
    being looked up (handle_inbound's surface lookup) also wins. Pins the
    capture in handle_inbound; bind_by_code's own default would miss it."""
    app, c, fake, agent_id, *_ = _chat_app(cfg, svc, monkeypatch)
    code = c.post("/api/surfaces/imessage",
                  json={"agent_id": agent_id}).get_json()["code"]
    real = svc.find_surface_by_handle
    fired = []

    def stop_during_lookup(*a, **kw):
        if not fired:
            fired.append(1)
            svc.imessage_stop(PHONE, also=PHONE)
        return real(*a, **kw)
    monkeypatch.setattr(svc, "find_surface_by_handle", stop_during_lookup)
    r = c.post("/api/surfaces/imessage/inbound",
               headers={"X-Internal-Secret": "mint-secret"},
               json={"handle": PHONE, "text": f"care {code}"})
    assert fired
    assert svc.find_surface_by_handle(PHONE) is None
    assert svc.imessage_opted_out(PHONE)
    assert r.status_code == 200 and not r.get_json().get("reply")


# --- F4: SQLite, the bind's read must take the write lock -----------------

@pytest.mark.skipif(PG.startswith("postgres"),
                    reason="SQLite behaviour; Postgres is F1-F3, F5")
def test_sqlite_stop_racing_a_link_bind_still_unbinds(tmp_path, monkeypatch):
    """sec #1007 F4. SQLite drops FOR UPDATE, so without a write first the
    bind's check takes no lock and a STOP commits between the check and
    the insert: bound while opted out. With the write lock, the STOP waits
    for the bind and then unbinds what it bound.

    Two threads, the bind held right after its lock: a STOP from the same
    thread would only wait out SQLite's busy timeout."""
    url = f"sqlite:///{tmp_path / 'sec1007.db'}"
    s2 = AccountService(Config(env={
        "CARE_DATABASE_URL": url, "CARE_RP_ID": "localhost",
        "CARE_ORIGIN": "http://localhost", "OPENAI_API_KEY": "k",
        "HEALTHCLAW_MINT_SECRET": "m"}))
    aid = _acct(s2)
    link_id = s2.peek_imessage_link(s2.issue_imessage_link(PHONE))
    _hold_after_state_lock(monkeypatch, 0.5)
    try:
        out, errs = _both(_held(lambda: s2.claim_imessage_link(link_id, aid)),
                          lambda: s2.imessage_stop(PHONE))
        assert errs == [], errs
        assert set(out) == {"first", "second"}, out
        assert s2.find_surface_by_handle(PHONE) is None
        assert s2.imessage_opted_out(PHONE)
    finally:
        s2.engine.dispose()
