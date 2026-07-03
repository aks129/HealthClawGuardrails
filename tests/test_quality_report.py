from r6.quality.measures import evaluate_nqf0018
from r6.quality.report import (
    build_measure_resource, build_individual_report, build_summary_report,
    MEASURE_URL,
)


def _pop(report, code):
    for g in report["group"]:
        for p in g["population"]:
            if p["code"]["coding"][0]["code"] == code:
                return p["count"]
    return None


def test_measure_resource_shape():
    m = build_measure_resource()
    assert m["resourceType"] == "Measure"
    assert m["url"] == MEASURE_URL
    assert m["scoring"]["coding"][0]["code"] == "proportion"
    codes = {p["code"]["coding"][0]["code"]
             for g in m["group"] for p in g["population"]}
    assert {"initial-population", "denominator", "denominator-exclusion",
            "numerator"} <= codes


def test_individual_report_controlled():
    result = {
        "in_initial_population": True, "in_denominator": True,
        "denominator_exclusion": False, "in_numerator": True,
        "control_threshold": {"systolic": 140, "diastolic": 90},
    }
    rep = build_individual_report("Patient/p1", result, "2026-01-01", "2026-12-31")
    assert rep["resourceType"] == "MeasureReport"
    assert rep["type"] == "individual"
    assert rep["measure"] == MEASURE_URL
    assert rep["subject"] == {"reference": "Patient/p1"}
    assert rep["period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert _pop(rep, "denominator") == 1
    assert _pop(rep, "numerator") == 1
    assert rep["group"][0]["measureScore"]["value"] == 1.0


def test_individual_report_uncontrolled():
    result = {
        "in_initial_population": True, "in_denominator": True,
        "denominator_exclusion": False, "in_numerator": False,
        "control_threshold": {"systolic": 140, "diastolic": 90},
    }
    rep = build_individual_report("Patient/p1", result, "2026-01-01", "2026-12-31")
    assert _pop(rep, "numerator") == 0
    assert rep["group"][0]["measureScore"]["value"] == 0.0


def test_summary_report_rate():
    pop = {"denominator": 4, "numerator": 3, "exclusions": 1,
           "performance_rate": 0.75}
    rep = build_summary_report(pop, "2026-01-01", "2026-12-31")
    assert rep["type"] == "summary"
    assert _pop(rep, "denominator") == 4
    assert _pop(rep, "numerator") == 3
    assert _pop(rep, "denominator-exclusion") == 1
    assert rep["group"][0]["measureScore"]["value"] == 0.75


def test_end_to_end_engine_to_report():
    patient = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-06-01"}
    cond = {"resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                                 "code": "I10"}]},
            "subject": {"reference": "Patient/p1"}}
    obs = {"resourceType": "Observation", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
           "subject": {"reference": "Patient/p1"},
           "effectiveDateTime": "2026-09-01",
           "component": [
               {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 132}},
               {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {"value": 82}}]}
    result = evaluate_nqf0018(patient, [cond], [obs], "2026-01-01", "2026-12-31")
    rep = build_individual_report("Patient/p1", result, "2026-01-01", "2026-12-31")
    assert _pop(rep, "numerator") == 1
