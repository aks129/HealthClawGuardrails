"""QA #944: gaps the PR's own tests leave open. Synthetic data only.

- A returning chat whose totals were not counted falls back to the stored
  count; a source counted at zero must not be offered
  "Make my visit brief" (mutation Q1 survived the PR's suite).
- Rows inside the made-up drawer keep Delete behind the menu too.
"""

from __future__ import annotations

import re

from tests.test_careagents import FakeClient, _login, consented
from tests.test_careagents import app as _app_fixture
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture
from tests.test_careagents_hub_outcomes import (INTAKE, _conn, _section,
                                                _signed_in, _starters)

cfg = _cfg_fixture
svc = _svc_fixture
app = _app_fixture


def test_a_returning_chat_on_a_source_counted_empty_offers_only_intake(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = _conn(svc, aid, "direct", "ca-q1", "Uploaded records", "active",
                 count=0)
    agent = svc.create_agent(aid, "Rowan", "calm", conn)
    monkeypatch.setattr(FakeClient, "search",
                        lambda self, t, rt, params=None: {"total": 0})
    monkeypatch.setattr(
        FakeClient, "recent_messages",
        lambda self, *a, **k: [{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "hello"}])
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    assert _starters(page) == [INTAKE]


def test_a_returning_chat_on_a_counted_source_offers_the_visit_taps(
        app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    conn = _conn(svc, aid, "fasten", "ca-q2", "Records from your doctor",
                 "active", count=40)
    agent = svc.create_agent(aid, "Rowan", "calm", conn)
    monkeypatch.setattr(FakeClient, "search",
                        lambda self, t, rt, params=None: {"total": 0})
    monkeypatch.setattr(
        FakeClient, "recent_messages",
        lambda self, *a, **k: [{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "hello"}])
    page = c.get(f"/chat?agent={agent}").get_data(as_text=True)
    # "What changed since my last visit?" was dropped after the patient
    # tester's walk on #944; the visit brief is the tap that stays.
    assert _starters(page) == [INTAKE, "Make my visit brief"]


def test_drawer_rows_keep_delete_behind_the_menu(app, svc, monkeypatch):
    c, aid = _signed_in(app, svc, monkeypatch)
    sample = _conn(svc, aid, "sample", "ca-q3", "Sample records", "active",
                   count=100)
    _conn(svc, aid, "fasten", "ca-q4", "Records from your doctor", "active",
          count=152)
    sec = _section(c.get("/home").get_data(as_text=True), "records-section")
    drawer = sec[sec.index('class="made-up-sources"'):]
    assert f'data-conn="{sample}"' in drawer
    # Every Delete in the drawer sits inside a row's More menu.
    menus = re.findall(r'<details class="card-menu source-menu">.*?</details>',
                       drawer, flags=re.S)
    assert sum(m.count('class="conn-delete"') for m in menus) == \
        drawer.count('class="conn-delete"') == 1


def test_tapping_upload_again_shows_where_to_upload(app, svc, monkeypatch):
    c = app.test_client()
    _login(c, svc, monkeypatch)
    first = c.post("/api/connections/direct", json=consented()).get_json()
    second = c.post("/api/connections/direct", json=consented()).get_json()
    sec = _section(c.get("/home").get_data(as_text=True), "records-section")
    # Either the tap reuses the empty upload already shown, or the row it
    # made is the one on screen. A row nobody can see is neither.
    assert second["id"] == first["id"] or f'data-conn="{second["id"]}"' in sec
