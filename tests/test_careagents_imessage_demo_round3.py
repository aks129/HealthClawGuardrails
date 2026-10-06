"""#875 QA round 2: the signed document URL, and labs read as of when.

A. `check_form_status` handed the model the engine's signed delivery link,
   and a model that echoed it texted `/r6/sdc/documents/...&sig=...`. On a
   text surface the tool now gives the review page instead, and run_reply
   strips any engine document URL from the model's words as a backstop.
B. `get_labs` gave the model one undated line per reading, so an old high
   read as current and the new creatinine rise was one line among many. It
   now gives the latest reading per analyte, with its date, and how many
   earlier readings there are, on every surface.
"""

from __future__ import annotations

import json

from careagents import imessage
from careagents.agent import _execute_tool
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_imessage_demo import _Call, _Turn
from tests.test_careagents_sendblue import (  # noqa: F401
    _app, _deliverer, _hook, _pair, _run, sb_cfg, sb_svc)

ORIGIN = "http://localhost"
SIGNED = ("https://engine.example/r6/sdc/documents/doc-1.pdf"
          "?t=1700000000&sig=0123abcd")


class _FormHC:
    def action_status(self, _tenant, action_id):
        return {"id": action_id, "status": "completed",
                "outcome_summary": json.dumps({"delivery_link": SIGNED})}


# --- A. the signed URL --------------------------------------------------------

def test_on_text_the_form_status_tool_gives_the_review_page():
    events: list = []
    out = _execute_tool(_FormHC(), "t", "check_form_status",
                        {"action_id": "act-1"}, events, agent_id="ag_1",
                        surface="imessage", origin=ORIGIN)
    assert json.loads(out) == {"status": "completed",
                               "form_link": f"{ORIGIN}/review/ag_1/act-1"}
    assert "sig=" not in out and "/r6/sdc/documents/" not in out


def test_on_the_web_the_form_status_tool_is_unchanged():
    out = json.loads(_execute_tool(_FormHC(), "t", "check_form_status",
                                   {"action_id": "act-1"}, []))
    assert out == {"status": "completed", "delivery_link": SIGNED}


def test_engine_document_urls_are_stripped_from_a_texted_answer():
    text = (f"Download it here: {SIGNED} and keep it.\n\n"
            "Or this one https://elsewhere.example/f?x=1&sig=zz.\n\n"
            f"Your chat: {ORIGIN}/chat?agent=ag_1")
    reply = imessage.run_reply(
        [{"type": "agent.text", "payload": {"text": text}}], ORIGIN, "ag_1")
    assert "sig=" not in reply and "/r6/sdc/documents/" not in reply
    assert f"{ORIGIN}/chat?agent=ag_1" in reply
    assert "Download it here:" in reply and "keep it." in reply


def test_a_model_that_echoes_the_tool_never_texts_a_signed_url(
        sb_cfg, sb_svc, monkeypatch):  # noqa: F811
    app, c, fake, hc, agent_id = _app(sb_cfg, sb_svc, monkeypatch)
    hc.action_status = lambda t, a: _FormHC().action_status(t, a)
    _pair(c, agent_id)

    def complete(_cfg, system, history, tools):
        results = [m["content"] for m in history if m.get("role") == "tool"]
        if not results:
            return _Turn("", [_Call(0, "check_form_status",
                                    {"action_id": "act-1"})])
        # The worst case: the tool result, word for word, and the link
        # the web surface would have been given.
        return _Turn(f"Here is what I found: {results[-1]} {SIGNED}")
    monkeypatch.setattr("careagents.worker.llm.complete", complete)

    fake.sent.clear()
    _hook(c, "is my form ready?", handle="sig-1")
    _run(app)
    _deliverer(app, fake).once()
    assert fake.sent, "the answer was not delivered"
    for _, text in fake.sent:
        assert "sig=" not in text and "/r6/sdc/documents/" not in text
    assert any(f"/review/{agent_id}/act-1" in t for _, t in fake.sent)


# --- B. labs as of when, on the real engine ----------------------------------

def test_get_labs_gives_the_latest_reading_per_analyte(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    for surface in ("imessage", "web", ""):
        out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                       surface=surface))
        latest = {x["analyte"]: x for x in out["consumer_summary"]["latest"]}
        assert "lines" not in out["consumer_summary"]

        assert latest["Total cholesterol"]["flag"] == "N"
        assert latest["LDL cholesterol"]["flag"] == "N"
        assert latest["Total cholesterol"]["earlier_readings"] == 3
        assert latest["Hemoglobin A1c"]["flag"] == "H"
        assert latest["Hemoglobin A1c"]["earlier_readings"] == 3

        creat = latest["Creatinine"]
        assert creat["flag"] == "H"
        assert (creat["value"], creat["unit"]) == (1.3, "mg/dL")
        assert creat["earlier_readings"] == 4
        # The creatinine rise is the newest reading on file.
        assert creat["date"] == max(x["date"] for x in latest.values())
        assert creat["message"].startswith("Your creatinine is above")
        assert "earlier" in out["note"].lower() or "latest" in out["note"]


def test_labs_without_matching_observations_fall_back_to_the_lines():
    class _HC:
        def interpret_labs(self, _t):
            return {"summary": {}, "disclaimer": "d", "consumer": {
                "lines": [{"analyte": "Potassium", "flag": "N",
                           "message": "ok"}]}}
    out = json.loads(_execute_tool(_HC(), "t", "get_labs", {}, []))
    assert out["consumer_summary"]["lines"][0]["analyte"] == "Potassium"
