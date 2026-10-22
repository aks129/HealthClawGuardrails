"""get_labs on a malformed BP panel (security review of #1021).

The plain-value path in careagents/agent.py reads a panel's components to
give the model "138/88". A feed can put any JSON there: a scalar
`component`, a string `code`, a scalar `coding`. Each one raised, failing
every "what do my labs say?" turn for that person. A malformed reading now
gets no number, and the rest of their labs still answer.
"""
from __future__ import annotations

import json

import pytest

from careagents.agent import _execute_tool, _plain_value
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_sec1021_bp_component_exploits import _ingest, _panel, _part

_PART = {"valueQuantity": {"value": 140, "unit": "mmHg"}}

MALFORMED = {
    "component-int": 5,
    "component-true": True,
    "component-code-str": [{"code": "8480-6", **_PART}],
    "component-coding-int": [{"code": {"coding": 5}, **_PART}],
}


@pytest.mark.parametrize("shape", list(MALFORMED))
def test_a_malformed_panel_gets_no_number(shape):
    """MUTATION: drop the guards -> raises."""
    assert _plain_value(_panel("x", MALFORMED[shape])) == (None, "")


def test_a_well_formed_panel_still_reads_as_systolic_over_diastolic():
    panel = _panel("x", [_part("8480-6", 141), _part("8462-4", 90)])
    assert _plain_value(panel) == ("141/90", "mmHg")


@pytest.mark.parametrize("shape", list(MALFORMED))
def test_get_labs_answers_beside_a_malformed_panel(cfg, svc, monkeypatch,  # noqa: F811
                                                    shape):
    """Through the real ingest and $interpret: the turn's tool still
    returns, with the sample's own readings in it."""
    chain = Chain(cfg, svc, monkeypatch)
    _ingest(chain, _panel("bad-labs-" + shape, MALFORMED[shape]))
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   surface="web"))
    got = {x["analyte"]: x for x in out["not_evaluated"]}
    assert got["Blood pressure"]["value"] == "138/88"
