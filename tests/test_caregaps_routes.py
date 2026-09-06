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
    # no longer dismissed as "no diabetes diagnosis found in your connected
    # records". Against subject None it was, because the Condition never
    # matched.
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
# A tombstone is not a patient, and not evidence (#422)
#
# Every query in this module counted soft-deleted rows. Three consequences,
# each worse than the last: a tenant that deleted its duplicate Patient still
# reads as ambiguous and is told nothing (so the only apparent next move is a
# hard delete against production); a supplied `?subject=` naming a deleted
# Patient still hands its date of birth to the rules; and a deleted clinical
# record still CLOSES a gap, which withholds a due screening rather than
# repeating one.
#
# None of that is reachable TODAY, and the tests say so by writing the
# tombstone themselves rather than through a product path. Verified while
# fixing this: `is_deleted = True` appears on one line in the repository
# (r6/routes.py:2824, the demo walkthrough, on Permission rows) and no route
# accepts DELETE. #422's own text cites a `delete-my-records` flow; no such
# flow exists yet.
#
# That is the argument for fixing the readers now rather than the argument
# against. The day a delete path ships, every one of these three is live at
# once, silently, in a clinical surface — and nothing in the diff that ships
# it would look wrong.

def _soft_delete(app, tenant_id, resource_type, resource_id):
    """Tombstone one row, exactly as the delete paths do."""
    with app.app_context():
        row = db.session.get(R6Resource, (tenant_id, resource_type, resource_id))
        assert row is not None, "nothing to soft-delete — check the seed"
        row.is_deleted = True
        db.session.commit()


def test_a_soft_deleted_patient_no_longer_makes_the_match_ambiguous(
        client, app, tenant_id, tenant_headers):
    """#422, stated as the operator's experience: they deleted the duplicate,
    and the symptom must move.

    MUTATION: drop `is_deleted=False` from _resolve_subject -> red, and the
    answer goes back to "ambiguous-patient" for a tenant with one live
    Patient.
    """
    _seed_patient(app, tenant_id, pid="p-live")
    _seed_patient(app, tenant_id, pid="p-gone")
    _soft_delete(app, tenant_id, "Patient", "p-gone")

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    resolution = json.loads(
        _resp_param(r.get_json(), "subjectResolution")["valueString"])
    assert resolution == {"state": "tenant-default", "subject": "Patient/p-live"}


def test_the_last_patient_being_deleted_reads_as_no_patient_not_a_default(
        client, app, tenant_id, tenant_headers):
    """The other side of the same filter. A tombstone must not be RESOLVED as
    the tenant's own Patient either — that would evaluate a deleted person and
    report a confident state while doing it.

    MUTATION: filter `is_deleted=True` (an easy inversion to typo) -> red.
    """
    _seed_patient(app, tenant_id, pid="p-only")
    _soft_delete(app, tenant_id, "Patient", "p-only")

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "no-patient", "subject": None}
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert consumer["unevaluated"] == "no-patient"


def test_a_supplied_subject_naming_a_deleted_patient_gets_no_demographics(
        client, app, tenant_id, tenant_headers):
    """The half `_resolve_subject` cannot cover: a supplied subject never
    passes through the resolver, so `_patient_for` needs its own filter.

    The route already knows what to do with a subject it cannot read — #417
    made that `check-incomplete`, which tells the caller the answer is partial
    instead of presenting it as a clean sheet. A deleted Patient must land
    there, identically to one that was never stored.

    Asserted against a live control of the same age and sex, so the test
    cannot pass by the endpoint being broken for everyone.

    MUTATION: drop `is_deleted=False` from _patient_for -> red.
    """
    _seed_patient(app, tenant_id, pid="p-erased", gender="female",
                  birth=_birth_date_for_age(58))
    _soft_delete(app, tenant_id, "Patient", "p-erased")
    _seed_patient(app, tenant_id, pid="p-alive", gender="female",
                  birth=_birth_date_for_age(58))

    def _unevaluated(pid):
        body = client.get(f"/r6/fhir/Patient/$care-gaps?subject=Patient/{pid}",
                          headers=tenant_headers).get_json()
        return json.loads(
            _resp_param(body, "consumerSummary")["valueString"]).get(
                "unevaluated")

    # Not `is None`: a live patient with partial evidence reports
    # 'evidence-not-read', which is a different sentence about a different
    # gap. The property is that only the deleted one is unreadable.
    assert _unevaluated("p-alive") != "check-incomplete", (
        "the live control is already check-incomplete — this test would pass "
        "for the wrong reason")
    assert _unevaluated("p-erased") == "check-incomplete", (
        "a deleted Patient was read as demographics and evaluated as though "
        "the record were still there")


def test_a_soft_deleted_observation_no_longer_closes_a_gap(
        client, app, tenant_id, tenant_headers):
    """The most consequential of the three, and the one that reaches a person.

    `_seed_patient` stores a blood-pressure Observation, which is what closes
    `bp-screening`. Delete it and the gap must REOPEN. If a tombstone still
    counts as evidence, a patient who deleted a record is told they are up to
    date on a screening the system no longer has.

    MUTATION: drop `is_deleted=False` from _resources_for -> red.
    """
    _seed_patient(app, tenant_id, pid="p-bp")

    before = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p-bp",
                         headers=tenant_headers).get_json()
    open_before = [g["rule_id"] for g in
                   json.loads(_resp_param(before, "summary")["valueString"])["gaps"]]
    assert "bp-screening" not in open_before, (
        "the seeded Observation should close bp-screening — if it does not, "
        "this test is not exercising what it claims")

    _soft_delete(app, tenant_id, "Observation", "o-p-bp")

    after = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p-bp",
                        headers=tenant_headers).get_json()
    open_after = [g["rule_id"] for g in
                  json.loads(_resp_param(after, "summary")["valueString"])["gaps"]]
    assert "bp-screening" in open_after, (
        "a soft-deleted Observation still closed the gap")


def test_a_resource_row_is_born_live_rather_than_null(app, tenant_id):
    """The trap under every `is_deleted=False` filter in this repository.

    The column is nullable with a PYTHON-side default. `is_deleted = 0` does
    not match NULL in SQL, so a row written without that default would be
    invisible to this module's three filters and to all 18 in r6/routes.py —
    live data, silently unreadable, with no error anywhere.

    Verified rather than assumed: the model's custom __init__ does not set the
    field, and the row still lands as False because SQLAlchemy applies the
    column default at INSERT.

    MUTATION: remove `default=False` from R6Resource.is_deleted -> red, and
    every newly written resource disappears from every read path.
    """
    with app.app_context():
        db.session.add(R6Resource(
            resource_type="Patient",
            resource_json=json.dumps({"resourceType": "Patient", "id": "p-new"}),
            resource_id="p-new", tenant_id=tenant_id))
        db.session.commit()
        stored = db.session.get(R6Resource, (tenant_id, "Patient", "p-new"))
        assert stored.is_deleted is False, (
            f"a new row was written with is_deleted={stored.is_deleted!r}; "
            "NULL is invisible to every read filter in the codebase")
        assert R6Resource.query.filter_by(
            tenant_id=tenant_id, resource_type="Patient",
            is_deleted=False).count() == 1


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

    # A 50yo woman: the A1c rule is gated out (no diabetes Condition), and
    # colorectal is undecided because this check does not read stool-based
    # tests (#425) — five decide.
    #
    # PIN MOVED with #425, in the PR that moved it. The property this test
    # exists for is unchanged and is asserted below: the rules read the
    # record. What moved is that one rule now declines for a reason of OURS.
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["indeterminate"] == 1
    assert summary["due"] + summary["up_to_date"] == 5

    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    # PIN MOVED with #436, in the PR that moved it: 5 -> 6. The five decided
    # screenings are unchanged; colorectal now gets a line of its own saying
    # it could not be checked, instead of appearing only in the note. The
    # property this test exists for is untouched — the rules read the record.
    assert len(consumer["lines"]) == 6
    assert [line["status"] for line in consumer["lines"]].count(
        "indeterminate") == 1

    # The point of #389 half two, stated directly: nothing is undecided for
    # want of demographics, because the evaluator was handed the record. The
    # one undecided screening names our coverage, never the person.
    assert consumer["unevaluated"] == "evidence-not-read"
    assert consumer["unevaluated_titles"] == ["Colorectal cancer screening"]
    assert "limit on the check" in consumer["unevaluated_note"]
    assert "were not available" not in r.get_data(as_text=True)
    for demographic in ("date of birth", "sex was not recorded"):
        assert demographic not in consumer["unevaluated_note"]


def test_the_fallback_path_now_carries_a_reason_that_is_true(
        client, app, tenant_id, tenant_headers):
    """The production shape of #417's partial list, reachable only once the
    evaluator sees the record.

    On the held path this same call answered `check-incomplete`, because a
    reason about demographics we never read could not be true. Now we read
    them, so the sex-gated pair is explained by the record's own gap and
    named.

    PIN MOVED with #425, in the PR that moved it: colorectal joins the
    undecided set for a reason of ours, so the marker carries both causes and
    keeps them apart. That is the same property this test was written for —
    a reason that is true of what it covers — now proven across two causes
    instead of one.

    MUTATION: pass `supplied` to _patient_for again -> red.
    """
    _seed_patient(app, tenant_id, pid="p-nosex", gender=None,
                  birth=_birth_date_for_age(50))

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    consumer = json.loads(
        _resp_param(r.get_json(), "consumerSummary")["valueString"])
    # PIN MOVED with #436, in the PR that moved it: 3 -> 4. Colorectal joins
    # the lines with a "could not check" status; the two sex-gated screenings
    # do not, because with no sex on file nothing here knows they apply to
    # this person. All three stay named in the marker below.
    assert len(consumer["lines"]) == 4
    assert consumer["unevaluated"] == "partly-unchecked"
    assert consumer["unevaluated_count"] == 3
    assert consumer["unevaluated_titles"] == [
        "Colorectal cancer screening",
        "Cervical cancer screening (Pap)",
        "Breast cancer screening (mammogram)"]

    note = consumer["unevaluated_note"]
    # The record's gap explains the two it explains, and ours explains ours.
    assert "sex was not recorded" in note
    assert "Cervical cancer screening (Pap)" in note
    assert "does not yet read" in note and "stool-based" in note
    assert "Colorectal cancer screening" in note
    # The birthDate was on file, and no reason may say otherwise.
    assert "date of birth" not in note


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
    #
    # PIN MOVED AGAIN with #436, in the PR that moved it: 3 -> 4. Colorectal
    # is still undecided and still named in the marker; what changed is that
    # it now also reaches the patient as a line, which is the whole of #436.
    # The two sex-gated screenings get no line — nothing here knows whether
    # they apply to a record with no sex on it.
    assert len(consumer["lines"]) == 4
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


# ─────────────────────────────────────────────
# The rules do not run against a patient nobody resolved (#542)
#
# The route set `not_evaluated` and then called the evaluator anyway. Two
# things came out of that, and the retro's shape is both of them: a check that
# examined nothing printed the verdict of a check that examined everything
# (docs/2026-08-02-retro.md).
#
#   1. FALSE not_applicable. With no patient the condition gate (evaluate.py,
#      before the age gate) fires first, so the A1c rule reported
#      "not_applicable" — a decision about a person nobody looked at.
#   2. THE AUDIT LIED. detail read `evaluated=7` for seven evaluations that
#      did not happen.
#
# The consumer summary already suppressed the rows (#389/#417), so this was
# invisible from the page. The payload and the audit carried them to every
# other caller — the MCP tool, the Telegram summary, curl.

def _audit_details(app, tenant_id):
    """Every care-gaps audit row this tenant has, newest last."""
    from r6.models import AuditEventRecord
    with app.app_context():
        rows = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, event_type="read",
            resource_type="Patient").all()
        return [r.detail for r in rows
                if (r.detail or "").startswith("care-gaps;")]


def test_an_unresolved_subject_evaluates_no_rules_at_all(
        client, app, tenant_id, tenant_headers):
    """Two Patient rows, no subject: nothing may be decided about either.

    `summary.evaluated` is the flag a client branches on without knowing the
    reason family. It is NOT sufficient on its own to catch the defect — it
    stays False whether or not the evaluator ran — so the emptiness of the
    rows is asserted directly.

    MUTATION: call evaluate_care_gaps(patient, ...) unconditionally again ->
    red on `detail`, on `not_applicable`, and on the audit row.
    """
    _seed_patient(app, tenant_id, pid="p-one")
    _seed_patient(app, tenant_id, pid="p-two")

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()

    detail = json.loads(_resp_param(body, "detail")["valueString"])
    assert detail == [], (
        "the rules ran against a patient the route could not resolve")

    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["evaluated"] is False
    assert summary["total"] == 0
    # The one that was patient-visible: A1c fell through the condition gate
    # and reported "not applicable" for a person nobody identified.
    assert summary["not_applicable"] == 0
    assert summary["due"] == 0
    assert summary["indeterminate"] == 0


def test_the_audit_says_nothing_was_evaluated_when_nothing_was(
        client, app, tenant_id, tenant_headers):
    """One AuditEvent on the caller path, PHI-free, counting what happened.

    MUTATION: restore the unconditional evaluate_care_gaps call -> red with
    `evaluated=7`.
    """
    _seed_patient(app, tenant_id, pid="p-one")
    _seed_patient(app, tenant_id, pid="p-two")

    client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers, json={})

    details = _audit_details(app, tenant_id)
    assert len(details) == 1, f"expected exactly one care-gaps audit row: {details}"
    assert details[0] == (
        "care-gaps; subject=ambiguous-patient evaluated=0 due=0")


def test_no_patient_row_evaluates_nothing_either(
        client, app, tenant_id, tenant_headers):
    """The other caller reason. Same property, and the more common one — a
    tenant that has connected nothing yet."""
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    body = r.get_json()
    assert json.loads(_resp_param(body, "detail")["valueString"]) == []
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["evaluated"] is False
    assert summary["not_applicable"] == 0
    assert _audit_details(app, tenant_id) == [
        "care-gaps; subject=no-patient evaluated=0 due=0"]


def test_a_subject_we_hold_no_record_for_evaluates_nothing(
        client, app, tenant_id, tenant_headers):
    """`check-incomplete` — a supplied subject naming a row we do not hold.

    The third reason, and the one where `state` and the not-evaluated reason
    differ: the subject WAS supplied, so the audit says so, and `evaluated=0`
    carries the rest.
    """
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/nope",
                   headers=tenant_headers)
    body = r.get_json()
    assert json.loads(_resp_param(body, "detail")["valueString"]) == []
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["evaluated"] is False
    assert _audit_details(app, tenant_id) == [
        "care-gaps; subject=supplied evaluated=0 due=0"]


def test_a_resolved_patient_is_still_evaluated_and_says_so(
        client, app, tenant_id, tenant_headers):
    """The guard must not become a blanket refusal to evaluate anyone.

    MUTATION: return `results = []` unconditionally -> red.
    """
    _seed_patient(app, tenant_id, pid="p-real", gender="female",
                  birth=_birth_date_for_age(50))

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    body = r.get_json()
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["evaluated"] is True
    assert summary["total"] == 7
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    assert len(detail) == 7
    assert _audit_details(app, tenant_id) == [
        "care-gaps; subject=tenant-default evaluated=7 due=%d" % summary["due"]]
