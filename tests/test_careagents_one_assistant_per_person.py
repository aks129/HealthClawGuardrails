"""One assistant per person (spec PR #943, beta slice items 1, 2, 3, 5).

An account has people: itself ("self"), and later someone it cares for
("other"). Each person has one assistant. Its specialties are routines
(prompt blocks that change no access), and its tone is a setting. The
engine audit says which account acted.

Synthetic data only. Runs on SQLite, and the migration tests on Postgres
too when CARE_TEST_DATABASE_URL points at one.
"""
# ruff: noqa: F811  (the cfg and svc fixtures, imported then requested)

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from careagents import models
from careagents.models import Agent, Connection, Person
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _make_account, cfg, svc)

PG = os.environ.get("CARE_TEST_DATABASE_URL", "")
URLS = ["sqlite"] + (["postgres"] if PG.startswith("postgres") else [])


def _app(cfg, svc, client=None):  # noqa: F811
    from careagents.app import create_app
    # "Add someone you care for" is behind CARE_PEOPLE until the hub
    # renders people (#947 QA).
    cfg.people_enabled = True
    app = create_app(config=cfg, client=client or FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    return app


def _signed_in(cfg, svc, monkeypatch, email="pat@example.com"):  # noqa: F811
    app = _app(cfg, svc)
    c = app.test_client()
    _login(c, svc, monkeypatch, email=email)
    with svc.session() as s:
        aid = s.query(models.Account).filter_by(email=email).one().id
    return app, c, aid


def _people(svc, aid):  # noqa: F811
    with svc.session() as s:
        return [(p.kind, p.relation, p.nickname)
                for p in s.query(Person).filter_by(account_id=aid)
                .order_by(Person.created_at)]


def _agents(svc, aid):  # noqa: F811
    with svc.session() as s:
        return s.query(Agent).filter_by(account_id=aid).all()


def _legacy_agent(svc, aid, conn_id, name="Coach", advisor=None):  # noqa: F811
    """A second assistant as accounts from before this change hold them:
    a row written straight to the table, past the one-per-person rule."""
    with svc.session() as s:
        person = s.get(Connection, conn_id).person_id
        a = Agent(account_id=aid, connection_id=conn_id, name=name,
                  persona="direct", advisor=advisor, routines="",
                  person_id=person)
        s.add(a)
        s.flush()
        return a.id


# --- 1. ca_people and the migration ------------------------------------------

def test_the_people_table_holds_no_name_birth_date_or_clinical_field(
        svc):  # noqa: F811
    cols = {c["name"] for c in inspect(svc.engine).get_columns("ca_people")}
    assert cols == {"id", "account_id", "kind", "relationship", "nickname",
                    "attested_at", "attestation_version", "agent_at",
                    "created_at"}


def test_every_new_account_gets_one_self_person(cfg, svc,
                                                monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    assert _people(svc, aid) == [("self", None, None)]
    # Signing in again does not make a second one.
    c.post("/logout")
    _login(c, svc, monkeypatch, email="pat@example.com")
    assert _people(svc, aid) == [("self", None, None)]


def test_a_new_connection_and_its_assistant_point_at_the_self_person(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    with svc.session() as s:
        me = s.query(Person).filter_by(account_id=aid, kind="self").one().id
        assert s.get(Connection, conn["id"]).person_id == me
        [agent] = s.query(Agent).filter_by(account_id=aid).all()
        assert agent.person_id == me
        assert agent.routines == ""


def _old_database(kind, tmp_path) -> str:
    """The schema as shipped before people existed, with legacy rows: one
    account with two assistants (advisors), one with none, one with an
    assistant it later removed."""
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url, future=True)
    models.Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE ca_accounts (id VARCHAR(32) PRIMARY KEY, "
            "email VARCHAR(255) NOT NULL UNIQUE, email_verified_at FLOAT, "
            "created_at FLOAT, last_login_at FLOAT, first_agent_at FLOAT)"))
        conn.execute(text(
            "CREATE TABLE ca_connections (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), kind VARCHAR(16) NOT NULL, "
            "tenant_id VARCHAR(64) NOT NULL, label VARCHAR(120), "
            "status VARCHAR(16), connected_at FLOAT)"))
        conn.execute(text(
            "CREATE TABLE ca_agents (id VARCHAR(32) PRIMARY KEY, "
            "account_id VARCHAR(32), connection_id VARCHAR(32), "
            "name VARCHAR(48), advisor VARCHAR(32), persona VARCHAR(16), "
            "created_at FLOAT)"))
        for aid, first in (("acct_two", 5.0), ("acct_none", None),
                           ("acct_gone", 7.0)):
            conn.execute(text(
                "INSERT INTO ca_accounts (id, email, first_agent_at) "
                "VALUES (:i, :e, :f)"),
                {"i": aid, "e": f"{aid}@example.com", "f": first})
        conn.execute(text(
            "INSERT INTO ca_connections VALUES "
            "('conn_a', 'acct_two', 'sample', 't-a', 'Sample', 'active', 1.0),"
            "('conn_b', 'acct_two', 'fasten', 't-b', 'Real', 'active', 2.0),"
            "('conn_g', 'acct_gone', 'sample', 't-g', 'Sample', 'active', 1.0)"))
        conn.execute(text(
            "INSERT INTO ca_agents VALUES "
            "('agent_1', 'acct_two', 'conn_a', 'Juniper', NULL, 'calm', 1.0),"
            "('agent_2', 'acct_two', 'conn_b', 'Coach', 'diet-exercise', "
            "'direct', 2.0),"
            "('agent_3', 'acct_two', 'conn_b', 'Pill', 'medication-refills', "
            "'sunny', 3.0),"
            "('agent_4', 'acct_two', 'conn_b', 'Kid', 'kids-health', "
            "'calm', 4.0)"))
    engine.dispose()
    return url


@pytest.mark.parametrize("kind", URLS)
def test_boot_gives_every_account_a_self_person_and_points_rows_at_it(
        kind, tmp_path):
    url = _old_database(kind, tmp_path)
    engine = models.make_engine(url)
    with engine.connect() as conn:
        people = {r[0]: r[1:] for r in conn.execute(text(
            "SELECT account_id, kind, relationship, nickname, "
            "agent_at IS NOT NULL FROM ca_people"))}
        conns = dict(conn.execute(text(
            "SELECT c.id, p.kind FROM ca_connections c "
            "JOIN ca_people p ON p.id = c.person_id")).all())
        agents = {r[0]: r[1:] for r in conn.execute(text(
            "SELECT a.id, p.kind, a.routines, a.advisor FROM ca_agents a "
            "JOIN ca_people p ON p.id = a.person_id"))}
    # Exactly one self person each. Assistants or not, nothing is "other":
    # a second assistant on another connection is never marked as someone
    # the holder cares for.
    assert people == {"acct_two": ("self", None, None, True),
                      "acct_none": ("self", None, None, False),
                      "acct_gone": ("self", None, None, False)}
    assert conns == {"conn_a": "self", "conn_b": "self", "conn_g": "self"}
    # Advisors become routines (D6: diet-exercise joins Habits and vitals);
    # general, NULL and an unavailable advisor become none. The advisor
    # column stays readable for one release.
    assert agents == {"agent_1": ("self", "", None),
                      "agent_2": ("self", "habits", "diet-exercise"),
                      "agent_3": ("self", "refills", "medication-refills"),
                      "agent_4": ("self", "", "kids-health")}
    engine.dispose()

    # A second boot changes nothing and makes no second self person.
    again = models.make_engine(url)
    with again.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM ca_people")).scalar() == 3
    again.dispose()


@pytest.mark.parametrize("kind", URLS)
def test_the_schema_refuses_a_second_self_person(kind, tmp_path):
    from sqlalchemy.exc import IntegrityError
    url = PG if kind == "postgres" else f"sqlite:///{tmp_path}/new.db"
    if kind == "postgres":
        e = create_engine(url)
        models.Base.metadata.drop_all(e)
        e.dispose()
    engine = models.make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ca_accounts (id, email) "
                          "VALUES ('acct_x', 'x@example.com')"))
        conn.execute(text("INSERT INTO ca_people (id, account_id, kind) "
                          "VALUES ('person_1', 'acct_x', 'self')"))
        # Any number of others.
        conn.execute(text("INSERT INTO ca_people (id, account_id, kind) "
                          "VALUES ('person_2', 'acct_x', 'other')"))
        conn.execute(text("INSERT INTO ca_people (id, account_id, kind) "
                          "VALUES ('person_3', 'acct_x', 'other')"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO ca_people (id, account_id, kind) "
                              "VALUES ('person_4', 'acct_x', 'self')"))
    engine.dispose()


def test_deleting_the_account_deletes_its_people(cfg, svc,
                                                 monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    c.post("/api/connections/sample")
    c.post("/api/people", json={"relationship": "parent", "nickname": "Mom"})
    assert svc.delete_account(aid)
    with svc.session() as s:
        assert s.query(Person).filter_by(account_id=aid).count() == 0


# --- 2. One assistant per person ----------------------------------------------

def test_asking_for_another_assistant_returns_the_one_you_have(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    first = conn["agent_id"]
    r = c.post("/api/agents", json={"name": "Coach", "persona": "direct",
                                    "connection_id": conn["id"]})
    assert r.status_code == 200
    assert r.get_json() == {"id": first, "existing": True}
    assert [a.id for a in _agents(svc, aid)] == [first]


def test_the_service_never_makes_a_second_assistant_for_a_person(
        cfg, svc, monkeypatch):  # noqa: F811
    aid = _make_account(svc, monkeypatch, "svc@example.com").id
    conn = svc.add_connection(aid, "sample", "t-svc", "Sample")
    one = svc.create_agent(aid, "Juniper", "calm", conn)
    two = svc.create_agent(aid, "Coach", "direct", conn)
    assert one == two
    assert len(_agents(svc, aid)) == 1


def test_a_legacy_person_with_an_assistant_gets_no_new_one(
        cfg, svc, monkeypatch):  # noqa: F811
    """`agent_at` set by the migration is the only thing standing between a
    legacy account and a third assistant."""
    aid = _make_account(svc, monkeypatch, "legacy@example.com").id
    conn = svc.add_connection(aid, "sample", "t-leg", "Sample")
    first = svc.create_agent(aid, "Juniper", "calm", conn)
    _legacy_agent(svc, aid, conn)
    assert svc.create_agent(aid, "Third", "calm", conn) == first
    assert len(_agents(svc, aid)) == 2


def test_removing_the_assistant_lets_the_person_have_one_again(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    # Removed through the service: the route refuses a primary (SEC951-L1).
    assert svc.delete_agent(aid, conn["agent_id"])
    r = c.post("/api/agents", json={"name": "Juniper",
                                    "connection_id": conn["id"]})
    assert r.status_code == 200
    assert r.get_json().get("existing") is not True
    assert len(_agents(svc, aid)) == 1


def test_removing_one_legacy_assistant_keeps_the_rule(cfg, svc,
                                                      monkeypatch):  # noqa: F811
    aid = _make_account(svc, monkeypatch, "two@example.com").id
    conn = svc.add_connection(aid, "sample", "t-two", "Sample")
    first = svc.create_agent(aid, "Juniper", "calm", conn)
    extra = _legacy_agent(svc, aid, conn)
    assert svc.delete_agent(aid, extra)
    # The person still has an assistant, so still no second one.
    assert svc.create_agent(aid, "Again", "calm", conn) == first


def test_the_first_assistant_is_still_made_once(cfg, svc,
                                                monkeypatch):  # noqa: F811
    """ensure_first_agent keeps its account stamp: a removed first
    assistant is not brought back by the next ingest callback."""
    aid = _make_account(svc, monkeypatch, "once@example.com").id
    conn = svc.add_connection(aid, "sample", "t-once", "Sample")
    made = svc.ensure_first_agent(aid, conn)
    assert made
    assert svc.ensure_first_agent(aid, conn) is None
    svc.delete_agent(aid, made)
    assert svc.ensure_first_agent(aid, conn) is None
    assert _agents(svc, aid) == []


# --- Someone you care for: made-up records only (D1) --------------------------

def test_adding_someone_you_care_for_gives_them_made_up_records_and_an_assistant(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    r = c.post("/api/people", json={"relationship": "parent",
                                    "nickname": "  Mom  "})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert set(body) == {"person_id", "connection_id", "agent_id", "redirect"}
    assert _people(svc, aid) == [("self", None, None),
                                 ("other", "parent", "Mom")]
    with svc.session() as s:
        conn = s.get(Connection, body["connection_id"])
        agent = s.get(Agent, body["agent_id"])
        assert conn.kind == "sample"           # never real records (D1)
        assert conn.person_id == body["person_id"] == agent.person_id
        # Authority is never inferred: no attestation is stamped.
        p = s.get(Person, body["person_id"])
        assert p.attested_at is None and p.attestation_version is None


@pytest.mark.parametrize("body", [
    {},
    {"relationship": "boss"},
    {"relationship": "self"},
    {"relationship": "parent", "nickname": "x" * 25},
    {"relationship": "parent", "nickname": 7},
    {"relationship": "parent", "nickname": "Mom\nborn 1950"},
])
def test_a_person_is_a_relationship_and_a_short_nickname_only(
        cfg, svc, monkeypatch, body):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    r = c.post("/api/people", json=body)
    assert r.status_code == 400
    assert _people(svc, aid) == [("self", None, None)]


def test_no_real_records_path_exists_for_someone_you_care_for(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    r = c.post("/api/people", json={"relationship": "child",
                                    "connector": "fasten"})
    assert r.status_code == 200
    with svc.session() as s:
        kinds = {x.kind for x in s.query(Connection).filter_by(account_id=aid)}
    assert kinds == {"sample"}


def test_your_own_sample_is_not_someone_elses(cfg, svc,
                                              monkeypatch):  # noqa: F811
    """Add Mom first, then tap Explore with made-up records: the holder gets
    their own sample and their own assistant, not Mom's."""
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    mom = c.post("/api/people", json={"relationship": "parent"}).get_json()
    mine = c.post("/api/connections/sample").get_json()
    assert mine["id"] != mom["connection_id"]
    assert mine["agent_id"] != mom["agent_id"]
    with svc.session() as s:
        me = s.query(Person).filter_by(account_id=aid, kind="self").one().id
        assert s.get(Connection, mine["id"]).person_id == me
        assert s.get(Agent, mine["agent_id"]).person_id == me


def test_a_callback_never_makes_an_assistant_for_someone_else(
        cfg, svc, monkeypatch):
    """ensure_first_agent is for the holder's own records: an ingest
    callback on someone else's tenant makes nothing and spends nothing."""
    aid = _make_account(svc, monkeypatch, "cb@example.com").id
    mom = svc.add_person(aid, "parent", "Mom")
    mom_conn = svc.add_connection(aid, "sample", "t-cbm", "Sample",
                                  person_id=mom)
    assert svc.ensure_first_agent(aid, mom_conn) is None
    assert _agents(svc, aid) == []
    mine = svc.add_connection(aid, "sample", "t-cbs", "Sample")
    assert svc.ensure_first_agent(aid, mine)


def test_an_assistant_cannot_be_moved_to_another_persons_records(
        cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    mine = c.post("/api/connections/sample").get_json()
    mom = c.post("/api/people", json={"relationship": "parent"}).get_json()
    r = c.post(f"/api/agents/{mine['agent_id']}/connection",
               json={"connection_id": mom["connection_id"]})
    assert r.status_code == 404
    r = c.post(f"/api/agents/{mom['agent_id']}/connection",
               json={"connection_id": mine["id"]})
    assert r.status_code == 404


def test_someone_you_care_for_is_not_offered_to_a_third_party_app(
        cfg, svc, monkeypatch):  # noqa: F811
    """Spec 6.3: the grant chooser leaves out kind="other" connections."""
    app, c, aid = _signed_in(cfg, svc, monkeypatch)
    c.post("/api/connections/sample")
    mom = c.post("/api/people", json={"relationship": "parent"}).get_json()
    assert mom["connection_id"] in svc.other_person_connection_ids(aid)
    assert len(svc.other_person_connection_ids(aid)) == 1


def test_adding_someone_is_off_until_the_hub_renders_people(
        cfg, svc, monkeypatch):
    """#947 QA: until the hub shows whose assistant is whose, adding
    someone made two identical cards. CARE_PEOPLE, default off."""
    from careagents.app import create_app
    from careagents.config import Config
    assert Config(env={}).people_enabled is False
    assert Config(env={"CARE_PEOPLE": "on"}).people_enabled is True
    cfg.people_enabled = False
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch, email="flag@example.com")
    r = c.post("/api/people", json={"relationship": "parent"})
    assert r.status_code == 404
    with svc.session() as s:
        aid = s.query(models.Account).filter_by(email="flag@example.com").one().id
    assert _people(svc, aid) == [("self", None, None)]


def test_change_records_offers_only_the_same_persons_records(
        cfg, svc, monkeypatch):
    from careagents import hub
    aid = _make_account(svc, monkeypatch, "mv@example.com").id
    mine = svc.add_connection(aid, "sample", "t-mv1", "Sample records")
    mine2 = svc.add_connection(aid, "direct", "t-mv2", "Uploaded")
    mom = svc.add_person(aid, "parent", "Mom")
    mom_conn = svc.add_connection(aid, "sample", "t-mv3", "Sample records",
                                  person_id=mom)
    view = hub.build(svc.list_home(aid), 0.0)
    assert {c["id"] for c in view["move_choices"]} == {mine, mine2}
    by_person = view["move_choices_by_person"]
    me = next(p["id"] for p in svc.list_home(aid)["people"]
              if p["kind"] == "self")
    assert {c["id"] for c in by_person[me]} == {mine, mine2}
    assert [c["id"] for c in by_person[mom]] == [mom_conn]


def test_settings_labels_and_offers_remove_on_a_legacy_account(
        cfg, svc, monkeypatch):
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    conn = c.post("/api/connections/sample").get_json()
    extra = _legacy_agent(svc, aid, conn["id"], name="Coach")
    html = c.get("/settings").get_data(as_text=True)
    assert html.count("Reads sample records") == 2
    assert f'class="pill danger assistant-remove" data-agent="{extra}"' in html
    # The primary assistant is not offered for removal here.
    assert f'assistant-remove" data-agent="{conn["agent_id"]}"' not in html


def test_settings_offers_no_remove_to_a_single_assistant(
        cfg, svc, monkeypatch):
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    c.post("/api/connections/sample")
    html = c.get("/settings").get_data(as_text=True)
    assert "assistant-remove" not in html.split("<script")[0]


def test_settings_pairs_a_phone_to_your_own_assistant(
        cfg, svc, monkeypatch):
    """Mom added first: the iMessage tile still pairs the holder's own."""
    cfg.imessage_handle = "+15555550100"
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    mom = c.post("/api/people", json={"relationship": "parent"}).get_json()
    mine = c.post("/api/connections/sample").get_json()
    html = c.get("/settings").get_data(as_text=True)
    assert f'id="im-surface" data-agent="{mine["agent_id"]}"' in html
    assert f'id="im-surface" data-agent="{mom["agent_id"]}"' not in html


def test_a_paused_account_adds_nobody(cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    svc.set_paused("pat@example.com", True)
    r = c.post("/api/people", json={"relationship": "parent"})
    assert r.status_code == 423
    assert _people(svc, aid) == [("self", None, None)]


def test_people_have_a_ceiling(cfg, svc, monkeypatch):  # noqa: F811
    """Each one mints and seeds a tenant, so the count is bounded."""
    from careagents.accounts import MAX_OTHER_PEOPLE
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    for _ in range(MAX_OTHER_PEOPLE):
        assert c.post("/api/people",
                      json={"relationship": "other"}).status_code == 200
    r = c.post("/api/people", json={"relationship": "other"})
    assert r.status_code == 409
    assert len(_people(svc, aid)) == MAX_OTHER_PEOPLE + 1


def test_a_link_session_cannot_add_people(cfg, svc,
                                          monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    with c.session_transaction() as sess:
        sess["via_link"] = True
    r = c.post("/api/people", json={"relationship": "parent"})
    assert r.status_code == 403
    assert _people(svc, aid) == [("self", None, None)]


# --- 3. Routines and tone -------------------------------------------------------

def test_routines_are_prompt_blocks_after_the_safety_core():
    from careagents.advisors import ROUTINES
    from careagents.personas import SAFETY_CORE, system_prompt
    base = system_prompt("Juniper", "calm")
    both = system_prompt("Juniper", "calm", routines="habits,refills")
    assert set(ROUTINES) == {"habits", "care-gaps", "refills"}
    assert both.startswith(base)
    assert both.index(SAFETY_CORE) < both.index(ROUTINES["habits"]["guidance"])
    assert ROUTINES["refills"]["guidance"] in both
    assert ROUTINES["care-gaps"]["guidance"] not in both
    # Habits and vitals carries what diet-exercise said (D6).
    assert "cardiac condition" in ROUTINES["habits"]["guidance"]
    # Unknown or hidden routines add nothing.
    assert system_prompt("Juniper", "calm",
                         routines="research-monitor,kids-health,x") == base


def test_a_legacy_advisor_still_reads_as_its_routine():
    from careagents.advisors import ROUTINES, routines_for
    assert routines_for({"routines": None, "advisor": "diet-exercise"}) == [
        "habits"]
    assert routines_for({"routines": "", "advisor": "diet-exercise"}) == []
    assert routines_for({"routines": "refills,habits,refills,nope"}) == [
        "refills", "habits"]
    assert set(routines_for({"routines": ",".join(ROUTINES)})) == set(ROUTINES)


def test_a_routine_changes_no_tool(cfg, svc, monkeypatch):  # noqa: F811
    """The tool set is the same guarded six whatever the routines."""
    from careagents import agent
    assert "routine" not in repr(agent.TOOLS).lower()


def test_tone_and_routines_are_editable(cfg, svc, monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    agent_id = c.post("/api/connections/sample").get_json()["agent_id"]
    r = c.post(f"/api/agents/{agent_id}/settings",
               json={"persona": "direct", "routines": ["refills", "habits"]})
    assert r.status_code == 200
    assert r.get_json() == {"id": agent_id, "persona": "direct",
                            "routines": ["habits", "refills"]}
    [a] = _agents(svc, aid)
    assert (a.persona, a.routines) == ("direct", "habits,refills")
    # Turning them all off is allowed.
    r = c.post(f"/api/agents/{agent_id}/settings", json={"routines": []})
    assert r.get_json()["routines"] == []
    [a] = _agents(svc, aid)
    assert (a.persona, a.routines) == ("direct", "")


@pytest.mark.parametrize("body", [
    {"persona": "shouty"},
    {"routines": ["research-monitor"]},
    {"routines": ["kids-health"]},
    {"routines": "habits"},
    {"routines": ["habits", 3]},
    {},
])
def test_unknown_tone_or_routine_is_refused(cfg, svc, monkeypatch,
                                            body):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    agent_id = c.post("/api/connections/sample").get_json()["agent_id"]
    r = c.post(f"/api/agents/{agent_id}/settings", json=body)
    assert r.status_code == 400
    [a] = _agents(svc, aid)
    assert (a.persona, a.routines) == ("calm", "")


def test_someone_elses_assistant_settings_are_not_yours(
        cfg, svc, monkeypatch):  # noqa: F811
    app = _app(cfg, svc)
    a, b = app.test_client(), app.test_client()
    _login(a, svc, monkeypatch, email="a@example.com")
    _login(b, svc, monkeypatch, email="b@example.com")
    theirs = a.post("/api/connections/sample").get_json()["agent_id"]
    r = b.post(f"/api/agents/{theirs}/settings", json={"persona": "direct"})
    assert r.status_code == 404


def test_the_worker_prompt_carries_the_assistants_routines(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.advisors import ROUTINES
    from tests.test_careagents import _turn
    seen = {}

    class _Turn:
        text, tool_calls, raw_tool_calls = "ok", [], []

    def _complete(_cfg, system, *_a, **_k):
        seen["system"] = system
        return _Turn()
    monkeypatch.setattr("careagents.worker.llm.complete", _complete)
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    agent_id = c.post("/api/connections/sample").get_json()["agent_id"]
    c.post(f"/api/agents/{agent_id}/settings", json={"routines": ["care-gaps"]})
    _turn(c, agent_id, "what is due?")
    # The worker's account label ends with its turn, not with the thread.
    from careagents.healthclaw import agent_header
    assert agent_header() == "careagents"
    assert ROUTINES["care-gaps"]["guidance"] in seen["system"]
    assert ROUTINES["refills"]["guidance"] not in seen["system"]


def test_the_settings_page_shows_tone_and_focuses_on(cfg, svc,
                                                     monkeypatch):  # noqa: F811
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    agent_id = c.post("/api/connections/sample").get_json()["agent_id"]
    c.post(f"/api/agents/{agent_id}/settings", json={"routines": ["refills"]})
    html = c.get("/settings").get_data(as_text=True)
    assert "Focuses on" in html
    assert "Watching" not in html          # owner decision D3
    assert "Habits and vitals" in html and "Refills" in html
    # The hub card's tone words, so Settings and the hub match.
    assert ">Direct</span>" in html and "Straight Shooter" not in html
    assert f'data-agent="{agent_id}"' in html


# --- Hub data (rendered by the hub follow-up, not here) -------------------------

def test_the_hub_data_names_each_assistants_person_tone_and_routines(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents import hub
    aid = _make_account(svc, monkeypatch, "hub@example.com").id
    conn = svc.add_connection(aid, "sample", "t-hub", "Sample")
    first = svc.create_agent(aid, "Juniper", "calm", conn)
    svc.set_agent_settings(aid, first, "calm", ["refills"])
    extra = _legacy_agent(svc, aid, conn, name="Coach")
    mom = svc.add_person(aid, "parent", "Mom")
    mom_conn = svc.add_connection(aid, "sample", "t-mom", "Sample",
                                  person_id=mom)
    mom_agent = svc.create_agent(aid, "Juniper", "calm", mom_conn)

    view = hub.assistants(svc.list_home(aid))
    assert view["legacy_multi"] is True
    by_id = {a["id"]: a for a in view["assistants"]}
    assert by_id[first]["is_primary"] is True
    assert by_id[first]["person"] == {"id": by_id[first]["person"]["id"],
                                      "kind": "self", "relationship": None,
                                      "nickname": None}
    assert by_id[first]["tone"] == "Calm Guide"
    assert by_id[first]["routines"] == ["Refills"]
    assert by_id[first]["focuses_on"] == "Focuses on: refills"
    assert by_id[extra]["is_primary"] is False
    assert by_id[extra]["legacy_extra"] is True
    assert by_id[mom_agent]["person"]["kind"] == "other"
    assert by_id[mom_agent]["person"]["nickname"] == "Mom"
    assert by_id[mom_agent]["is_primary"] is True
    assert by_id[mom_agent]["legacy_extra"] is False
    assert [p["kind"] for p in view["people"]] == ["self", "other"]


def test_the_switch_question_never_offers_your_records_to_someone_else(
        cfg, svc, monkeypatch):
    from careagents import hub
    aid = _make_account(svc, monkeypatch, "sw@example.com").id
    mom = svc.add_person(aid, "parent", "Mom")
    mom_conn = svc.add_connection(aid, "sample", "t-swm", "Sample",
                                  person_id=mom)
    svc.create_agent(aid, "Juniper", "calm", mom_conn)
    svc.add_connection(aid, "fasten", "t-swr", "Records from your doctor")
    assert hub.switch_prompt(svc.list_home(aid)) is None
    mine = svc.add_connection(aid, "sample", "t-sws", "Sample")
    me = svc.create_agent(aid, "Juniper", "calm", mine)
    assert hub.switch_prompt(svc.list_home(aid))["agent_id"] == me


def test_one_assistant_is_not_legacy(cfg, svc, monkeypatch):  # noqa: F811
    from careagents import hub
    aid = _make_account(svc, monkeypatch, "solo@example.com").id
    conn = svc.add_connection(aid, "sample", "t-solo", "Sample")
    svc.create_agent(aid, "Juniper", "calm", conn)
    view = hub.assistants(svc.list_home(aid))
    assert view["legacy_multi"] is False
    assert [a["focuses_on"] for a in view["assistants"]] == [""]


# --- Operator count -------------------------------------------------------------

def test_the_operator_count_prints_counts_only(cfg, svc,
                                               monkeypatch):  # noqa: F811
    app = _app(cfg, svc)
    a1 = _make_account(svc, monkeypatch, "c1@example.com").id
    a2 = _make_account(svc, monkeypatch, "c2@example.com").id
    _make_account(svc, monkeypatch, "c3@example.com").id
    c1 = svc.add_connection(a1, "sample", "t-c1", "Sample")
    svc.create_agent(a1, "Juniper", "calm", c1)
    _legacy_agent(svc, a1, c1)
    _legacy_agent(svc, a1, c1, name="Third")
    c2 = svc.add_connection(a2, "sample", "t-c2", "Sample")
    svc.create_agent(a2, "Juniper", "calm", c2)
    out = app.test_cli_runner().invoke(args=["assistants", "count"])
    assert out.exit_code == 0, out.output
    assert out.output.splitlines() == [
        "accounts: 3",
        "accounts with no assistant: 1",
        "accounts with one assistant: 1",
        "accounts with more than one assistant: 1",
    ]
    assert "@" not in out.output and "acct_" not in out.output


# --- 5. The audit trail names the account that acted -----------------------

class _Capture:
    """A requests.Session stand-in that records each call's headers."""

    def __init__(self):
        self.calls = []

    def _answer(self, url, headers):
        import requests
        self.calls.append((url, dict(headers or {})))
        r = requests.Response()
        r.status_code = 200
        r._content = (b'{"token": "tok"}' if url.endswith("-token")
                      else b'{"resourceType": "Bundle", "entry": []}')
        return r

    def get(self, url, params=None, headers=None, timeout=None):
        return self._answer(url, headers)

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        return self._answer(url, headers)


def _client():
    from careagents.healthclaw import HealthClawClient
    hc = HealthClawClient("http://engine", "mint")
    hc.http = _Capture()
    return hc


def _agent_ids(hc, path):
    return [h.get("X-Agent-Id") for u, h in hc.http.calls if path in u]


def test_a_tenant_call_names_the_acting_account():
    from careagents.healthclaw import acting_for
    hc = _client()
    with acting_for("acct_0123456789abcdef"):
        hc.search("t-1", "Observation")
    hc.search("t-1", "Condition")
    assert _agent_ids(hc, "/Observation") == ["careagents:acct_0123456789abcdef"]
    # Outside a signed-in request or a worker's turn: the plain label.
    assert _agent_ids(hc, "/Condition") == ["careagents"]


@pytest.mark.parametrize("bad", ["", None, "acct_", "acct_x y", "Jane Roe",
                                 "acct_" + "a" * 65, "careagents:acct_1",
                                 "acct_1\nX-Evil: 1"])
def test_only_an_account_id_becomes_part_of_the_header(bad):
    from careagents.healthclaw import acting_for, agent_header
    with acting_for(bad):
        assert agent_header() == "careagents"


def test_the_worker_lifecycle_label_is_unchanged():
    from careagents.healthclaw import acting_for
    hc = _client()
    with acting_for("acct_0123456789abcdef"):
        assert hc._internal_headers()["X-Agent-Id"] == "careagents-worker"


def test_approve_and_decline_name_the_acting_account():
    from careagents.healthclaw import acting_for
    hc = _client()
    with acting_for("acct_0123456789abcdef"):
        hc.confirm_action("t-1", "act-1")
        hc.decline_action("t-1", "act-2")
    assert _agent_ids(hc, "/act-1/confirm") == [
        "careagents:acct_0123456789abcdef"]
    assert _agent_ids(hc, "/act-2/decline") == [
        "careagents:acct_0123456789abcdef"]


def test_the_label_does_not_outlive_its_request(cfg, svc,
                                                monkeypatch):  # noqa: F811
    from careagents.healthclaw import agent_header
    _, c, aid = _signed_in(cfg, svc, monkeypatch)
    c.post("/api/connections/sample")
    assert agent_header() == "careagents"


def _engine_agent_ids(chain):
    from r6.models import AuditEventRecord
    from tests.test_beta_acceptance_rows import TENANT
    with chain.engine_app.app_context():
        return {r.agent_id for r in AuditEventRecord.query.filter_by(
            tenant_id=TENANT)}


def test_the_engine_audit_records_which_account_acted(
        cfg, svc, monkeypatch):  # noqa: F811
    """Cross-layer: the real client on the real engine. The sample connect
    (a signed-in request) and a chat turn's tool read (the worker) both
    land in the engine's AuditEvent table under `careagents:<account id>`,
    which the engine stores as given (String(128))."""
    from careagents.llm import LLMTurn, ToolCall
    from careagents.worker import RunWorker
    from tests.test_beta_acceptance_rows import Chain
    from tests.test_careagents import _turn
    chain = Chain(cfg, svc, monkeypatch)
    with svc.session() as s:
        aid = s.query(models.Account).filter_by(
            email="acceptance@example.com").one().id
    actor = f"careagents:{aid}"
    assert len(actor) <= 128
    after_connect = _engine_agent_ids(chain)
    assert actor in after_connect

    seq = iter([LLMTurn(tool_calls=[ToolCall("1", "get_health_summary", {})]),
                LLMTurn(text="Here is the summary.")])
    monkeypatch.setattr("careagents.worker.llm.complete",
                        lambda *a, **k: next(seq))
    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    RunWorker(cfg, chain.hc, svc, "audit-worker").run_once()
    from r6.models import AuditEventRecord
    from tests.test_beta_acceptance_rows import TENANT

    def _by_actor():
        with chain.engine_app.app_context():
            return AuditEventRecord.query.filter_by(
                tenant_id=TENANT, agent_id=actor).count()
    before = _by_actor()
    r = _turn(c, chain.agent, "give me a summary")
    assert r.status_code == 200
    # The worker's tool read is labelled with the account that asked.
    assert _by_actor() > before
    # No other CareAgents label claims the reads: only this account's.
    labels = {a for a in _engine_agent_ids(chain)
              if a and a.startswith("careagents")}
    assert labels <= {actor, "careagents-worker"}, labels
