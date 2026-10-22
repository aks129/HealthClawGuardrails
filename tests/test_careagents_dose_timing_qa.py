"""QA on #1015: timing rendered from structure must not misstate the order.

Each test here failed against the PR head (3479621). The rendering is safe as
a privacy channel (closed maps, bounded numbers), but a rendering that drops
part of the structure says something the record does not:

- an as-needed order coded the R4 way (`asNeededCodeableConcept`) or the
  R5/R6 way (`asNeededFor`) read as a fixed schedule. The engine strips the
  dosage text ("as needed for pain"), so the code is the only as-needed
  signal left, and it was ignored;
- `frequencyMax` / `periodMax` ranges read as their lower bound;
- `when` was neither deduplicated nor bounded, so one row could put most of
  a megabyte into every get_health_summary result.
"""

from __future__ import annotations

import json

import pytest

from careagents import personas
from careagents.agent import _summarize_bundle

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
PAIN = {"coding": [{"system": "http://snomed.info/sct", "code": "22253000"}]}
Q6H = {"repeat": {"frequency": 1, "period": 6, "periodUnit": "h"}}


def _timing(dosage, rt="MedicationRequest"):
    res = {"resourceType": rt, "id": "m1", "status": "active",
           "medicationCodeableConcept": {"coding": [
               {"system": RXNORM, "code": "860975"}]},
           ("dosageInstruction" if rt == "MedicationRequest"
            else "dosage"): [dosage]}
    [item] = _summarize_bundle({"resourceType": "Bundle", "total": 1,
                                "entry": [{"resource": res}]})
    return item.get("timing")


@pytest.mark.parametrize("prn", [
    {"asNeededCodeableConcept": PAIN},   # R4
    {"asNeededFor": [PAIN]},             # R5 / R6
])
def test_an_as_needed_order_never_reads_as_a_fixed_schedule(prn):
    [line] = _timing({"timing": Q6H, **prn})
    assert "as needed" in line


@pytest.mark.parametrize("repeat", [
    {"frequency": 1, "frequencyMax": 2, "period": 1, "periodUnit": "d"},
    {"frequency": 1, "period": 4, "periodMax": 6, "periodUnit": "h"},
])
def test_a_range_is_not_said_as_its_lower_bound(repeat):
    # "1 to 2 times a day" said as "once a day", "every 4 to 6 hours" as
    # "every 4 hours". Saying nothing is acceptable; saying a narrower
    # schedule than the record is not.
    timing = _timing({"timing": {"repeat": repeat}})
    assert timing not in (["once a day"], ["once every 4 hours"])


def test_repeated_when_codes_are_said_once_and_bounded():
    timing = _timing({"timing": {"repeat": {"when": ["CM"] * 50_000}}})
    assert timing == ["with breakfast"]
    assert len(json.dumps(timing)) < 200


def test_hourly_reads_as_english():
    assert _timing({"timing": {"repeat": {
        "frequency": 1, "period": 1, "periodUnit": "h"}}}) == [
            "once an hour"]


# --- QA round 2 on 805def91: fences a weakened bullet slipped past ------------
#
# Each clause below was deleted from SAFETY_CORE in a mutation run and the
# dose-timing tests stayed green. "pharmacist or prescriber" is asserted
# elsewhere, but it also occurs in "their pharmacist or prescriber can
# answer", so the suggestion after the general sentence could go unnoticed.

@pytest.mark.parametrize("clause", [
    # The brief's requirement: the general sentence ends on a professional.
    "and suggest confirming with their pharmacist or prescriber.",
    # What the general sentence may cover, and nothing wider.
    "general information about timing only: time of day, or with or "
    "without food.",
    # The fallback is an exception to "say only what the records show",
    # not a new license.
    "this is the one exception to saying only what the records show.",
])
def test_the_medication_rule_pins_its_remaining_fences(clause):
    assert clause in " ".join(personas.SAFETY_CORE.split())
