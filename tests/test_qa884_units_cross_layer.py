"""QA #884 round 4: a reading's unit, end to end on the real engine.

A number is shown only beside the unit the reading itself stated. Readings
in SI units, a wrong-case unit and an unknown unit are added on top of the
seeded sample; the visit brief, the chat `get_labs` result and the texted
timeline are each read through the real client on the real engine.

Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from careagents.agent import _execute_tool
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

_LOINC = "http://loinc.org"
_UCUM = "http://unitsofmeasure.org"


def _obs(oid, code, vq, when):
    return {"resourceType": "Observation", "id": oid, "status": "final",
            "subject": {"reference": "Patient/demo-patient-rivera"},
            "code": {"coding": [{"system": _LOINC, "code": code}]},
            "valueQuantity": vq, "effectiveDateTime": when}


READINGS = [
    # the three round-3 repros
    ("qa-creat-si", "2160-0", {"value": 88, "unit": "umol/L",
                               "code": "umol/L"}),
    ("qa-gluc-si", "2345-7", {"value": 5.4, "unit": "mmol/L"}),
    ("qa-a1c-ifcc", "4548-4", {"value": 43, "unit": "mmol/mol",
                               "system": _UCUM, "code": "mmol/mol"}),
    # a wrong-case unit and an unknown one
    ("qa-chol-case", "2093-3", {"value": 190, "unit": "MG/DL"}),
    ("qa-ldl-unknown", "13457-7", {"value": 100, "unit": "mg/dl/x",
                                   "code": "mg/dl/x"}),
]


def _world(cfg, svc, monkeypatch):  # noqa: F811
    from models import db
    from r6.models import R6Resource
    chain = Chain(cfg, svc, monkeypatch)
    when = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with chain.engine_app.app_context():
        for oid, code, vq in READINGS:
            db.session.add(R6Resource("Observation",
                                      json.dumps(_obs(oid, code, vq, when)),
                                      resource_id=oid, tenant_id=TENANT))
        db.session.commit()
    return chain


def _brief_values(chain):
    brief = chain.hc.fetch_appointment_brief(TENANT)
    out = {}
    for ext in brief.get("extension") or []:
        for sub in ext.get("extension") or []:
            if sub.get("url") != "field":
                continue
            field = json.loads(sub["valueString"])
            out.setdefault(field.get("sourceId"), field.get("value"))
    return out


def test_the_brief_shows_each_number_beside_its_own_unit(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = _world(cfg, svc, monkeypatch)
    v = _brief_values(chain)
    assert v["qa-creat-si"].startswith("88 umol/L"), v["qa-creat-si"]
    assert v["qa-gluc-si"].startswith("5.4 mmol/L"), v["qa-gluc-si"]
    assert v["qa-a1c-ifcc"].startswith("43 mmol/mol"), v["qa-a1c-ifcc"]
    for oid in ("qa-chol-case", "qa-ldl-unknown"):
        assert v[oid].startswith("Result not listed"), (oid, v[oid])
    text = json.dumps(v)
    for wrong in ("88 mg/dL", "5.4 mg/dL", "43 %", "190 mg/dL", "100 mg/dL",
                  "MG/DL", "mg/dl/x"):
        assert wrong not in text, wrong


def test_chat_get_labs_never_pairs_a_number_with_a_unit_it_was_not_in(
        cfg, svc, monkeypatch):  # noqa: F811
    # Today the engine's $interpret marks these readings indeterminate (their
    # unit is not the range's), so `latest` keeps the last interpretable
    # one. This pins the outcome on the wire, not _coded_unit's withholding
    # branch; tests/test_careagents_r875_round3_exploits.py pins that.
    chain = _world(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   agent_id=chain.agent, surface="web"))
    raw = json.dumps(out)
    for wrong in ('"value": 88, "unit": "mg/dL"', '"value": 5.4, "unit": "mg/dL"',
                  '"value": 43, "unit": "%"', "MG/DL", "mg/dl/x"):
        assert wrong not in raw, wrong
    latest = {x["analyte"]: x for x in
              out["consumer_summary"].get("latest") or []}
    for item in latest.values():
        if item.get("value") is not None:
            assert item.get("unit") not in (None,), item


def test_a_text_timeline_across_two_units_shows_no_numbers(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = _world(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "show_lab_timeline",
                                   {"topic": "a1c"}, [],
                                   agent_id=chain.agent, surface="imessage"))
    series = {s["name"]: s for s in out["series"]}
    a1c = series["Hemoglobin A1c"]
    # The sample's % readings, then 43 mmol/mol a day later: the series has
    # both, and the words carry no first, latest or direction.
    assert a1c["trend_plottable"] and a1c["readings"] >= 2, a1c
    assert "first" not in a1c and "direction" not in a1c, a1c


def test_an_unreachable_engine_is_cached_too(cfg, svc, monkeypatch):  # noqa: F811
    """QA round 4: the badge cache holds a failure for its 120 s as well;
    an outage is when re-asking on every public load hurts most."""
    import careagents.app as app_mod
    from careagents.app import create_app
    from careagents.healthclaw import HealthClawError
    from tests.test_careagents import FakeClient

    class Down(FakeClient):
        calls = 0

        def conformance_badge(self):
            Down.calls += 1
            raise HealthClawError("conformance failed (503)", 503)
    clock = [1000.0]
    monkeypatch.setattr(app_mod.time, "time", lambda: clock[0])
    a = create_app(config=cfg, client=Down(), accounts=svc)
    a.config["TESTING"] = True
    c = a.test_client()
    for _ in range(3):
        assert c.get("/safety").status_code == 200
        assert c.get("/api/trust").get_json()["badge"] == "unavailable"
    assert Down.calls == 1
    clock[0] += 121
    c.get("/safety")
    assert Down.calls == 2
