"""Page-view counting counts public pages, and nothing else.

The table exists to answer "is anybody coming". The risk it carries is that
a counter is an easy place to start storing a little more than a count, on a
surface whose signed-in pages are a person's own health records. These tests
pin the narrow shape: a flag that is off until set, only the pages anyone can
open, an endpoint name rather than a URL, and a row that holds no column
about a visitor.
"""
import datetime as _dt

import pytest

from careagents import analytics
from careagents.accounts import AccountService
from careagents.app import create_app
from careagents.config import Config
from careagents.models import Base, PageViewDay, make_engine

from tests.test_careagents import FakeClient


def _config(env_extra=None):
    import os
    url = os.environ.get("CARE_TEST_DATABASE_URL", "sqlite:///:memory:")
    if not url.startswith("sqlite"):
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    env = {"CARE_DATABASE_URL": url,
           "CARE_RP_ID": "localhost",
           "CARE_ORIGIN": "http://localhost",
           "HEALTHCLAW_MINT_SECRET": "mint-secret"}
    env.update(env_extra or {})
    return Config(env=env)


@pytest.fixture
def counted():
    """An app with counting switched on, plus its account service."""
    cfg = _config({"CARE_ANALYTICS": "1"})
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True
    return app, svc


def _rows(svc):
    with svc.session() as session:
        return [(r.day, r.endpoint, r.views)
                for r in session.query(PageViewDay).all()]


def test_nothing_is_counted_until_the_flag_is_set():
    """MUTATION: default CARE_ANALYTICS to on -> red."""
    cfg = _config()
    assert cfg.analytics_enabled is False
    svc = AccountService(cfg)
    app = create_app(config=cfg, client=FakeClient(), accounts=svc)
    app.config["TESTING"] = True

    assert app.test_client().get("/").status_code == 200

    assert _rows(svc) == []


def test_a_view_of_the_landing_page_is_counted_once_per_day(counted):
    app, svc = counted
    client = app.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 200

    rows = _rows(svc)
    assert len(rows) == 1, rows
    day, endpoint, views = rows[0]
    assert endpoint == "landing"
    assert views == 2
    assert day == analytics.utc_day()


def test_a_page_behind_sign_in_is_never_counted(counted):
    """The one that matters. /home redirects an anonymous caller to /auth,
    so this also pins that a redirect is not a view."""
    app, svc = counted
    client = app.test_client()

    home = client.get("/home")

    assert home.status_code in (302, 303), home.status_code
    assert [r for r in _rows(svc) if r[1] == "home"] == []


def test_the_signed_in_pages_are_absent_from_the_public_list():
    """A structural pin, so adding a page to PUBLIC_PAGES is a decision
    somebody makes rather than a line that drifts in with a refactor.

    MUTATION: add "home" to PUBLIC_PAGES -> red.
    """
    assert analytics.PUBLIC_PAGES == frozenset({"landing", "auth"})
    for private in ("home", "chat", "brief"):
        assert private not in analytics.PUBLIC_PAGES


def test_record_view_refuses_an_endpoint_outside_the_public_list(counted):
    """The hook is not the only guard: the function refuses too, so a caller
    wired to the wrong hook counts nothing rather than counting everything."""
    _app, svc = counted

    assert analytics.record_view(svc.session, "home") is False
    assert analytics.record_view(svc.session, "api_chat") is False
    assert _rows(svc) == []


def test_a_page_that_did_not_render_is_not_a_view(counted):
    app, svc = counted

    missing = app.test_client().get("/no-such-page")

    assert missing.status_code == 404
    assert _rows(svc) == []


def test_the_row_holds_no_column_about_the_visitor():
    """What is not stored is the whole design, so it is asserted rather than
    described: day, page, count, and an id — nothing else.

    MUTATION: add an `ip`, `user_agent` or `account_id` column -> red.
    """
    columns = {c.name for c in PageViewDay.__table__.columns}

    assert columns == {"id", "day", "endpoint", "views"}
    for forbidden in ("ip", "ip_address", "user_agent", "referrer",
                      "account_id", "session_id", "path", "url", "query"):
        assert forbidden not in columns


def test_two_days_of_views_are_separate_rows(counted):
    _app, svc = counted
    monday = _dt.datetime(2026, 9, 7, 12, 0, tzinfo=_dt.timezone.utc)
    tuesday = monday + _dt.timedelta(days=1)

    analytics.record_view(svc.session, "landing", now=monday)
    analytics.record_view(svc.session, "landing", now=monday)
    analytics.record_view(svc.session, "landing", now=tuesday)

    assert sorted(_rows(svc)) == [("2026-09-07", "landing", 2),
                                  ("2026-09-08", "landing", 1)]


def test_counting_never_breaks_the_page(counted, monkeypatch):
    """A page a person is reading must not fail because a counter did.

    MUTATION: drop the try/except around record_view in app.py -> red (500).
    """
    app, _svc = counted

    def _boom(*_a, **_k):
        raise RuntimeError("the counter is having a day")

    monkeypatch.setattr(analytics, "record_view", _boom)

    assert app.test_client().get("/").status_code == 200


def test_counts_reads_back_what_was_recorded(counted):
    _app, svc = counted
    now = _dt.datetime(2026, 9, 7, 12, 0, tzinfo=_dt.timezone.utc)

    analytics.record_view(svc.session, "landing", now=now)
    analytics.record_view(svc.session, "auth", now=now)

    assert sorted(analytics.counts(svc.session, days=7, now=now)) == [
        ("2026-09-07", "auth", 1), ("2026-09-07", "landing", 1)]


def test_counts_leaves_out_days_older_than_the_window(counted):
    _app, svc = counted
    now = _dt.datetime(2026, 9, 7, 12, 0, tzinfo=_dt.timezone.utc)
    long_ago = now - _dt.timedelta(days=30)

    analytics.record_view(svc.session, "landing", now=long_ago)
    analytics.record_view(svc.session, "landing", now=now)

    assert analytics.counts(svc.session, days=7, now=now) == [
        ("2026-09-07", "landing", 1)]
