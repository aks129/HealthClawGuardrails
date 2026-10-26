"""QA for #1036: a count unit the engine now scores must reach the chat
with its number, not as a bare flag.

#1036 taught `r6/labs/interpret.py` that thou/uL, 10*3/µL, 10E3/uL and
x10^9/L are the same quantity as 10*3/uL, so a platelet count in one of
them is scored. CareAgents names the unit only from its own allowlist
(`careagents/labs_timeline.UCUM_ALLOWED`), and an unlisted unit drops the
number (#884 QA F1). So the chat is handed "Platelets: H" with value None:
a flag without the number it is about.

Driven through the real client onto the real engine, not a fake: the
engine's `$interpret` decides what is scored. Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from careagents.agent import _execute_tool
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)

#: Spellings #1036 scores (tests/test_labs_unit_equivalence.py EQUIVALENT)
#: that UCUM_ALLOWED does not list.
UNLISTED = ["thou/uL", "10*3/µL", "10*3/μL", "10E3/uL", "x10^9/L"]


def _platelets(rid, unit, value=520, when="2099-01-02"):
    return {"resourceType": "Observation", "id": rid, "status": "final",
            "subject": {"reference": "Patient/demo-patient-rivera"},
            "effectiveDateTime": when,
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "777-3"}]},
            "valueQuantity": {"value": value, "unit": unit}}


def _latest_platelets(chain, unit):
    from models import db
    from r6.models import R6Resource
    res = _platelets(f"qa1036-plt-{abs(hash(unit))}", unit)
    with chain.engine_app.app_context():
        db.session.add(R6Resource("Observation", json.dumps(res),
                                  resource_id=res["id"], tenant_id=TENANT))
        db.session.commit()
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, []))
    latest = {x["analyte"]: x for x in
              (out["consumer_summary"].get("latest") or [])}
    return latest.get("Platelets"), out


def test_listed_spelling_reaches_the_chat_with_its_number(
        cfg, svc, monkeypatch):  # noqa: F811
    # Control: a spelling on both lists is scored AND keeps its number.
    chain = Chain(cfg, svc, monkeypatch)
    plt, out = _latest_platelets(chain, "K/uL")
    assert plt is not None, out
    assert plt["flag"] == "H" and plt["value"] == 520 and plt["unit"] == "K/uL"


@pytest.mark.parametrize("unit", UNLISTED)
def test_scored_spelling_reaches_the_chat_with_its_number(
        cfg, svc, monkeypatch, unit):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    plt, out = _latest_platelets(chain, unit)
    # The engine scored it (this is #1036's change)...
    assert plt is not None and plt["flag"] == "H", out
    # ...so the person must be told the number the flag is about.
    assert plt["value"] == 520, plt
    assert plt["unit"], plt
