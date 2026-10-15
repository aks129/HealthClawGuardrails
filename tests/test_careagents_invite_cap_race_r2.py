"""Re-verify of #928 F1 on the fix: real threads, an inviter WITH real-record
access (so joins actually try to grant), on file SQLite and, when
CARE_TEST_PG_URL is set, Postgres. Synthetic data only."""

from __future__ import annotations

import os
import threading

import pytest

from careagents import beta, invites
from careagents.config import Config
from careagents.models import RealRecordInvite

URLS = ["sqlite"] + (["pg"] if os.environ.get("CARE_TEST_PG_URL") else [])


def _svc(kind, tmp_path):
    from careagents.accounts import AccountService
    from careagents.models import Base, make_engine
    if kind == "pg":
        url = os.environ["CARE_TEST_PG_URL"]
        eng = make_engine(url)
        Base.metadata.drop_all(eng)
        eng.dispose()
    else:
        url = f"sqlite:///{tmp_path}/race.db"
    cfg = Config(env={"CARE_DATABASE_URL": url, "OPENAI_API_KEY": "k",
                      "CARE_INVITE_LINK_CAP": "40",
                      "CARE_REAL_RECORDS": "allowlist",
                      "CARE_REAL_RECORDS_ALLOWLIST": "host@example.com"})
    return cfg, AccountService(cfg)


def _live(svc):
    with svc.session() as s:
        return (s.query(RealRecordInvite)
                .filter(RealRecordInvite.revoked_at.is_(None)).count())


def _run(fns):
    start = threading.Barrier(len(fns))
    out, errs = [], []

    def go(fn):
        start.wait()
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))
    ts = [threading.Thread(target=go, args=(f,)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out, errs


@pytest.mark.parametrize("kind", URLS)
def test_racing_link_joins_hold_the_stage1_cap(kind, tmp_path, monkeypatch):
    from tests.test_careagents import _make_account
    cfg, svc = _svc(kind, tmp_path)
    try:
        for i in range(beta.STAGE1_INVITE_CAP - 3):
            svc.invite_real_records(f"t{i}@example.com", "operator")
        host = _make_account(svc, monkeypatch, "host@example.com")
        assert invites.inviter_has_access(svc, cfg, host)
        link = invites.link_for(svc.session, host.id, 40)
        joiners = [f"racer{i}@example.org" for i in range(16)]
        for e in joiners:
            assert invites.note_pending(svc.session, e, link["code"], 40)
        out, errs = _run([lambda e=e: invites.on_join(svc, cfg, e)
                          for e in joiners])
        granted = [o for o in out if o and o.get("real_records") == "granted"]
        assert _live(svc) == beta.STAGE1_INVITE_CAP, (out, errs)
        assert len(granted) == 3, (out, errs)
    finally:
        svc.engine.dispose()


@pytest.mark.parametrize("kind", URLS)
def test_racing_operator_and_joins_hold_the_cap(kind, tmp_path, monkeypatch):
    from tests.test_careagents import _make_account
    cfg, svc = _svc(kind, tmp_path)
    try:
        for i in range(beta.STAGE1_INVITE_CAP - 2):
            svc.invite_real_records(f"t{i}@example.com", "operator")
        host = _make_account(svc, monkeypatch, "host@example.com")
        link = invites.link_for(svc.session, host.id, 40)
        joiners = [f"j{i}@example.org" for i in range(8)]
        for e in joiners:
            invites.note_pending(svc.session, e, link["code"], 40)

        def op(i):
            try:
                return svc.invite_real_records(f"op{i}@example.net", "operator")
            except ValueError:
                return "full"
        fns = ([lambda e=e: invites.on_join(svc, cfg, e) for e in joiners]
               + [lambda i=i: op(i) for i in range(8)])
        out, errs = _run(fns)
        assert _live(svc) == beta.STAGE1_INVITE_CAP, (out, errs)
        assert not errs, errs
    finally:
        svc.engine.dispose()
