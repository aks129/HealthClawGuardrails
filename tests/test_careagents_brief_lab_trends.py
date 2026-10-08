"""#867: the visit brief page and the agent's brief tool show the creatinine
trend in its own section, and only when the engine sent one.

Its own file because tests/test_careagents.py's `app` fixture would shadow
the engine's in tests/test_brief_lab_trends.py. Synthetic data only.
"""

import json

from tests.careagents_consent_helpers import consented
from tests.test_beta_acceptance_rows import BASE, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, app, cfg, svc)

PROMPTLY = "Contact your doctor promptly."
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
    # A real connection: this pins a real brief's wording; the sample's is
    # pinned in tests/test_careagents_sample_framing_rewalk.py (#908).
    conn_id = c.post("/api/connections/direct",
                     json=consented()).get_json()["id"]
    agent_id = c.post("/api/agents", json={
        "name": "Ada", "persona": "direct",
        "connection_id": conn_id}).get_json()["id"]
    monkeypatch.setattr(FakeClient, "fetch_appointment_brief",
                        lambda self, tenant, **_: brief)
    resp = c.get(f"/brief?agent={agent_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


HEADING = "Something to raise with your doctor"


def _alert(body):
    """The trend section's own markup, start tag to its closing </section>."""
    start = body.index('class="brief-alert"')
    return body[start:body.index("</section>", start)]


def test_the_page_leads_with_the_trend(app, svc, monkeypatch):  # noqa: F811
    """#878 patient tester V4: the one thing to act on comes first, under
    the intro and above every list, as an alert at full width."""
    body = _page(app, svc, monkeypatch, _brief_with_trend())
    alert = _alert(body)
    assert HEADING in alert
    assert RISE in alert and PROMPTLY in alert
    intro = body.index("A snapshot of your records")
    assert intro < body.index(HEADING) < body.index("Current conditions")


def test_the_trend_names_no_resource_type_or_id(app, svc, monkeypatch):  # noqa: F811
    """#878 patient tester G7: "from Observation obs-9" means nothing to a
    patient. The section says where it came from in words."""
    alert = _alert(_page(app, svc, monkeypatch, _brief_with_trend()))
    assert "From your lab results" in alert
    assert "Observation" not in alert and "obs-9" not in alert
    assert "brief-source-id" not in alert


def test_the_alert_spans_the_card_width():
    import pathlib
    css = (pathlib.Path(__file__).resolve().parents[1] / "careagents"
           / "static" / "careagents.css").read_text()
    rule = css[css.index(".brief-alert {"):]
    rule = rule[:rule.index("}")]
    assert "width: 100%" in rule or "grid-column: 1 / -1" in rule


def test_the_page_has_no_trend_section_without_a_trend(app, svc, monkeypatch):  # noqa: F811
    body = _page(app, svc, monkeypatch, {"resourceType": "Basic",
                                         "extension": []})
    assert HEADING not in body and "brief-alert" not in body


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
    # The chain is the sample connection, so the engine words the rise
    # about the sample person (#908).
    assert RISE in page.text
    assert "The sample person should contact their doctor promptly." in (
        page.text)
    assert "your creatinine" not in page.text
