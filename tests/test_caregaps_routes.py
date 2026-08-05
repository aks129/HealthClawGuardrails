# tests/test_caregaps_routes.py
import json
from datetime import date

from r6.models import R6Resource, db


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource.get("id"),
            tenant_id=tenant_id))
        db.session.commit()


def _seed_patient(app, tenant_id, pid="p1", gender="female", birth="1968-05-01"):
    # gender=None omits the element rather than coding it null — the shape a
    # real feed sends when sex was never captured.
    patient = {"resourceType": "Patient", "id": pid, "birthDate": birth}
    if gender:
        patient["gender"] = gender
    _store(app, patient, tenant_id)
    _store(app, {"resourceType": "Observation", "id": f"o-{pid}", "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                 "subject": {"reference": f"Patient/{pid}"},
                 "effectiveDateTime": "2026-03-01"}, tenant_id)


def _birth_date_for_age(years):
    """A birthDate that is `years` old whichever day the suite runs.

    `as_of` is date.today() inside the route, so a hardcoded year would walk
    a patient across the age bands and change which rules apply.
    """
    return f"{date.today().year - years}-01-01"


def _resp_param(body, name):
    for p in body["parameter"]:
        if p["name"] == name:
            return p
    return None


def test_care_gaps_returns_parameters_with_summary(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id)
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Parameters"
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["total"] > 0
    assert "bp-screening" not in [g["rule_id"] for g in summary["gaps"]]
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert "lines" in consumer
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    assert isinstance(detail, list) and len(detail) == summary["total"]
    assert _resp_param(body, "disclaimer") is not None


def test_care_gaps_get_also_works(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id, pid="p2")
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p2",
                   headers=tenant_headers)
    assert r.status_code == 200
    assert r.get_json()["resourceType"] == "Parameters"


def test_care_gaps_requires_tenant(client):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1")
    assert r.status_code == 400


def test_care_gaps_unknown_patient_is_ok_but_indeterminate(client, tenant_headers):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/does-not-exist",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    # No patient found -> no birthDate/gender available -> indeterminate/not-run rules
    assert summary["total"] >= 0


# ─────────────────────────────────────────────
# The production call shape: no subject at all
# ─────────────────────────────────────────────

def test_care_gaps_with_no_subject_uses_the_tenants_own_patient(
        client, app, tenant_id, tenant_headers):
    """The shape both production callers actually send (#389).

    CareAgents' get_care_gaps and the care-gaps MCP App page post an empty
    body with no ?subject=. Every other test in this file passes one, which
    is why this survived: with subject None the resource filter compares
    every subject.reference against None, nothing matches, and the evaluator
    is handed an empty record.

    MUTATION: delete the fallback branch in _resolve_subject (return the
    supplied subject unconditionally) -> both asserts red.
    """
    _seed_patient(app, tenant_id, pid="p-solo")
    _store(app, {"resourceType": "Condition", "id": "c-dm",
                 "subject": {"reference": "Patient/p-solo"},
                 "code": {"coding": [{"system": "http://snomed.info/sct",
                                      "code": "44054006"}]}}, tenant_id)

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "tenant-default", "subject": "Patient/p-solo"}

    # The tenant's own Condition reached the evaluator: the diabetes rule is
    # no longer dismissed as "applies to patients with a diabetes diagnosis".
    # Against subject None it was, because the Condition never matched.
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    a1c = next(d for d in detail if d["rule_id"] == "diabetes-a1c")
    assert a1c["status"] != "not_applicable", (
        "the tenant's stored Condition did not reach the evaluator")


def test_care_gaps_with_no_patient_row_says_so_rather_than_no_gaps(
        client, tenant_headers):
    """"Could not look" must not arrive as "looked and found none".

    Third instance of this shape in a week (#379 medications, #381 the
    brief's care gaps, #390 the intake form), and the one on the highest
    traffic entry point.

    MUTATION: collapse the unresolved reason (return a bare empty consumer
    summary) -> red.
    """
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "no-patient", "subject": None}
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert consumer["lines"] == []
    assert consumer["unevaluated"] == "no-patient"
    assert "no screenings outstanding" in consumer["unevaluated_note"]


def test_care_gaps_with_two_patient_rows_is_ambiguous_not_empty(
        client, app, tenant_id, tenant_headers):
    """Two Patient rows and no subject: the fallback cannot pick one. That is
    its own outcome, not a clean sheet.

    MUTATION: take rows[0] instead of reporting ambiguity -> red.
    """
    _seed_patient(app, tenant_id, pid="p-one")
    _seed_patient(app, tenant_id, pid="p-two")
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "ambiguous-patient", "subject": None}
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert consumer["unevaluated"] == "ambiguous-patient"


def test_care_gaps_with_a_supplied_subject_reports_that_state(
        client, app, tenant_id, tenant_headers):
    """A caller-supplied subject is not the fallback, and says so."""
    _seed_patient(app, tenant_id, pid="p-named")
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p-named",
                   headers=tenant_headers)
    resolution = json.loads(
        _resp_param(r.get_json(), "subjectResolution")["valueString"])
    assert resolution == {"state": "supplied", "subject": "Patient/p-named"}


# ─────────────────────────────────────────────
# What we say when we did not look (#417)
# ─────────────────────────────────────────────

def test_a_subject_we_hold_no_record_for_claims_nothing_about_that_record(
        client, tenant_headers):
    """A supplied subject naming a row we do not hold reads nothing, so it
    cannot say what that record was missing.

    `check-incomplete` (#417) covers it. It used to answer
    "Your date of birth and sex were not available to this check", which
    describes a record this deployment has never seen.

    MUTATION: drop the `check-incomplete` branch in the route -> red.
    """
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/nope",
                   headers=tenant_headers)
    assert r.status_code == 200
    consumer = json.loads(
        _resp_param(r.get_json(), "consumerSummary")["valueString"])
    assert consumer["unevaluated"] == "check-incomplete"
    for claim in ("date of birth", "were not available", "not recorded"):
        assert claim not in consumer["unevaluated_note"]


def test_the_fallback_path_evaluates_the_patient_it_resolved(
        client, app, tenant_id, tenant_headers):
    """The production call shape, against a record holding birthDate AND gender.

    The fallback resolved the Patient and reported it in `subjectResolution`,
    then handed the evaluator `supplied` — None. Age and sex unknown, all
    seven rules indeterminate, and the person got a reason instead of an
    answer. #389 half two; released by the clinical ruling on the cadence
    table, not by engineering.

    MUTATION: pass `supplied` to _patient_for again -> red.
    """
    _seed_patient(app, tenant_id, pid="p-onfile", gender="female",
                  birth=_birth_date_for_age(50))

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "tenant-default", "subject": "Patient/p-onfile"}

    # A 50yo woman: only the A1c rule is gated out (no diabetes Condition).
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["indeterminate"] == 0
    assert summary["due"] + summary["up_to_date"] == 6

    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert len(consumer["lines"]) == 6
    assert "unevaluated" not in consumer
    assert "were not available" not in r.get_data(as_text=True)


def test_the_fallback_path_now_carries_a_reason_that_is_true(
        client, app, tenant_id, tenant_headers):
    """The production shape of #417's partial list, reachable only once the
    evaluator sees the record.

    On the held path this same call answered `check-incomplete`, because a
    reason about demographics we never read could not be true. Now we read
    them, so `sex-unavailable` describes the record and names the two
    screenings it could not decide.

    MUTATION: pass `supplied` to _patient_for again -> red.
    """
    _seed_patient(app, tenant_id, pid="p-nosex", gender=None,
                  birth=_birth_date_for_age(50))

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    consumer = json.loads(
        _resp_param(r.get_json(), "consumerSummary")["valueString"])
    assert len(consumer["lines"]) == 4
    assert consumer["unevaluated"] == "sex-unavailable"
    assert consumer["unevaluated_count"] == 2
    assert consumer["unevaluated_titles"] == [
        "Cervical cancer screening (Pap)",
        "Breast cancer screening (mammogram)"]
    assert "date of birth" not in consumer["unevaluated_note"]


def test_a_partial_screening_list_says_how_much_of_it_is_missing(
        client, app, tenant_id, tenant_headers):
    """A Patient with a birthDate and no gender — routine in real feeds.

    Four screenings are decided and the two sex-gated ones are not. The marker
    was attached only when the consumer list was ENTIRELY empty, so four lines
    shipped as the whole answer and cervical and mammography disappeared
    without a word (#417).

    Driven with an explicit subject, which is the only shape that reads the
    Patient today — the fallback path is held behind the clinical gate, and
    a reason about this record would be false there (see the fallback test
    above). The defect and its fix are in the consumer summary either way.

    MUTATION: restore `if not lines:` in build_consumer_summary -> red.
    """
    _seed_patient(app, tenant_id, pid="p-nosex", gender=None,
                  birth=_birth_date_for_age(50))

    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p-nosex",
                   headers=tenant_headers)
    assert r.status_code == 200
    consumer = json.loads(
        _resp_param(r.get_json(), "consumerSummary")["valueString"])
    # PIN MOVED with #425, in the PR that moved it. Colorectal screening used
    # to be decided here and reported "due"; it is now undecided, because this
    # check reads only colonoscopy and sigmoidoscopy procedures and a patient
    # on annual FIT would have matched nothing. So the decided list drops from
    # four to three and a third screening joins the undecided set — for a
    # different reason than the other two.
    assert len(consumer["lines"]) == 3
    assert consumer["unevaluated_count"] == 3
    assert consumer["unevaluated_titles"] == [
        "Colorectal cancer screening",
        "Cervical cancer screening (Pap)",
        "Breast cancer screening (mammogram)"]

    # The two causes stay apart. "Your sex was not recorded" is true of the
    # sex-gated pair and false of colorectal, which failed for our reason and
    # not the record's — borrowing one reason for both is #417 with a
    # different subject.
    assert consumer["unevaluated"] == "partly-unchecked"
    note = consumer["unevaluated_note"]
    for title in consumer["unevaluated_titles"]:
        assert title in note
    assert "sex was not recorded" in note
    assert "does not yet read" in note and "stool-based" in note
    # Still nothing about a birthDate that was on file.
    assert "date of birth" not in note


# ─────────────────────────────────────────────
# MCP App page (embedded HTML surface)
# ─────────────────────────────────────────────

class TestCareGapsMcpApp:
    """The care-gaps MCP App page — layout ported from SmartHealthConnect
    (archived), data path rebuilt on the engine's own $care-gaps operation."""

    def test_serves_html_with_mcp_app_profile(self, client):
        resp = client.get('/r6/fhir/mcp-apps/care-gaps/?tenant_id=desktop-demo')
        assert resp.status_code == 200
        assert 'text/html' in resp.headers['Content-Type']
        assert 'profile=mcp-app' in resp.headers['Content-Type']
        assert resp.headers.get('X-MCP-App') == 'care-gaps'
        body = resp.get_data(as_text=True)
        assert '<title>Care Gaps' in body
        assert 'desktop-demo' in body

    def test_page_reads_through_the_guarded_operation_only(self, client):
        """The page's only data path is the engine's $care-gaps operation —
        no direct table reads, no alternate endpoints (the SHC failure mode)."""
        body = client.get('/r6/fhir/mcp-apps/care-gaps/').get_data(as_text=True)
        assert '/r6/fhir/Patient/$care-gaps' in body
        # no other fetch targets appear in the page
        import re
        fetches = re.findall(r"fetch\('([^']+)'", body)
        assert fetches == ['/r6/fhir/Patient/$care-gaps']

    def test_plain_terms_list_renders_the_message_not_the_object(self, client):
        """`consumer.lines` are objects — {rule_id, title, message}.

        The page interpolated the object itself, which renders as
        "[object Object]". Latent only because this box has never had a
        populated list to draw; it would surface the moment one worked.
        String-level assertion — these pages have no JS harness in the Python
        suite.
        """
        body = client.get('/r6/fhir/mcp-apps/care-gaps/').get_data(as_text=True)
        assert 'esc(l.message' in body
        assert 'esc(l)' not in body

    def test_plain_terms_list_shows_what_was_not_checked(self, client):
        """Whatever this box does draw, it must not draw a partial list as a
        whole one (#417)."""
        body = client.get('/r6/fhir/mcp-apps/care-gaps/').get_data(as_text=True)
        assert 'unevaluated_note' in body

    def test_no_tenant_renders_empty_shell(self, client):
        resp = client.get('/r6/fhir/mcp-apps/care-gaps/')
        assert resp.status_code == 200
        assert 'Enter a tenant id' in resp.get_data(as_text=True)
