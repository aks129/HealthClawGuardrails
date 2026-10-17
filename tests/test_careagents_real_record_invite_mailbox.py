"""Real-record invites match the inbox, not the typed string.

`flask invites add <email>` opens real records to one person in
`CARE_REAL_RECORDS=allowlist`. The invite is keyed by `invites.invite_key`:
case and the domain's ASCII form on every domain, and on Gmail the dots, a
+tag and googlemail too, so the person signing in as `Jane.Doe@gmail.com`
matches an invite for `janedoe+beta@googlemail.com`. On any other domain a
+tag is kept: `bob+x@` may be someone other than `bob@`. The typed address is kept for the owner's
list. Rows written before this keyed by the typed address; boot rewrites
them, on SQLite and Postgres, with web and worker starting together.

The gate fails closed: anyone whose inbox holds no live invite is refused.
Synthetic addresses only.
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, text

from careagents import invites
from careagents.app import create_app
from careagents.models import Base, RealRecordInvite, make_engine
from tests.careagents_consent_helpers import consented
from tests.careagents_stage1_helpers import allowlist_cfg, approve_terms
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _beta_app, _login, cfg, svc)
from tests.test_careagents_beta_signup import (  # noqa: F401  (fixtures)
    made, sent)
from tests.test_careagents_invite_links import _code, _join_via

PG = os.environ.get("CARE_TEST_DATABASE_URL", "")
URLS = ["sqlite"] + (["postgres"] if PG.startswith("postgres") else [])

INVITED = "Jane.Doe+beta@Gmail.com"
VARIANTS = ("jane.doe+beta@gmail.com", "janedoe@gmail.com",
            "JANEDOE@GMAIL.COM", "j.a.n.e.d.o.e@gmail.com",
            "jane.doe+other@googlemail.com", "  Jane.Doe@GoogleMail.com ")
OTHERS = ("janedoe2@gmail.com", "jane.doe@example.com", "janedoe@gmail.co",
          "jane_doe@gmail.com", "doe.jane@gmail.com")


def _starts_real(app, svc, monkeypatch, email) -> int:  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    return c.post("/api/connections/direct", json=consented()).status_code


# --- matching ---------------------------------------------------------------

def test_variants_of_one_invited_address_match(svc):  # noqa: F811
    assert svc.invite_real_records(INVITED, "owner") is True
    for v in VARIANTS:
        assert svc.real_records_invited(v) is True, v


def test_a_variant_opens_real_records_through_the_gate(svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records(INVITED, "owner")
    assert _starts_real(app, svc, monkeypatch, "j.a.n.e.doe@gmail.com") == 200


def test_a_different_mailbox_does_not_match(svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records(INVITED, "owner")
    for other in OTHERS:
        assert svc.real_records_invited(other) is False, other
    assert _starts_real(app, svc, monkeypatch, "janedoe2@gmail.com") == 503


def test_a_non_gmail_domain_is_not_dot_folded(svc):  # noqa: F811
    svc.invite_real_records("Jane.Doe@Example.org", "owner")
    assert svc.real_records_invited("janedoe@example.org") is False
    assert svc.real_records_invited("j.anedoe@example.org") is False
    # Case folds on every domain.
    assert svc.real_records_invited("JANE.DOE@example.ORG") is True


def test_a_non_gmail_plus_tag_is_another_person(svc, monkeypatch):  # noqa: F811
    """Fail closed: off Gmail, `+` may be an ordinary character, so a +tag
    variant is a different mailbox, in both directions."""
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records("bob@corp.example", "owner")
    assert svc.real_records_invited("bob+x@corp.example") is False
    assert svc.real_records_invited("Bob@Corp.Example") is True
    assert _starts_real(app, svc, monkeypatch, "bob+x@corp.example") == 503
    svc.invite_real_records("carol+lab@corp.example", "owner")
    assert svc.real_records_invited("carol@corp.example") is False
    assert svc.real_records_invited("carol+lab@corp.example") is True


def test_the_deny_side_counts_the_inbox_wide(svc):  # noqa: F811
    """Granting is narrow; refusing is wide (#993 F1). Off Gmail, a +tag
    of a revoked or invited address cannot get, hold or add an invite."""
    svc.invite_real_records("carol@corp.example", "owner")
    # The operator cannot open a second place on the same inbox.
    with pytest.raises(ValueError, match="same inbox"):
        svc.invite_real_records("carol+x@corp.example", "owner")
    assert svc.grant_join_invite("carol+y@corp.example", "acct_x") == "live"
    # Revoking any +tag revokes the inbox.
    assert svc.revoke_real_records_invite("Carol+Z@corp.example") is True
    assert svc.real_records_invited("carol@corp.example") is False
    assert svc.grant_join_invite("carol+y@corp.example", "acct_x") == "revoked"
    with pytest.raises(ValueError, match="same inbox"):
        svc.invite_real_records("carol+x@corp.example", "owner")
    with svc.session() as s:
        assert s.query(RealRecordInvite).count() == 1


def test_a_live_alias_row_is_refused_while_its_inbox_is_revoked(svc):  # noqa: F811
    """Two rows on one wide inbox can only come from before this change.
    A revoke on either side refuses both."""
    import time
    with svc.session() as s:
        s.add(RealRecordInvite(email="erin+x@corp.example", invited_by="o",
                               invited_at=time.time()))
        s.add(RealRecordInvite(email="erin@corp.example", invited_by="o",
                               invited_at=time.time(),
                               revoked_at=time.time()))
    assert svc.real_records_invited("erin+x@corp.example") is False


def test_a_trailing_dot_is_folded_for_storing_and_never_grants(svc):  # noqa: F811
    """#993 F2: `dave@corp.example.` is stored as `dave@corp.example`, so
    the plain address revokes it; an address typed with the dot is never
    granted."""
    svc.invite_real_records("Dave@Corp.Example.", "owner")
    with svc.session() as s:
        [row] = s.query(RealRecordInvite).all()
        assert row.email == "dave@corp.example"
        assert row.display_email == "dave@corp.example."
    assert svc.real_records_invited("dave@corp.example") is True
    assert svc.real_records_invited("dave@corp.example.") is False
    assert svc.invite_real_records("dave@corp.example", "owner") is False
    assert svc.revoke_real_records_invite("dave@corp.example") is True
    assert svc.real_records_invited("dave@corp.example") is False


def test_a_gmail_plus_tag_still_matches(svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    svc.invite_real_records("bob@gmail.com", "owner")
    assert svc.real_records_invited("bob+x@gmail.com") is True
    assert svc.real_records_invited("b.o.b+y@googlemail.com") is True
    assert _starts_real(app, svc, monkeypatch, "bob+x@gmail.com") == 200


def test_the_allowlist_refuses_anyone_not_invited(svc, monkeypatch):  # noqa: F811
    """The real-records gate fails closed: no invite, a revoked invite, or
    an empty or malformed address is a refusal."""
    approve_terms(monkeypatch)
    app = _beta_app(svc, CARE_REAL_RECORDS="allowlist")
    for nobody in ("", None, "@", "+x@gmail.com", "gmail.com",
                   "janedoe@gmail.com"):
        assert svc.real_records_invited(nobody) is False, nobody
    assert _starts_real(app, svc, monkeypatch, "janedoe@gmail.com") == 503

    svc.invite_real_records(INVITED, "owner")
    # Revoking through any variant revokes the inbox.
    assert svc.revoke_real_records_invite("JaneDoe@googlemail.com") is True
    for v in VARIANTS:
        assert svc.real_records_invited(v) is False, v
    assert _starts_real(app, svc, monkeypatch, "jane.doe@gmail.com") == 503


def test_an_invite_is_still_no_way_around_off(svc, monkeypatch):  # noqa: F811
    approve_terms(monkeypatch)
    app = _beta_app(svc)            # CARE_REAL_RECORDS unset is off
    svc.invite_real_records(INVITED, "owner")
    assert _starts_real(app, svc, monkeypatch, "janedoe@gmail.com") == 503


# --- storage ----------------------------------------------------------------

def test_the_row_is_keyed_by_the_inbox_and_keeps_what_was_typed(svc):  # noqa: F811
    svc.invite_real_records(INVITED, "owner")
    with svc.session() as s:
        [row] = s.query(RealRecordInvite).all()
        assert row.email == "janedoe@gmail.com"
        assert row.display_email == "jane.doe+beta@gmail.com"


def test_a_variant_of_an_invited_inbox_is_already_invited(svc):  # noqa: F811
    """One row per inbox: a second alias neither adds a row nor takes a
    second stage 1 place."""
    svc.invite_real_records(INVITED, "owner")
    assert svc.invite_real_records("JaneDoe@googlemail.com", "ops") is False
    with svc.session() as s:
        assert s.query(RealRecordInvite).count() == 1


def test_reinviting_a_revoked_inbox_through_a_variant_reopens_it(svc):  # noqa: F811
    svc.invite_real_records(INVITED, "owner")
    svc.revoke_real_records_invite(INVITED)
    assert svc.invite_real_records("jane.doe@googlemail.com", "ops") is True
    assert svc.real_records_invited("janedoe@gmail.com") is True
    with svc.session() as s:
        [row] = s.query(RealRecordInvite).all()
        assert row.display_email == "jane.doe@googlemail.com"
        assert row.invited_by == "ops"


# --- the owner's list -------------------------------------------------------

def _runner():
    from careagents.accounts import AccountService
    c = allowlist_cfg()
    service = AccountService(c)
    app = create_app(config=c, client=FakeClient(), accounts=service)
    app.config["TESTING"] = True
    return app, app.test_cli_runner(), service


def test_list_shows_the_match_key_and_who_has_signed_up(monkeypatch):
    app, run, service = _runner()
    r = run.invoke(args=["invites", "add", INVITED, "--by", "owner"])
    assert r.exit_code == 0, r.output
    assert "invited jane.doe+beta@gmail.com" in r.output
    assert "janedoe@gmail.com" in r.output       # what it will match
    run.invoke(args=["invites", "add", "late@example.org", "--by", "owner"])

    out = run.invoke(args=["invites", "list"]).output
    lines = {ln.split()[0]: ln for ln in out.splitlines()}
    assert "janedoe@gmail.com" in lines["jane.doe+beta@gmail.com"]
    assert "not signed up" in lines["jane.doe+beta@gmail.com"]
    assert "not signed up" in lines["late@example.org"]

    # The invitee signs up under another alias of the same inbox.
    _login(app.test_client(), service, monkeypatch,
           email="Jane.Doe@gmail.com")
    out = run.invoke(args=["invites", "list"]).output
    lines = {ln.split()[0]: ln for ln in out.splitlines()}
    assert "not signed up" not in lines["jane.doe+beta@gmail.com"]
    assert "signed up" in lines["jane.doe+beta@gmail.com"]
    assert "not signed up" in lines["late@example.org"]
    service.engine.dispose()


# --- the boot migration -----------------------------------------------------

#: Rows as the code before this wrote them: keyed by the typed address,
#: lowercased, with no display column. A non-Gmail +tag row keeps its own
#: key; a trailing dot on a domain is dropped. Two aliases of one Gmail
#: inbox may both
#: exist; the latest owner action between them wins (revoked at 30 beats
#: invited at 20), and on a tie, revoked.
OLD = [("jane.doe+beta@gmail.com", 10.0, "owner", None),
       ("bob+x@example.org", 11.0, "owner", None),
       ("plain@example.org", 12.0, "owner", None),
       ("a.b@gmail.com", 20.0, "owner", None),
       ("ab+old@googlemail.com", 5.0, "owner", 30.0),
       ("c.d@gmail.com", 40.0, "owner", None),
       ("cd@gmail.com", 1.0, "owner", 40.0),
       ("e.f@gmail.com", 1.0, "owner", 50.0),
       ("ef@gmail.com", 60.0, "ops", None),
       ("dave@corp.example.", 70.0, "owner", None)]

WANT = {"janedoe@gmail.com": ("jane.doe+beta@gmail.com", "owner", None),
        "bob+x@example.org": (None, "owner", None),
        "plain@example.org": (None, "owner", None),
        "ab@gmail.com": ("ab+old@googlemail.com", "owner", 30.0),
        "cd@gmail.com": (None, "owner", 40.0),
        "ef@gmail.com": (None, "ops", None),
        "dave@corp.example": ("dave@corp.example.", "owner", None)}


def _old_database(kind, tmp_path) -> str:
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables
                        if t.name != "ca_real_record_invites"])
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_real_record_invites ("
            "email VARCHAR(255) PRIMARY KEY, invited_at FLOAT NOT NULL, "
            "invited_by VARCHAR(255) NOT NULL, revoked_at FLOAT)"))
        for e, at, by, rev in OLD:
            conn.execute(text(
                "INSERT INTO ca_real_record_invites VALUES (:e, :a, :b, :r)"),
                {"e": e, "a": at, "b": by, "r": rev})
    engine.dispose()
    return url


def _rows(url) -> dict:
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(text(
            "SELECT email, display_email, invited_by, revoked_at "
            "FROM ca_real_record_invites"))}
    engine.dispose()
    return rows


@pytest.mark.parametrize("kind", URLS)
def test_boot_rekeys_existing_invites_by_inbox(kind, tmp_path):
    url = _old_database(kind, tmp_path)
    make_engine(url).dispose()
    assert _rows(url) == WANT
    # A second boot changes nothing.
    make_engine(url).dispose()
    assert _rows(url) == WANT


@pytest.mark.parametrize("kind", URLS)
def test_an_old_invite_matches_its_variants_after_boot(kind, tmp_path):
    from careagents.accounts import AccountService
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/old.db"
    # allowlist_cfg empties a Postgres database: build it first.
    config = allowlist_cfg(CARE_DATABASE_URL=url)
    _old_database(kind, tmp_path)
    service = AccountService(config)
    try:
        assert service.real_records_invited("Jane.Doe@googlemail.com") is True
        assert service.real_records_invited("BOB+x@example.org") is True
        assert service.real_records_invited("bob@example.org") is False
        assert service.real_records_invited("dave@corp.example") is True
        assert service.revoke_real_records_invite("dave@corp.example")
        assert service.real_records_invited("a.b@gmail.com") is False   # revoked
        assert service.real_records_invited("e.f+z@gmail.com") is True
        assert service.real_records_invited("stranger@example.org") is False
    finally:
        service.engine.dispose()


@pytest.mark.parametrize("kind", URLS)
def test_web_and_worker_booting_together_both_come_up(kind, tmp_path,
                                                     caplog):
    """Production starts web and worker at once on the same database. Every
    boot comes up and the rows are rewritten once. No boot needed the
    retry: the lock serialises them, and the retry is only a backstop that
    would otherwise hide a race."""
    import logging
    caplog.set_level(logging.WARNING, logger="careagents.models")
    for trial in range(25):
        (tmp_path / str(trial)).mkdir()
        url = _old_database(kind, tmp_path / str(trial))
        n = 4
        barrier = threading.Barrier(n)
        errors = []

        def boot():
            barrier.wait()
            try:
                make_engine(url).dispose()
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
        threads = [threading.Thread(target=boot) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"trial {trial}: {errors}"
        retried = [r.getMessage() for r in caplog.records
                   if "migration retried" in r.getMessage()]
        assert retried == [], f"trial {trial}: {retried}"
        assert _rows(url) == WANT, f"trial {trial}"


def test_invite_key_folds_like_the_caps_on_gmail_only():
    from careagents import beta_signup
    for v in VARIANTS + OTHERS:
        if v.strip().lower().endswith(("@gmail.com", "@googlemail.com")):
            assert invites.invite_key(v) == beta_signup.account_key(v), v
    assert invites.invite_key(INVITED) == "janedoe@gmail.com"
    assert invites.invite_key(" Bob+X@Corp.Example ") == "bob+x@corp.example"
    assert invites.invite_key("a.b@corp.example") == "a.b@corp.example"
    assert invites.invite_key("") == invites.invite_key(None) == ""


def test_a_join_on_an_inbox_invited_under_another_address_is_none(
        made, sent, monkeypatch):  # noqa: F811
    """carol@ holds a live invite. Carol joining a link as carol+x@ gets
    no invite and the hub's "none" line, not "had": carol+x@ itself is not
    invited (the grant is narrow)."""
    approve_terms(monkeypatch)
    app, svc = made(RESEND_API_KEY="re_test", CARE_REAL_RECORDS="allowlist",  # noqa: F811
                    CARE_REAL_RECORDS_ALLOWLIST="friend@example.com")
    svc.invite_real_records("carol@corp.example", "operator")
    c = app.test_client()
    _login(c, svc, monkeypatch, email="friend@example.com")
    _join_via(app, sent, _code(c), "carol+x@corp.example", first_run=False)
    assert invites.join_outcome(svc.session,
                                "carol+x@corp.example") == invites.NONE
    assert svc.real_records_invited("carol+x@corp.example") is False
