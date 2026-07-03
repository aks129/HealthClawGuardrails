"""FHIR Measure + MeasureReport builders for NQF 0018 (pure)."""

MEASURE_URL = "https://healthclaw.io/Measure/nqf0018-controlling-high-bp"
_POP_SYSTEM = "http://terminology.hl7.org/CodeSystem/measure-population"


def _population(code, count):
    return {"code": {"coding": [{"system": _POP_SYSTEM, "code": code}]},
            "count": count}


def build_measure_resource():
    """A FHIR Measure describing NQF 0018 / CMS165 (proportion scoring)."""
    return {
        "resourceType": "Measure",
        "id": "nqf0018-controlling-high-bp",
        "url": MEASURE_URL,
        "version": "1.0.0",
        "name": "ControllingHighBloodPressure",
        "title": "Controlling High Blood Pressure (NQF 0018 / CMS165)",
        "status": "active",
        "experimental": True,
        "description": (
            "Percentage of patients 18-85 with a diagnosis of hypertension whose "
            "most recent blood pressure during the measurement period was "
            "adequately controlled (<140/90). Calculator implementation — not a "
            "certified eCQM; denominator exclusions are partial (v1)."
        ),
        "scoring": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/measure-scoring",
            "code": "proportion"}]},
        "improvementNotation": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/measure-improvement-notation",
            "code": "increase"}]},
        "group": [{
            "population": [
                _population("initial-population", 0),
                _population("denominator", 0),
                _population("denominator-exclusion", 0),
                _population("numerator", 0),
            ],
        }],
    }


def _bool_count(flag):
    return 1 if flag else 0


def build_individual_report(subject_ref, result, period_start, period_end):
    """MeasureReport (type=individual) from an evaluate_nqf0018() result."""
    return {
        "resourceType": "MeasureReport",
        "status": "complete",
        "type": "individual",
        "measure": MEASURE_URL,
        "subject": {"reference": subject_ref},
        "period": {"start": period_start, "end": period_end},
        "group": [{
            "population": [
                _population("initial-population",
                            _bool_count(result.get("in_initial_population"))),
                # HL7 convention: denominator is the pre-exclusion population.
                _population("denominator",
                            _bool_count(result.get("in_denominator_gross"))),
                _population("denominator-exclusion",
                            _bool_count(result.get("denominator_exclusion"))),
                _population("numerator",
                            _bool_count(result.get("in_numerator"))),
            ],
            "measureScore": {"value": 1.0 if result.get("in_numerator") else 0.0},
        }],
    }


def build_summary_report(pop_result, period_start, period_end):
    """MeasureReport (type=summary) from an evaluate_population() result."""
    return {
        "resourceType": "MeasureReport",
        "status": "complete",
        "type": "summary",
        "measure": MEASURE_URL,
        "period": {"start": period_start, "end": period_end},
        "group": [{
            "population": [
                _population("initial-population", pop_result.get("initial_population", 0)),
                _population("denominator", pop_result.get("denominator", 0)),
                _population("denominator-exclusion", pop_result.get("exclusions", 0)),
                _population("numerator", pop_result.get("numerator", 0)),
            ],
            "measureScore": {"value": pop_result.get("performance_rate")},
        }],
    }
