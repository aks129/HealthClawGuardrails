"""Composition regressions for issue #207.

An unreadable coded record is still a record.  These tests deliberately use a
code outside the server terminology table so the safe fallback—not a familiar
demo label—has to survive redaction, summarization, and the agent turn.
"""

from __future__ import annotations

import json

import pytest

from careagents import llm
from careagents.agent import _summarize_bundle, run_turn
from careagents.personas import system_prompt
from r6.redaction import apply_redaction


ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
ICD9 = "http://hl7.org/fhir/sid/icd-9-cm"
UNKNOWN_CODE = "Q99.9"


def _condition(system: str, code: str, **coding_fields) -> dict:
    return {
        "resourceType": "Condition",
        "status": "active",
        "code": {"coding": [{"system": system, "code": code,
                              **coding_fields}]},
    }


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": resource} for resource in resources],
    }


def _redacted_summary(*resources: dict) -> list[dict]:
    return _summarize_bundle(apply_redaction(_bundle(*resources)))


def test_unknown_code_survives_redaction_as_unreadable_summary():
    summary = _redacted_summary(_condition(
        ICD10,
        UNKNOWN_CODE,
        display="Chromosomal finding for Jane Secret",
    ))

    assert summary == [{
        "type": "Condition",
        "name": f"unlabeled record, code {UNKNOWN_CODE}",
        "unreadable": True,
        "status": "active",
    }]
    assert "Jane" not in str(summary)
    assert "Secret" not in str(summary)


def test_mixed_bundle_labels_known_and_flags_unknown():
    summary = _redacted_summary(
        _condition(ICD9, "250.00"),
        _condition(ICD10, UNKNOWN_CODE),
    )

    assert len(summary) == 2
    assert "diabetes" in summary[0]["name"].lower()
    assert summary[0].get("unreadable") is None
    assert summary[1]["name"].endswith(UNKNOWN_CODE)
    assert summary[1]["unreadable"] is True


@pytest.mark.parametrize("resource", [
    {"resourceType": "Condition", "code": {}},
    _condition(ICD10, UNKNOWN_CODE),
    _condition(ICD10, UNKNOWN_CODE, display="Unsafe upstream display"),
    {"resourceType": "Observation", "code": {"coding": []}},
    {"resourceType": "MedicationRequest",
     "medicationCodeableConcept": {}},
])
def test_summarizer_never_omits_name_after_redaction(resource):
    summary = _redacted_summary(resource)

    assert len(summary) == 1
    assert summary[0]["name"]


def test_agent_turn_receives_unreadable_record_and_never_emits_bare_no(
        monkeypatch):
    """Exercise the production composition without calling an external LLM.

    The stub asserts what the model actually receives on both rounds.  Its safe
    second response is conditional on the real safety prompt and serialized
    unreadable marker being present, so losing either side fails before an
    assistant answer is emitted.
    """

    redacted = apply_redaction(_bundle(
        _condition(ICD10, UNKNOWN_CODE, display="Jane Secret finding")))

    class HealthClawStub:
        def search(self, tenant, resource_type):
            assert tenant == "tenant-207"
            assert resource_type == "Condition"
            return redacted

    calls = 0

    def complete(_cfg, system, messages, tools):
        nonlocal calls
        calls += 1
        normalized_system = " ".join(system.split())
        assert "never treat it as absence" in normalized_system
        if calls == 1:
            assert tools
            return llm.LLMTurn(tool_calls=[llm.ToolCall(
                id="condition-search",
                name="search_records",
                arguments={"resource_type": "Condition"},
            )])

        assert messages[-1]["role"] == "tool"
        result = json.loads(messages[-1]["content"])
        assert result == [{
            "type": "Condition",
            "name": f"unlabeled record, code {UNKNOWN_CODE}",
            "unreadable": True,
            "status": "active",
        }]
        return llm.LLMTurn(text=(
            f"I found an active condition record with code {UNKNOWN_CODE}, "
            "but I cannot read its name here. Ask your clinician to identify "
            "the code; I cannot treat this record as absence."))

    monkeypatch.setattr(llm, "complete", complete)
    events = list(run_turn(
        object(),
        HealthClawStub(),
        "tenant-207",
        system_prompt("Test Agent", "direct"),
        [],
        "Do I have this condition?",
    ))

    answer = next(event["text"] for event in events
                  if event["type"] == "text").lower()
    forbidden = (
        "no, you",
        "you don't have",
        "you do not have",
        "no record of",
        "no such condition",
    )
    assert calls == 2
    assert not any(phrase in answer for phrase in forbidden)
    assert "cannot read" in answer
