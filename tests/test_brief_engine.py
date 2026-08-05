"""Unit tests for the Appointment Brief engine.

Three core cases:
  1. Full records — all five sections populated from realistic FHIR data.
  2. Empty records — all lists empty; engine produces empty sections, never
     'none' / 'no records' / 'you have no' strings in any field value.
  3. Partial records — some sections populated, others empty; engine produces
     correct output in populated sections, empty lists in others.

The unknown-never-absent assertion covers every test: no output field may
contain an absence string.
"""

from r6.brief.engine import (
    generate_brief,
    build_problems,
    build_medications,
    build_labs,
    build_care_gaps,
    build_visits,
    BriefResult,
    BriefField,
    CARE_GAPS_OK,
    CARE_GAPS_UNAVAILABLE,
    CARE_GAPS_REASON_ENGINE_ERROR,
)


# ---------------------------------------------------------------------------
# Absence-string assertions (unknown-never-absent doctrine)
# ---------------------------------------------------------------------------

_FORBIDDEN_ABSENCE_PHRASES = (
    "none",
    "no records",
    "no conditions",
    "no medications",
    "no labs",
    "you have no",
    "you do not have",
    "not found",
    "zero",
)


def _assert_no_absence_strings(result: BriefResult) -> None:
    all_fields = (
        result.problems
        + result.medications
        + result.labs
        + result.care_gaps
        + result.visits
    )
    for f in all_fields:
        for phrase in _FORBIDDEN_ABSENCE_PHRASES:
            assert phrase.lower() not in f.label.lower(), (
                f"Absence phrase {phrase!r} found in label {f.label!r}"
            )
            assert phrase.lower() not in f.value.lower(), (
                f"Absence phrase {phrase!r} found in value {f.value!r}"
            )


# ---------------------------------------------------------------------------
# FHIR fixtures
# ---------------------------------------------------------------------------

def _condition(id_, code_text, status="active", onset="2021-03-15"):
    return {
        "resourceType": "Condition",
        "id": id_,
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": status}]
        },
        "code": {"text": code_text},
        "onsetDateTime": onset,
    }


def _med_request(id_, name, dosage="10mg daily", status="active"):
    return {
        "resourceType": "MedicationRequest",
        "id": id_,
        "status": status,
        "medicationCodeableConcept": {"text": name},
        "dosageInstruction": [{"text": dosage}],
    }


def _observation(id_, code_text, value, unit, date="2026-07-01T09:00:00Z"):
    return {
        "resourceType": "Observation",
        "id": id_,
        "code": {"text": code_text},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": date,
    }


def _encounter(id_, type_text, status="finished", date="2026-06-15T10:00:00Z"):
    return {
        "resourceType": "Encounter",
        "id": id_,
        "status": status,
        "type": [{"text": type_text}],
        "period": {"start": date},
    }


def _care_gap_result(items):
    return {"consumer": {"due": items}}


# ---------------------------------------------------------------------------
# Case 1: Full records
# ---------------------------------------------------------------------------

class TestFullRecords:
    CONDITIONS = [
        _condition("c-1", "Hypertension", "active", "2019-01-10"),
        _condition("c-2", "Type 2 diabetes mellitus", "active", "2020-06-01"),
        _condition("c-3", "Seasonal allergies", "resolved"),  # resolved — excluded
    ]
    MEDS = [
        _med_request("m-1", "Lisinopril", "10mg once daily"),
        _med_request("m-2", "Metformin", "500mg twice daily"),
        _med_request("m-3", "Cetirizine", "10mg as needed", status="completed"),  # excluded
    ]
    OBSERVATIONS = [
        _observation("o-1", "Blood pressure", 128, "mmHg", "2026-07-15T08:00:00Z"),
        _observation("o-2", "HbA1c", 7.2, "%", "2026-06-01T09:00:00Z"),
        _observation("o-3", "Total cholesterol", 195, "mg/dL", "2026-05-10T08:00:00Z"),
    ]
    ENCOUNTERS = [
        _encounter("e-1", "Annual wellness visit", "finished", "2026-06-01T10:00:00Z"),
        _encounter("e-2", "Cardiology follow-up", "planned", "2026-08-20T14:00:00Z"),
    ]
    CARE_GAPS = _care_gap_result([
        {"measure": "Colorectal Cancer Screening", "reason": "No qualifying exam in period",
         "id": "gap-1"},
    ])

    def test_problems_only_active(self):
        problems = build_problems(self.CONDITIONS)
        assert len(problems) == 2
        assert all(isinstance(f, BriefField) for f in problems)
        assert problems[0].source_type == "Condition"
        assert "2019-01" in problems[0].value  # onset projected into value

    def test_medications_only_active(self):
        meds = build_medications(self.MEDS)
        assert len(meds) == 2
        assert meds[0].source_type == "MedicationRequest"
        assert "10mg once daily" in meds[0].value

    def test_labs_ordered_newest_first(self):
        labs = build_labs(self.OBSERVATIONS)
        assert len(labs) == 3
        assert "2026-07-15" in labs[0].value  # most recent first

    def test_care_gaps_populated(self):
        gaps = build_care_gaps(self.CARE_GAPS)
        assert gaps.status == CARE_GAPS_OK
        assert len(gaps.fields) == 1
        assert "Colorectal" in gaps.fields[0].label

    def test_visits_populated(self):
        visits = build_visits(self.ENCOUNTERS)
        assert len(visits) == 2

    def test_generate_brief_full(self):
        result = generate_brief(
            conditions=self.CONDITIONS,
            medication_requests=self.MEDS,
            observations=self.OBSERVATIONS,
            encounters=self.ENCOUNTERS,
            care_gap_result=self.CARE_GAPS,
        )
        assert isinstance(result, BriefResult)
        assert len(result.problems) == 2
        assert len(result.medications) == 2
        assert len(result.labs) == 3
        assert len(result.care_gaps) == 1
        assert len(result.visits) == 2
        _assert_no_absence_strings(result)


# ---------------------------------------------------------------------------
# Case 2: Empty records — unknown-never-absent
# ---------------------------------------------------------------------------

class TestEmptyRecords:
    def test_empty_inputs_produce_empty_sections_not_strings(self):
        result = generate_brief(
            conditions=[],
            medication_requests=[],
            observations=[],
            encounters=[],
            care_gap_result={},
        )
        assert result.problems == []
        assert result.medications == []
        assert result.labs == []
        assert result.care_gaps == []
        assert result.visits == []

    def test_no_absence_strings_on_empty(self):
        result = generate_brief(
            conditions=[],
            medication_requests=[],
            observations=[],
            encounters=[],
            care_gap_result={"consumer": {"due": []}},
        )
        _assert_no_absence_strings(result)

    def test_empty_returns_brief_result_not_none(self):
        result = generate_brief([], [], [], [], {})
        assert isinstance(result, BriefResult)


# ---------------------------------------------------------------------------
# Case 3: Partial records
# ---------------------------------------------------------------------------

class TestPartialRecords:
    def test_only_conditions_present(self):
        result = generate_brief(
            conditions=[_condition("c-1", "Hypertension")],
            medication_requests=[],
            observations=[],
            encounters=[],
            care_gap_result={},
        )
        assert len(result.problems) == 1
        assert result.medications == []
        assert result.labs == []
        _assert_no_absence_strings(result)

    def test_only_resolved_conditions_excluded(self):
        result = generate_brief(
            conditions=[_condition("c-1", "Appendectomy", status="resolved")],
            medication_requests=[],
            observations=[],
            encounters=[],
            care_gap_result={},
        )
        assert result.problems == []

    def test_labs_capped_at_ten(self):
        observations = [
            _observation(f"o-{i}", f"Lab {i}", i, "unit",
                         f"2026-0{(i % 9) + 1}-01T00:00:00Z")
            for i in range(15)
        ]
        labs = build_labs(observations)
        assert len(labs) == 10

    def test_source_ids_preserved(self):
        result = generate_brief(
            conditions=[_condition("cond-abc", "Hypertension")],
            medication_requests=[_med_request("med-xyz", "Atorvastatin")],
            observations=[_observation("obs-123", "Glucose", 95, "mg/dL")],
            encounters=[],
            care_gap_result={},
        )
        assert result.problems[0].source_id == "cond-abc"
        assert result.medications[0].source_id == "med-xyz"
        assert result.labs[0].source_id == "obs-123"


# ---------------------------------------------------------------------------
# Case 4: care gaps — the third state (#381)
# ---------------------------------------------------------------------------

class TestCareGapsThirdState:
    """"The screening review raised" and "you have no open screening gaps"
    must not produce the same section. An empty list reads to a patient as
    "you are up to date on your cancer screenings", so the ok state has to be
    earned by a result that actually says it."""

    def test_caller_reported_failure_is_unavailable(self):
        section = build_care_gaps({"status": CARE_GAPS_UNAVAILABLE,
                                   "reason": CARE_GAPS_REASON_ENGINE_ERROR})
        assert section.status == CARE_GAPS_UNAVAILABLE
        assert section.reason == CARE_GAPS_REASON_ENGINE_ERROR
        assert section.fields == []

    def test_missing_result_is_unavailable_not_no_gaps(self):
        assert build_care_gaps({}).status == CARE_GAPS_UNAVAILABLE
        assert build_care_gaps(None).status == CARE_GAPS_UNAVAILABLE  # type: ignore[arg-type]

    def test_consumer_payload_without_a_due_list_is_unavailable(self):
        # A payload we cannot read is not an answer. Reporting "nothing due"
        # off a shape that never carried the due items is the #381 defect.
        section = build_care_gaps({"consumer": {"note": "some disclaimer"}})
        assert section.status == CARE_GAPS_UNAVAILABLE
        assert section.fields == []

    def test_successful_empty_evaluation_is_ok(self):
        section = build_care_gaps({"consumer": {"due": []}})
        assert section.status == CARE_GAPS_OK
        assert section.fields == []
        assert section.reason == ""

    def test_unavailable_and_no_gaps_are_distinguishable(self):
        failed = generate_brief([], [], [], [], {})
        evaluated = generate_brief([], [], [], [], {"consumer": {"due": []}})
        assert failed.care_gaps == evaluated.care_gaps == []
        assert failed.care_gaps_status != evaluated.care_gaps_status
        assert failed.care_gaps_status == CARE_GAPS_UNAVAILABLE
        assert evaluated.care_gaps_status == CARE_GAPS_OK

    def test_reason_is_carried_to_the_brief(self):
        result = generate_brief([], [], [], [], {
            "status": CARE_GAPS_UNAVAILABLE,
            "reason": CARE_GAPS_REASON_ENGINE_ERROR,
        })
        assert result.care_gaps_reason == CARE_GAPS_REASON_ENGINE_ERROR
