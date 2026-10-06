"""#867 follow-up: a subject is matched by resolving the reference, not by
comparing strings, and the ?subject= branch is capped like the fallback.

CareAgents now sends `?subject=` for every single-Patient tenant, so an
exact `subject.reference == "Patient/<id>"` test would silently drop real
records: Fasten exports and uploaded bundles carry absolute URLs and
`urn:uuid:` references, and some results carry no subject at all. Those
labs used to be read by the no-subject fallback; vanishing from chat is
worse than the missing trend this work set out to add.

One matcher (r6/caregaps/routes.py) serves labs, care gaps and the brief,
so the three cannot disagree about whose result a row is. The Fasten
ingester keeps the upstream id as the row id and stores no fullUrl, so a
`urn:uuid:` reference resolves against the Patient's stored id.

Synthetic data only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

PID = "8f14e45f-ceea-467f-a0e6-1b2a3c4d5e6f"   # a uuid-shaped Patient id


def _row(app, tenant_id, resource, when=None):
    from r6.models import R6Resource, db
    with app.app_context():
        row = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"], tenant_id=tenant_id)
        if when is not None:
            row.last_updated = when
        db.session.add(row)
        db.session.commit()


def _patient(app, tenant_id, pid=PID):
    _row(app, tenant_id, {"resourceType": "Patient", "id": pid})


def _obs(rid, ref=None, *, value=0.9, days_ago=1, code="2160-0",
         no_subject=False):
    obs = {"resourceType": "Observation", "id": rid, "status": "final",
           "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
           "effectiveDateTime": (datetime.now(timezone.utc)
                                 - timedelta(days=days_ago)).isoformat(),
           "valueQuantity": {"value": value, "unit": "mg/dL"}}
    if not no_subject:
        obs["subject"] = {"reference": ref or f"Patient/{PID}"}
    return obs


def _interpret(client, headers, subject=None):
    url = "/r6/fhir/Observation/$interpret"
    if subject:
        url += f"?subject={subject}"
    r = client.post(url, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    params = {p["name"]: p for p in r.get_json()["parameter"]}
    ids = [e["resource"]["id"]
           for e in params["return"]["resource"]["entry"]]
    return json.loads(params["summary"]["valueString"]), ids


#: Every form that names this tenant's one Patient.
SAME_PATIENT = [
    pytest.param(f"Patient/{PID}", id="relative"),
    pytest.param(f"Patient/{PID}/_history/3", id="versioned"),
    pytest.param(f"https://ehr.example.org/fhir/R4/Patient/{PID}",
                 id="absolute-url"),
    pytest.param(f"urn:uuid:{PID}", id="urn-uuid"),
]

#: Forms that name someone else, or only look like this Patient.
OTHER_PATIENT = [
    pytest.param("Patient/someone-else", id="relative-other"),
    pytest.param(f"Patient/{PID}x", id="relative-prefix"),
    pytest.param(f"https://ehr.example.org/fhir/Patient/{PID}x",
                 id="absolute-prefix"),
    pytest.param("urn:uuid:00000000-0000-4000-8000-000000000000",
                 id="urn-uuid-other"),
    pytest.param(f"https://ehr.example.org/fhir/Patient/{PID}/Observation/o9",
                 id="path-past-the-patient"),
    pytest.param(f"Patient/{PID}#contained", id="fragment"),
    # #878 QA: a relative reference is exactly Patient/<id>, an absolute
    # one ends in /Patient/<id> with no dot-segment, and nothing trails.
    pytest.param(f"Group/Patient/{PID}", id="relative-nested-under-group"),
    pytest.param(f"Observation/Patient/{PID}", id="relative-nested"),
    pytest.param(f"Patient/../Patient/{PID}", id="relative-dot-segment"),
    pytest.param(f"https://ehr.example.org/fhir/../Patient/{PID}",
                 id="absolute-dot-segment"),
    pytest.param(f"Patient/{PID}\n", id="trailing-newline"),
    pytest.param(f"FakePatient/{PID}", id="fake-patient"),
    pytest.param(f"Group/{PID}", id="not-a-patient"),
    pytest.param(f"https://ehr.example.org/fhir/Group/{PID}",
                 id="absolute-not-a-patient"),
]


@pytest.mark.parametrize("ref", SAME_PATIENT)
def test_each_reference_form_for_the_patient_matches(
        app, client, tenant_id, tenant_headers, ref):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("o1", ref))
    summary, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == ["o1"] and summary["ignored"] == 0


@pytest.mark.parametrize("ref", OTHER_PATIENT)
def test_another_patients_reference_does_not_match(
        app, client, tenant_id, tenant_headers, ref):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("o1", ref))
    _, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == []


@pytest.mark.parametrize("ref,expected", [
    ("Patient/p1", "p1"),
    ("Patient/p1/_history/2", "p1"),
    ("https://ehr.example.org/fhir/Patient/p1", "p1"),
    ("https://ehr.example.org/fhir/Patient/p1/_history/2", "p1"),
    ("urn:uuid:p1", "p1"),
    ("FakePatient/p1", None),
    ("Group/Patient/p1", None),
    ("Observation/Patient/p1", None),
    ("Patient/../Patient/p1", None),
    ("Patient/..", None),
    ("https://x.example/a/../Patient/p1", None),
    ("https://x.example/Patient/..", None),
    ("Patient/p1\n", None),
    ("Patient/p1/", None),
    (" Patient/p1", None),
    ("patient/p1", None),
    ("Patient/" + "a" * 128, "a" * 128),
    ("Patient/" + "a" * 129, None),
])
def test_the_reference_forms_the_matcher_accepts(ref, expected):
    from r6.caregaps.routes import referenced_patient_id
    assert referenced_patient_id(ref) == expected


def test_a_109_character_id_matches(app, client, tenant_id, tenant_headers):
    """Live Epic Patient ids reach 109 characters (#878 security)."""
    long_id = ("e" + "Xy3.-" * 22)[:109]
    assert len(long_id) == 109
    _patient(app, tenant_id, long_id)
    _row(app, tenant_id, _obs("o1", f"Patient/{long_id}"))
    _row(app, tenant_id, _obs("o2", no_subject=True))
    _, ids = _interpret(client, tenant_headers, f"Patient/{long_id}")
    assert sorted(ids) == ["o1", "o2"]


def test_an_identifier_only_subject_is_not_counted_as_malformed(
        app, client, tenant_id, tenant_headers):
    """No reference to resolve is not a broken row: it is not this
    patient's, and it is not `ignored`."""
    _patient(app, tenant_id)
    obs = _obs("o1")
    obs["subject"] = {"identifier": {"system": "urn:mrn", "value": "x"}}
    _row(app, tenant_id, obs)
    summary, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == [] and summary["ignored"] == 0


def test_no_subject_belongs_to_the_only_patient(
        app, client, tenant_id, tenant_headers):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("o1", no_subject=True))
    _, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == ["o1"]


@pytest.mark.parametrize("patients", [[], [PID, "p-two"]],
                         ids=["no-patient", "two-patients"])
def test_no_subject_belongs_to_nobody_unless_one_patient(
        app, client, tenant_id, tenant_headers, patients):
    for pid in patients:
        _patient(app, tenant_id, pid)
    _row(app, tenant_id, _obs("o1", no_subject=True))
    _, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == []


def test_no_subject_does_not_belong_to_a_patient_the_tenant_lacks(
        app, client, tenant_id, tenant_headers):
    """The caller asks about p-other; the tenant's one Patient is PID."""
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("o1", no_subject=True))
    _, ids = _interpret(client, tenant_headers, "Patient/p-other")
    assert ids == []


def test_mixed_reference_styles_lose_nothing_to_the_subject(
        app, client, tenant_id, tenant_headers):
    """The total with ?subject= equals the total without it on a
    single-Patient tenant: scoping to the patient drops no one's labs."""
    _patient(app, tenant_id)
    for n, ref in enumerate([f"Patient/{PID}",
                             f"https://ehr.example.org/fhir/Patient/{PID}",
                             f"urn:uuid:{PID}", None]):
        _row(app, tenant_id, _obs(f"o{n}", ref, no_subject=ref is None))
    with_subject, ids_with = _interpret(client, tenant_headers,
                                        f"Patient/{PID}")
    without, ids_without = _interpret(client, tenant_headers)
    assert with_subject["total"] == without["total"] == 4
    assert sorted(ids_with) == sorted(ids_without)


def test_the_creatinine_rise_is_found_across_reference_styles(
        app, client, tenant_id, tenant_headers):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("a", f"urn:uuid:{PID}", value=0.8, days_ago=6))
    _row(app, tenant_id, _obs(
        "b", f"https://ehr.example.org/fhir/Patient/{PID}", value=1.3,
        days_ago=0))
    summary, _ = _interpret(client, tenant_headers, f"Patient/{PID}")
    [trend] = summary["trends"]
    assert trend["kdigo_criterion"] == ["B"]


@pytest.mark.parametrize("ref", [f"urn:uuid:{PID}",
                                 f"https://ehr.example.org/fhir/Patient/{PID}"])
def test_a_resolved_reference_still_selects_the_sex_specific_range(
        app, client, tenant_id, tenant_headers, ref):
    """1.2 mg/dL is within the non-specific range (high 1.3) and above the
    female one (high 1.04)."""
    _row(app, tenant_id, {"resourceType": "Patient", "id": PID,
                          "gender": "female"})
    obs = _obs("o1", ref, value=1.2)
    obs["valueQuantity"]["code"] = "mg/dL"
    _row(app, tenant_id, obs)
    summary, _ = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert [f["flag"] for f in summary["flagged"]] == ["H"]


# --- the cap ------------------------------------------------------------------

def test_the_subject_branch_is_capped_like_the_fallback(
        app, client, tenant_id, tenant_headers):
    """Newest 200 by last update, the fallback's order: with one Patient
    the two branches read the very same rows."""
    from r6.labs.routes import STORED_OBSERVATION_CAP
    _patient(app, tenant_id)
    base = datetime(2026, 1, 1)
    n = STORED_OBSERVATION_CAP + 5
    for i in range(n):
        _row(app, tenant_id, _obs(f"o{i:03d}", f"Patient/{PID}"),
             when=base + timedelta(minutes=i))
    with_subject, ids_with = _interpret(client, tenant_headers,
                                        f"Patient/{PID}")
    without, ids_without = _interpret(client, tenant_headers)
    assert with_subject["total"] == without["total"] == STORED_OBSERVATION_CAP
    assert ids_with == ids_without
    # The oldest five are the ones left out.
    assert "o000" not in ids_with and f"o{n - 1:03d}" in ids_with


def test_at_the_cap_a_last_update_tie_is_broken_by_id(
        app, client, tenant_id, tenant_headers):
    """Every row updated at the same instant: which 200 are kept is decided
    by id, highest first, on both branches, not by the database's whim."""
    from r6.labs.routes import STORED_OBSERVATION_CAP
    _patient(app, tenant_id)
    same = datetime(2026, 1, 1)
    n = STORED_OBSERVATION_CAP + 1
    for i in range(n):
        _row(app, tenant_id, _obs(f"o{i:03d}", f"Patient/{PID}"), when=same)
    _, ids_with = _interpret(client, tenant_headers, f"Patient/{PID}")
    _, ids_without = _interpret(client, tenant_headers)
    assert ids_with == ids_without
    assert "o000" not in ids_with and f"o{n - 1:03d}" in ids_with


def test_the_cap_counts_the_patients_rows_not_everyones(
        app, client, tenant_id, tenant_headers):
    """Another person's newer rows must not use up this patient's slots."""
    from r6.labs.routes import STORED_OBSERVATION_CAP
    _patient(app, tenant_id)
    _patient(app, tenant_id, "p-two")
    base = datetime(2026, 1, 1)
    _row(app, tenant_id, _obs("mine", f"Patient/{PID}"), when=base)
    for i in range(STORED_OBSERVATION_CAP + 1):
        _row(app, tenant_id, _obs(f"x{i:03d}", "Patient/p-two"),
             when=base + timedelta(minutes=i + 1))
    _, ids = _interpret(client, tenant_headers, f"Patient/{PID}")
    assert ids == ["mine"]


# --- care gaps and the brief read through the same matcher -------------------

def test_care_gaps_reads_the_same_rows(app, tenant_id):
    from r6.caregaps.routes import subject_resources
    _patient(app, tenant_id)
    for n, ref in enumerate([f"Patient/{PID}",
                             f"https://ehr.example.org/fhir/Patient/{PID}",
                             f"urn:uuid:{PID}", None, "Patient/other"]):
        _row(app, tenant_id, _obs(f"o{n}", ref, no_subject=ref is None))
    with app.app_context():
        got = {r["id"] for r in subject_resources(
            "Observation", f"Patient/{PID}", tenant_id)}
    assert got == {"o0", "o1", "o2", "o3"}


def test_the_brief_finds_the_rise_across_reference_styles(
        app, client, tenant_id, tenant_headers):
    _patient(app, tenant_id)
    _row(app, tenant_id, _obs("a", f"urn:uuid:{PID}", value=0.8, days_ago=6))
    _row(app, tenant_id, _obs("b", value=1.3, days_ago=0, no_subject=True))
    r = client.get("/r6/fhir/AppointmentBrief", headers=tenant_headers)
    assert "Contact your clinician promptly." in r.get_data(as_text=True)
