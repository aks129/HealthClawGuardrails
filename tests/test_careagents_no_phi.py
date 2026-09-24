"""CareAgents stores no PHI: record CONTENT, driven through every surface.

`test_no_chat_turn_text_is_ever_written_to_a_careagents_table` covers the
patient's message and a fixed model reply. Its model never calls a tool and
`FakeClient` returns placeholder records, so no record content ever passes
through CareAgents there, and it never visits the brief, the labs timeline,
the approvals list or the review relay — the surfaces that do handle it.

Here every read the fake answers carries a planted canary in the field a real
feed would put it in (a coded `display`, `CodeableConcept.text`, a lab unit, a
brief field, a review page), the model calls every read tool and repeats the
canary back, and the person visits every page that renders record content.
Then every text column of every CareAgents table is scanned for it (#638).
"""

from __future__ import annotations

import json

from careagents.app import _BRIEF_SECTION_PREFIX
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    FakeClient, _login, _make_direct_conn, _turn, cfg, svc)
from tests.test_careagents_census_gaps import _all_text_in_careagents_db

CANARY = "CANARY-PHI-4417"


def _concept(label):
    return {"coding": [{"system": "http://loinc.org", "code": "2093-3",
                        "display": label}], "text": label}


class _CanaryClient(FakeClient):
    """Every clinical read answers with the canary where a real feed would."""

    def search(self, tenant, resource_type, params=None):
        return {"total": 1, "entry": [{"resource": {
            "resourceType": resource_type, "status": "active",
            "code": _concept(f"{CANARY} {resource_type}")}}]}

    def read(self, tenant, resource_type, resource_id):
        return {"resourceType": resource_type, "id": resource_id,
                "code": _concept(f"{CANARY} {resource_id}")}

    def interpret_labs(self, tenant):
        obs = [{"resource": {
            "resourceType": "Observation", "code": _concept(CANARY),
            "effectiveDateTime": day,
            "valueQuantity": {"value": value, "unit": f"mg/dL {CANARY}"}}}
            for day, value in (("2026-01-02", 212), ("2026-06-02", 188))]
        return {"summary": {}, "consumer": {"headline": CANARY},
                "disclaimer": CANARY,
                "bundle": {"resourceType": "Bundle", "entry": obs}}

    def care_gaps(self, tenant):
        return {"summary": {}, "consumer": {"due": [CANARY],
                                            "lines": [CANARY]}}

    def fetch_appointment_brief(self, tenant):
        field = json.dumps({"label": "Medication", "value": CANARY,
                            "sourceType": "MedicationRequest",
                            "sourceId": "m-1"})
        return {"resourceType": "Basic", "extension": [{
            "url": _BRIEF_SECTION_PREFIX + "medications",
            "extension": [{"url": "field", "valueString": field}]}]}

    def pending_actions(self, tenant):
        return [{"id": "act-1", "kind": "form-fill", "to": CANARY,
                 "status": "awaiting_confirmation"}]

    def action_status(self, tenant, action_id):
        status = super().action_status(tenant, action_id)
        status["outcome_summary"] = json.dumps(
            {"delivery_link": f"https://x/{CANARY}.pdf"})
        return status

    def fetch_review_page(self, tenant, action_id):
        return 200, (f"<html><p>{CANARY}</p>"
                     f"<form action='/r6/actions/{action_id}/review'>"
                     "</form></html>")

    def submit_review(self, tenant, action_id, decisions):
        return 200, {"status": "awaiting_confirmation", "summary": CANARY}


_READ_TOOLS = ("get_health_summary", "get_labs", "show_lab_timeline",
               "get_care_gaps", "search_records", "check_form_status")


def test_no_record_content_reaches_a_careagents_table_on_any_surface(
        cfg, svc, monkeypatch):  # noqa: F811
    """MUTATIONS, each applied alone (careagents/app.py): right after the
    engine call in `approvals`, set the tenant's `Connection.label` to
    `pending[0]["to"]`; or right after the one in `labs_timeline`, set its
    `Connection.provider` to `str(labs["consumer"])`. Either way
    tests/test_careagents.py, tests/test_careagents_census_gaps.py and
    tests/test_pending_approvals.py stay green (294 passed); this goes red.
    """
    from careagents import agent as agent_mod
    from careagents.app import create_app

    class _Call:
        def __init__(self, i, name):
            self.id, self.name = f"c{i}", name
            self.arguments = ({"resource_type": "Condition"}
                              if name == "search_records" else
                              {"action_id": "act-1"}
                              if name == "check_form_status" else
                              {"topic": "cholesterol"}
                              if name == "show_lab_timeline" else {})

    class _Turn:
        def __init__(self, text, calls):
            self.text, self.tool_calls, self.raw_tool_calls = text, calls, []

    tool_results: dict[str, str] = {}

    def _model(_cfg, _system, history, tools):
        # Call every read tool once, then answer by repeating the record back.
        results = {m["tool_call_id"]: m["content"] for m in history
                   if m.get("role") == "tool"}
        if tools and not results:
            return _Turn("", [_Call(i, n) for i, n in enumerate(_READ_TOOLS)])
        tool_results.update(results)
        return _Turn(f"Your record lists {CANARY}.", [])

    monkeypatch.setattr(agent_mod.llm, "complete", _model)
    fake = _CanaryClient()
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)

    # Connect: the sample source, and a direct upload carrying the canary.
    conn = c.post("/api/connections/sample").get_json()["id"]
    direct = _make_direct_conn(c)
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p-1",
                      "name": [{"family": CANARY}]}}]}
    assert c.post(f"/api/connections/{direct}/upload",
                  data=json.dumps(bundle),
                  headers={"Content-Type": "application/fhir+json"}
                  ).status_code == 200
    agent_id = c.post("/api/agents", json={
        "name": "Juniper", "persona": "calm",
        "connection_id": conn}).get_json()["id"]
    assert c.get(f"/chat?agent={agent_id}").status_code == 200

    # A chat turn whose tools read canary-bearing records.
    r = _turn(c, agent_id, f"what does {CANARY} mean?")
    stream = r.get_data(as_text=True)
    assert r.status_code == 200, stream
    assert CANARY in stream, "the model's answer never reached the person"
    # The tools really ran and really handed record content to the worker.
    # show_lab_timeline is exempt by design: it gives the model shape only.
    assert len(tool_results) == len(_READ_TOOLS), tool_results
    for i, name in enumerate(_READ_TOOLS):
        if name != "show_lab_timeline":
            assert CANARY in tool_results[f"c{i}"], (name, tool_results)

    # Every page that renders record content. Each must actually render, and
    # carry the canary to the person, or the scan below proves nothing.
    pages = {
        "brief": c.get(f"/brief?agent={agent_id}"),
        "labs": c.get(f"/api/labs/timeline?agent={agent_id}"
                      "&topic=cholesterol"),
        "approvals": c.get(f"/agents/{agent_id}/approvals"),
        "review": c.get(f"/review/{agent_id}/act-1"),
        "submit": c.post(f"/review/{agent_id}/act-1/submit",
                         json={"nka": "true"}),
        "form": c.get(f"/api/form/act-1?agent={agent_id}"),
    }
    for name, resp in pages.items():
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200, (name, resp.status_code, body[:300])
        assert CANARY in body, f"{name} did not carry the record to the person"
    assert c.get("/home").status_code == 200

    leaks = [(table, column, value)
             for table, column, value in _all_text_in_careagents_db(svc)
             if CANARY in value]
    assert not leaks, (
        "health-record content reached CareAgents' own database; it belongs "
        f"only in the HealthClaw tenant: {leaks}")
