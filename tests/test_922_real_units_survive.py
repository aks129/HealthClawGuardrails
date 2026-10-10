"""Good input survives (#922 QA F1–F4).

Every #922 test pinned bad input being blocked; nothing pinned real units
getting through, and 41 of 116 real UCUM codes were being redacted on every
FHIR read. This corpus is the other half: real units from real feeds pass
the engine's unit shape, and the lab units pass CareAgents' closed list.
"""
import pytest

from careagents.labs_timeline import UCUM_ALLOWED
from r6.redaction import apply_redaction, unit_is_shaped

#: Units real US lab and EHR feeds send, coded or as text.
REAL_UNITS = [
    "mg/dL", "mmol/L", "g/dL", "%", "mmol/mol", "10*3/uL", "10*6/uL",
    "x10E3/uL", "K/uL", "fL", "fl", "pg", "U/L", "[IU]/L", "[iU]/L",
    "k[IU]/L", "u[IU]/mL", "mIU/mL", "uIU/mL", "mIU/L", "meq/L", "mEq/L",
    "mosm/kg", "mL/min/{1.73_m2}", "{INR}", "INR", "{ratio}", "{score}",
    "{titer}", "{index}", "{copies}/mL", "/[HPF]", "/[LPF]", "s", "ms",
    "mm/h", "cm[H2O]", "mg/(24.h)", "ng/mL{FEU}", "mg/g{creat}", "[ppm]",
    "{actuat}", "[drp]", "[tsp_us]", "mm[Hg]", "kg", "[lb_av]", "[in_i]",
    "cm", "Cel", "[degF]", "°F", "°C", "{beats}/min", "/min", "kg/m2",
    "MG/DL", "years", "minutes", "days", "tablets", "Units", "Each",
]

#: The subset CareAgents puts in front of the model and on the chart.
LAB_UNITS = [
    "mg/dL", "mmol/L", "g/dL", "%", "mmol/mol", "10*3/uL", "x10E3/uL",
    "K/uL", "fL", "U/L", "u[IU]/mL", "mIU/mL", "uIU/mL", "mIU/L", "meq/L",
    "mEq/L", "mosm/kg", "mL/min/{1.73_m2}", "{INR}", "INR", "{ratio}",
    "{score}", "{copies}/mL", "/[HPF]", "/[LPF]", "s", "ms", "mm/h",
]


@pytest.mark.parametrize("unit", REAL_UNITS)
def test_a_real_unit_is_unit_shaped(unit):
    assert unit_is_shaped(unit)


@pytest.mark.parametrize("unit", REAL_UNITS)
def test_redaction_keeps_a_real_unit_and_its_code(unit):
    obs = {"resourceType": "Observation", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "2093-3"}]},
           "valueQuantity": {"value": 1.5, "unit": unit,
                             "system": "http://unitsofmeasure.org",
                             "code": unit}}
    vq = apply_redaction(obs)["valueQuantity"]
    assert vq["unit"] == unit
    assert vq["code"] == unit


@pytest.mark.parametrize("unit", LAB_UNITS)
def test_a_common_lab_unit_is_on_careagents_list(unit):
    assert unit in UCUM_ALLOWED
