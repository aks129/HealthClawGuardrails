"""Security recheck of #993 after the F1/F2 fix: the wide inbox denies.

Grants match the narrow `invite_key`; every denial (a revoked alias, a
second place, a join) matches the wide `inbox_key`. These tests try to
get real records or a second stage 1 place for an inbox that is
un-invited or revoked, through aliases, old rows and races, on SQLite and
on Postgres when CARE_TEST_DATABASE_URL is set. Synthetic addresses only.
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import text

from careagents.accounts import AccountService
from careagents.models import RealRecordInvite
from tests.careagents_stage1_helpers import allowlist_cfg
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    _post, made, sent)

PG = os.environ.get("CARE_TEST_DATABASE_URL", "")
URLS = ["sqlite"] + (["postgres"] if PG.startswith("postgres") else [])


@pytest.fixture(params=URLS)
def service(request, tmp_path):
    url = PG if request.param == "postgres" else f"sqlite:///{tmp_path}/r.db"
    svc = AccountService(allowlist_cfg(CARE_DATABASE_URL=url))
    yield svc
    svc.engine.dispose()


def _rows(svc) -> dict:
    with svc.session() as s:
        return {r.email: r.revoked_at for r in s.query(RealRecordInvite)}


def _old_rows(svc, rows) -> None:
    """Rows as an older build left them: two keys on one non-Gmail inbox
    (+tags are kept apart by the narrow key, so boot does not merge them)."""
    with svc.engine.begin() as conn:
        for email, at, revoked in rows:
            conn.execute(text(
                "INSERT INTO ca_real_record_invites (email, invited_at, "
                "invited_by, revoked_at) VALUES (:e, :a, 'owner', :r)"),
                {"e": email, "a": at, "r": revoked})


def test_old_rows_one_revoked_count_the_inbox_closed(service):
    _old_rows(service, [("carol@corp.example", 1.0, 5.0),
                        ("carol+x@corp.example", 2.0, None)])
    for v in ("carol+x@corp.example", "CAROL+X@corp.example",
              "carol@corp.example", "carol+y@corp.example"):
        assert service.real_records_invited(v) is False, v
    # Neither a link grant nor the operator adds a place on that inbox.
    assert service.grant_join_invite("carol+y@corp.example", "acct") \
        == "revoked"
    with pytest.raises(ValueError):
        service.invite_real_records("carol+z@corp.example", "owner")
    assert len(_rows(service)) == 2


def test_old_rows_both_live_take_no_third_place(service):
    _old_rows(service, [("dan@corp.example", 1.0, None),
                        ("dan+x@corp.example", 2.0, None)])
    assert service.grant_join_invite("dan+y@corp.example", "acct") == "live"
    with pytest.raises(ValueError):
        service.invite_real_records("dan+z@corp.example", "owner")
    assert set(_rows(service)) == {"dan@corp.example", "dan+x@corp.example"}
    # One revoke through a third alias closes both.
    assert service.revoke_real_records_invite("Dan+Q@corp.example.") is True
    assert all(v is not None for v in _rows(service).values())
    for v in ("dan@corp.example", "dan+x@corp.example"):
        assert service.real_records_invited(v) is False, v


@pytest.mark.parametrize("alias", [
    "erin+x@corp.example", "erin@CORP.example.", "erin@corp.example..",
    "erin+x@ｃｏｒｐ.example", "erin@corp.example。", " Erin+1@Corp.Example "])
def test_a_revoked_inbox_stays_shut_through_any_alias(service, alias):
    service.invite_real_records("erin@corp.example", "owner")
    assert service.revoke_real_records_invite("erin@corp.example")
    assert service.real_records_invited(alias) is False
    if "\u3002" in alias:
        return      # the /beta form refuses it, so no join reaches a grant
    assert service.grant_join_invite(alias, "acct") == "revoked"
    assert service.real_records_invited(alias) is False
    live = [k for k, r in _rows(service).items() if r is None]
    assert live == [], live


@pytest.mark.parametrize("typed", [
    "frank@corp.example.", "frank@corp.example..", "FRANK@corp.example.",
    "frank@corp.example. "])
def test_a_trailing_dot_never_grants(service, typed):
    service.invite_real_records("frank@corp.example", "owner")
    assert service.real_records_invited(typed) is False


def test_a_trailing_dot_invite_revokes_through_the_plain_address(service):
    service.invite_real_records("gail@corp.example.", "owner")
    assert service.real_records_invited("gail@corp.example") is True
    assert service.revoke_real_records_invite("gail@corp.example") is True
    assert service.real_records_invited("gail@corp.example") is False


def _race(fns):
    barrier = threading.Barrier(len(fns))
    out, errors = [None] * len(fns), []

    def run(i, fn):
        barrier.wait()
        try:
            out[i] = fn()
        except ValueError as exc:
            out[i] = f"refused: {exc}"[:40]
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(f"{type(exc).__name__}: {exc}"[:200])
    ts = [threading.Thread(target=run, args=(i, f))
          for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out, errors


def test_racing_aliases_take_one_place(service):
    """Four aliases of one inbox granted at once (links and the operator):
    one row at most, whatever the order."""
    for trial in range(15):
        inbox = f"hal{trial}"
        out, errors = _race([
            lambda: service.grant_join_invite(f"{inbox}+a@corp.example", "x"),
            lambda: service.grant_join_invite(f"{inbox}+b@corp.example", "y"),
            lambda: service.invite_real_records(f"{inbox}@corp.example", "o"),
            lambda: service.grant_join_invite(f"{inbox}+c@Corp.Example", "z"),
        ])
        assert errors == [], (trial, errors)
        mine = [k for k in _rows(service) if k.startswith(inbox + "@")
                or k.startswith(inbox + "+")]
        assert len(mine) == 1, (trial, mine, out)


def test_a_join_racing_a_revoke_never_reopens_the_inbox(service):
    for trial in range(15):
        inbox = f"ivy{trial}"
        service.invite_real_records(f"{inbox}@corp.example", "owner")
        out, errors = _race([
            lambda: service.revoke_real_records_invite(
                f"{inbox}@corp.example"),
            lambda: service.grant_join_invite(f"{inbox}+x@corp.example",
                                              "acct"),
        ])
        assert errors == [], (trial, errors)
        assert out[0] is True
        for v in (f"{inbox}@corp.example", f"{inbox}+x@corp.example"):
            assert service.real_records_invited(v) is False, (trial, v, out)


@pytest.mark.parametrize("alias", [
    "erin@corp.example\u3002", "erin@corp\uff0eexample",
    "erin@corp.example\uff61", "erin+x@corp.example\uff0e"])
def test_a_unicode_dot_alias_never_reaches_a_join_grant(made, sent, alias):  # noqa: F811
    """`invite_key` and `inbox_key` both fall back to the typed address for
    a domain `ascii_email` refuses, so the wide denial cannot see a revoked
    `erin@` from `erin@corp.example\u3002`. What keeps that closed is the
    /beta form refusing the address before any link join is recorded."""
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",
                    CARE_REAL_RECORDS_ALLOWLIST="friend@example.com")
    c = app.test_client()
    c.get("/beta")
    r = _post(c, ip="203.0.113.77", email=alias, first_name="Erin")
    assert r.status_code == 400
    assert [m for m in sent if m[0] == alias] == []
