"""Red-team of PR #922: free text that still reaches the model after the fix.

Each record below was accepted by the engine's ingest (13/13 on a live stack)
and read back through apply_redaction. The model-facing summary still carries
the marker. Synthetic data only.
"""
import pytest

from careagents.agent import _summarize_bundle
from r6.redaction import apply_redaction

NAME = "Quixtrandle"


def _model_view(resource):
    return str(_summarize_bundle({"entry": [{"resource": apply_redaction(resource)}]}))


def _obs(**kw):
    return {"resourceType": "Observation", "id": "x",
            "status": kw.get("status", "final"),
            "code": {"coding": [{"system": "http://loinc.org", "code": "13457-7"}]},
            "effectiveDateTime": kw.get("eff", "2026-09-01"),
            "valueQuantity": {"value": 99, "unit": "mg/dL"}}


def _cond(code):
    return {"resourceType": "Condition", "id": "c",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]}}


def test_status_free_text_does_not_reach_the_model():
    assert NAME not in _model_view(_obs(status=f"final {NAME}"))


def test_non_date_effective_does_not_reach_the_model():
    assert NAME[:6] not in _model_view(_obs(eff=f"{NAME}-ignore"))


@pytest.mark.parametrize("code", [
    "555-013-7719",                               # phone
    "123-45-6789",                                # SSN
    "٥٥٥٠١٣",       # Arabic-Indic digits
])
def test_identifier_shaped_code_does_not_reach_the_model(code):
    assert code not in _model_view(_cond(code))
