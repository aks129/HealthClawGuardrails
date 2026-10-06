"""#867: the visit brief page and the agent's brief tool show the creatinine
trend in its own section, and only when the engine sent one.

Its own file because tests/test_careagents.py's `app` fixture would shadow
the engine's in tests/test_brief_lab_trends.py. Synthetic data only.
"""

import json

from tests.test_beta_acceptance_rows import BASE, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

PROMPTLY = "Contact your clinician promptly."
RISE = "creatinine rose from 0.8 to 1.3 mg/dL in 6 days"

def _brief_with_trend():
    url = ("https://healthclaw.io/fhir/StructureDefinition/"
           "brief-section-lab-trends")
    field = {"label": "Creatinine",
             "value": f"Between Sep 1 and Sep 7, 2026, your {RISE}. "
                      f"A rise like this can mean the kidneys are under "
                      f"strain. {PROMPTLY}",
             "sourceType": "Observation", "sourceId": "obs-9"}
    return {"resourceType": "Basic", "extension": [
        {"url": url, "extension": [
            {"url": "field", "valueString": json.dumps(field)}]}]}


def _page(app, svc, monkeypatch, brief):  # noqa: F811
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = c.post("/api/connections/sample").get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant: brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_the_page_shows_the_trend_in_its_own_section(app, svc, monkeypatch):  # noqa: F811
    body = _page(app, svc, monkeypatch, _brief_with_trend())
    assert "Lab trends" in body
    assert RISE in body and PROMPTLY in body


def test_the_page_has_no_trend_section_without_a_trend(app, svc, monkeypatch):  # noqa: F811
    body = _page(app, svc, monkeypatch, {"resourceType": "Basic",
                                         "extension": []})
    assert "Lab trends" not in body


def test_the_brief_tool_asks_for_the_trend_as_written():
    from careagents.agent import _execute_tool

    class _HC:
        def fetch_appointment_brief(self, _t):
            return _brief_with_trend()
    out = json.loads(_execute_tool(_HC(), "t", "appointment_brief", {}, []))
    [line] = out["sections"]["lab-trends"]
    assert PROMPTLY in line["value"]
    assert "as written" in out["note"] and "promptly" in out["note"]


def test_the_brief_page_on_the_real_engine_shows_the_rise(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    page = chain.s.get(f"{BASE}/brief", params={"agent": chain.agent})
    assert page.status_code == 200
    assert RISE in page.text and PROMPTLY in page.text
