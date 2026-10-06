"""#867: the KDIGO creatinine trend reaches chat, texting and the brief.

The trend runs only on `Observation/$interpret?subject=` (#865): a Bundle
or the tenant fallback can hold several people, and a "rise" across two of
them is arithmetic on the wrong person. CareAgents posted no subject, so the
trend never reached anyone.

CareAgents stores no patient id (ca_connections holds a tenant and nothing
inside it). The id lives in the tenant: `demo-patient-rivera` for the sample
records (r6/seed.py), the upstream Patient's own id for Fasten. So the client
asks the tenant, and passes a subject only when there is exactly one Patient
whose id has the FHIR id shape. Everything else posts as before.

The cross-layer tests drive the real client onto the real engine with the
seeded sample, whose creatinine goes 0.8 -> 1.3 mg/dL over six days.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from careagents import imessage
from careagents.agent import _execute_tool
from careagents.healthclaw import HealthClawClient
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, _turn, cfg, svc)
from tests.test_careagents_imessage_demo import _Call, _Turn

PROMPTLY = "Contact your clinician promptly."


# --- 1. the subject, at the client --------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok = status, body, status < 400
        self.text = json.dumps(body)
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._body


class _Wire:
    """Records every request; answers a Patient search with `patients`."""

    def __init__(self, patients):
        self.patients, self.calls = patients, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params or {}))
        if url.endswith("/Patient"):
            return _Resp(200, {"resourceType": "Bundle", "entry": [
                {"resource": p} for p in self.patients]})
        return _Resp(404, {})

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/approval-token") or "/token" in url:
            return _Resp(200, {"token": "t"})
        return _Resp(200, {"resourceType": "Parameters", "parameter": []})


def _client(patients):
    hc = HealthClawClient(base="http://engine", mint_secret="m")
    hc.http = _Wire(patients)
    hc.mint_token = lambda tenant: "tok"
    return hc


def _interpret_subject(hc):
    [url] = [u for m, u, _ in hc.http.calls
             if m == "POST" and "/Observation/$interpret" in u]
    return parse_qs(urlsplit(url).query).get("subject")


def test_one_patient_is_passed_as_the_subject():
    hc = _client([{"resourceType": "Patient", "id": "demo-patient-rivera"}])
    hc.interpret_labs("t1")
    assert _interpret_subject(hc) == ["Patient/demo-patient-rivera"]
    # The search asks for two: enough to know whether the match is unique.
    [(_, _, params)] = [c for c in hc.http.calls if c[0] == "GET"]
    assert params.get("_count") == "2"


@pytest.mark.parametrize("patients", [
    pytest.param([], id="no-patient"),
    pytest.param([{"resourceType": "Patient", "id": "a"},
                  {"resourceType": "Patient", "id": "b"}], id="two-patients"),
    pytest.param([{"resourceType": "Patient", "id": "a/b"}], id="slash"),
    pytest.param([{"resourceType": "Patient", "id": "a&subject=x"}],
                 id="query-injection"),
    pytest.param([{"resourceType": "Patient", "id": "x" * 65}], id="too-long"),
    pytest.param([{"resourceType": "Patient", "id": ""}], id="empty"),
    pytest.param([{"resourceType": "Patient", "id": 7}], id="not-a-string"),
    pytest.param([{"resourceType": "Patient"}], id="no-id"),
    pytest.param([{"resourceType": "Observation", "id": "o1"}],
                 id="not-a-patient"),
])
def test_no_subject_unless_exactly_one_valid_patient(patients):
    hc = _client(patients)
    hc.interpret_labs("t1")
    assert _interpret_subject(hc) is None


# --- 2. the trend key in get_labs ---------------------------------------------

_TREND = {"analyte": "Creatinine", "check": "kdigo-aki-creatinine",
          "message": ("Between Sep 1 and Sep 7, 2026, your creatinine rose "
                      "from 0.8 to 1.3 mg/dL in 6 days. A rise like this can "
                      "mean the kidneys are under strain. " + PROMPTLY)}


def _labs(lines, bundle=None):
    class _HC:
        def interpret_labs(self, _t):
            return {"summary": {}, "disclaimer": "d", "bundle": bundle or {},
                    "consumer": {"lines": lines, "trends": [_TREND],
                                 "note": "n"}}
    return _HC()


@pytest.mark.parametrize("surface", ["web", "imessage", ""])
def test_trend_lines_get_their_own_key(surface):
    from tests.test_careagents_imessage_demo_round3 import _line, _scored
    # Matched lines: the `latest` rewrite. Unmatched: the lines as they came.
    for hc in (_labs([_line("H")], {"entry": [
                   _scored("b", "2026-09-07T00:00:00Z", 1.3, "H")]}),
               _labs([_line("N"), _line("N")])):
        out = json.loads(_execute_tool(hc, "t", "get_labs", {}, [],
                                       surface=surface))
        assert out["trends"] == [_TREND["message"]]
        assert "trends" not in out["consumer_summary"]
        note = out["note"]
        assert "as written" in note and "promptly" in note
        assert "diagnos" in note


def test_no_trend_no_trend_key_and_no_trend_note():
    class _HC:
        def interpret_labs(self, _t):
            return {"summary": {}, "disclaimer": "d", "bundle": {},
                    "consumer": {"lines": [{"analyte": "Potassium",
                                            "flag": "N", "message": "ok"}]}}
    out = json.loads(_execute_tool(_HC(), "t", "get_labs", {}, []))
    assert "trends" not in out
    assert "as written" not in out.get("note", "")


# --- 3. cross-layer: a chat turn on the real engine ---------------------------

def _creatinine_sentence(text):
    return ("creatinine rose from 0.8 to 1.3 mg/dL in 6 days" in text
            and PROMPTLY in text)


def test_a_chat_turn_on_the_real_engine_reports_the_creatinine_rise(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    seen = []

    def complete(_cfg, system, history, tools):
        results = [m["content"] for m in history if m.get("role") == "tool"]
        if not results:
            return _Turn("", [_Call(0, "get_labs", {})])
        seen.append(results[-1])
        # Answer with the trend line as handed over, as the note asks.
        return _Turn(" ".join(json.loads(results[-1]).get("trends") or [])
                     or "no trend")
    monkeypatch.setattr("careagents.worker.llm.complete", complete)

    c = chain.app.test_client()
    _login(c, svc, monkeypatch, email="acceptance@example.com")
    # A deployed worker has polled the engine before a turn is admitted;
    # this one polls once, finding nothing, so the admission check sees it.
    from careagents.worker import RunWorker
    RunWorker(cfg, chain.hc, svc, "test-worker").run_once()
    r = _turn(c, chain.agent, "what do my labs say?")
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    [tool_result] = seen
    out = json.loads(tool_result)
    [trend] = out["trends"]
    assert _creatinine_sentence(trend), trend
    assert "trends" not in out["consumer_summary"]
    # The range line is still there, separately.
    latest = {x["analyte"]: x for x in out["consumer_summary"]["latest"]}
    assert latest["Creatinine"]["flag"] == "H"
    # And the answer reached the chat stream.
    assert _creatinine_sentence(body), body[-800:]


def test_by_text_the_trend_sentence_survives_run_reply(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    out = json.loads(_execute_tool(chain.hc, TENANT, "get_labs", {}, [],
                                   agent_id=chain.agent, surface="imessage"))
    [trend] = out["trends"]
    assert _creatinine_sentence(trend), trend
    assert "text" in out["note"].lower()

    reply = imessage.run_reply(
        [{"type": "agent.text",
          "payload": {"text": f"**Your labs.** {trend}"}}],
        cfg.origin, chain.agent)
    assert _creatinine_sentence(reply), reply
    assert "**" not in reply
