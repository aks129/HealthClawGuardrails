""""Has my cholesterol changed?" by text, against the real engine.

The sample connection seeds the engine (r6/seed.py through /internal/seed);
the timeline tool reads it through the real HealthClaw client and $interpret;
the chart link opens a chart with those readings. The model is the one part
not driven. CareAgents' unit tests fake the client, so this is the check that
the sample records actually carry a trend.
"""

from __future__ import annotations

import json

from careagents import imessage
from careagents.agent import _execute_tool
from tests.test_beta_acceptance_rows import BASE, TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)


def test_a_cholesterol_question_by_text_gets_a_trend_and_a_real_chart(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)

    events: list = []
    out = json.loads(_execute_tool(
        chain.hc, TENANT, "show_lab_timeline", {"topic": "cholesterol"},
        events, agent_id=chain.agent, surface="imessage"))
    by_name = {s["name"]: s for s in out["series"]}
    for name in ("Total cholesterol", "LDL cholesterol"):
        series = by_name[name]
        assert series["readings"] >= 4 and series["trend_plottable"], name
        assert series["direction"] == "lower", name
        assert series["first"]["value"] > series["latest"]["value"]
        assert series["latest"]["unit"] == "mg/dL"
    assert events == [{"type": "card", "kind": "lab-timeline",
                       "topic": "cholesterol"}]

    reply = imessage.run_reply(
        [{"type": "agent.text",
          "payload": {"text": "Your LDL has come down over the last year."}},
         {"type": "agent.card", "payload": events[0]}],
        cfg.origin, chain.agent)
    link = f"{cfg.origin}/chat?agent={chain.agent}&chart=cholesterol"
    assert link in reply

    # The link's page, and the chart it draws: the same readings.
    page = chain.s.get(f"{BASE}/chat",
                       params={"agent": chain.agent, "chart": "cholesterol"})
    assert page.status_code == 200
    chart = chain.s.get(f"{BASE}/api/labs/timeline",
                        params={"agent": chain.agent, "topic": "cholesterol"})
    assert chart.status_code == 200
    drawn = {s["name"]: len(s["readings"]) for s in chart.json()["series"]}
    assert drawn["Total cholesterol"] >= 4 and drawn["LDL cholesterol"] >= 4
