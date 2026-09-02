"""Ingest resilience — real EHR data breaks naive assumptions.

Both bugs here only manifested on Postgres (SQLite does not enforce varchar
length), so 966 green tests missed them until a live Epic export
(2026-07-08): 65/250 resource ids exceeded varchar(64), the first over-length
id truncation-errored, and the un-rolled-back session poisoned all 250.
"""

import uuid

from r6.models import R6Resource
from models import db


def test_resource_id_column_fits_real_ehr_ids():
    # Epic ids run to ~109 chars; the FHIR 64-char limit is widely violated.
    # Assert the column is wide enough WITHOUT relying on Postgres to enforce
    # it (SQLite silently accepts any length, which is why this regressed).
    assert R6Resource.__table__.c.id.type.length >= 128


def test_long_id_resource_stores_and_round_trips(client, tenant_id):
    long_id = "e-" + uuid.uuid4().hex + uuid.uuid4().hex + "X" * 20  # ~86 chars
    assert len(long_id) > 64
    res = R6Resource(resource_type="Observation",
                     resource_json='{"resourceType":"Observation","id":"%s"}' % long_id,
                     resource_id=long_id, tenant_id=tenant_id)
    db.session.add(res)
    db.session.commit()
    got = R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation", id=long_id).first()
    assert got is not None and got.id == long_id


def test_fasten_ingest_still_accepts_real_epic_shaped_long_ids(client, tenant_id):
    """Regression guard for #267's `_RESOURCE_ID_PATTERN`
    (r6/fasten/ingester.py): `^[A-Za-z0-9\\-\\.]{1,64}$`, enforced
    unconditionally inside `_ingest_one` -- the SAME function Fasten's
    `stream_ingest` calls for every real EHR export, not just the
    direct-upload path #267 was fixing.

    `test_long_id_resource_stores_and_round_trips` above proves the DB
    column is wide enough for a ~86-char Epic id, but it writes the
    R6Resource row directly and never calls `_ingest_one` -- so it could
    not have caught this. This test drives the real ingester entry point.

    #267's own docstring says the pattern is "FHIR id: ... per the spec",
    and 64 chars IS the FHIR spec limit -- but this file's docstring (and
    the 2026-07-08 live-Epic-export incident it documents: 65/250 resource
    ids over varchar(64), which is why R6Resource.id was widened to
    varchar(128) instead of truncated or rejected) is the standing record
    that real Epic exports routinely violate that limit. `_ingest_one`
    previously stored whatever id Epic sent, subject only to the DB column
    width; #267 now rejects it at the application layer as `invalid_id`
    before it ever reaches the DB -- same ceiling the 128-char column was
    widened specifically to avoid, reintroduced silently (a skipped/
    invalid_id entry, not a crash) for the live Fasten/Epic connector.
    """
    from r6.fasten.ingester import _ingest_one

    long_id = "e-" + uuid.uuid4().hex + uuid.uuid4().hex + "X" * 20  # ~86 chars
    assert len(long_id) > 64
    resource = {"resourceType": "Observation", "id": long_id, "status": "final"}

    result, rid = _ingest_one(resource, tenant_id)

    assert result == "ok" and rid == long_id, (
        f"real Epic-shaped id (len={len(long_id)}) was refused as "
        f"{result!r} by _RESOURCE_ID_PATTERN -- #267's id-shape "
        "validation silently drops real Fasten/Epic resources whose "
        "ids exceed 64 characters, exactly the case "
        "test_resource_id_column_fits_real_ehr_ids above widened the "
        "column for"
    )


def test_ingest_error_rolls_back_session_so_next_resource_succeeds(client, tenant_id):
    # The core resilience contract: a failed resource must not poison the
    # session for the ones after it. Simulate by forcing one flush to fail,
    # rolling back, then confirming a clean insert still commits.
    from sqlalchemy.exc import IntegrityError

    good1 = R6Resource(resource_type="Observation",
                       resource_json="{}", resource_id="ir-good-1",
                       tenant_id=tenant_id)
    db.session.add(good1)
    db.session.commit()

    # Force a failure (duplicate composite PK — same tenant, type, AND id)
    # then the ingester's recovery: rollback.
    dup = R6Resource(resource_type="Observation",
                     resource_json="{}", resource_id="ir-good-1",
                     tenant_id=tenant_id)
    db.session.add(dup)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # this is what the ingester now does per-resource

    # After rollback the session is usable again — the "next" resource commits.
    good2 = R6Resource(resource_type="Observation",
                       resource_json="{}", resource_id="ir-good-2",
                       tenant_id=tenant_id)
    db.session.add(good2)
    db.session.commit()
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation",
        id="ir-good-2").first() is not None


def test_audit_resource_id_column_fits_real_ehr_ids():
    # The audit table stores the same resource ids; if it's narrower than
    # R6Resource.id, an audit-insert truncation rolls back the resource write
    # in the shared transaction (found live 2026-07-08 — only 2/56
    # observations survived). Must be at least as wide.
    from r6.models import AuditEventRecord, R6Resource
    assert (AuditEventRecord.__table__.c.resource_id.type.length
            >= R6Resource.__table__.c.id.type.length)


def test_audit_public_outcome_detail_code_is_bounded():
    from r6.models import AuditEventRecord
    column = AuditEventRecord.__table__.c.outcome_detail_code
    assert column.type.length == 64
    assert column.nullable is True


def test_curatr_engine_api_matches_ingester_usage():
    # The Fasten post-ingest scan imports CuratrEngine and calls
    # evaluate(resource) -> result.issues. Pin that contract so the import/
    # signature can't silently drift again (it did: stale 'CuratrEvaluator').
    from r6.curatr import CuratrEngine
    engine = CuratrEngine()
    result = engine.evaluate({"resourceType": "Condition", "id": "c1"})
    assert hasattr(result, "issues")
    assert isinstance(result.issues, list)


def test_resource_id_pattern_is_a_charset_control_not_a_length_control():
    """#267's id validation must keep rejecting injectable ids WITHOUT
    re-imposing the 64-char FHIR ceiling this file's incident forced us off.

    The security finding it closes is that a caller-supplied id reached
    `AuditEventRecord.resource_id`, which `health_compliance.py` exports into
    the auditor-facing compliance bundle — so an id could smuggle a name, a
    date, a path, or markup into an audit artifact. The CHARSET stops that.
    The LENGTH never had anything to do with it, and capping at 64 silently
    skipped real Epic resources on the live Fasten connector (caught in
    review, not by 1772 green tests).

    Pinned in both directions so a future "tighten it to spec" edit fails
    here instead of in production.
    """
    from r6.fasten.ingester import _RESOURCE_ID_PATTERN as pat
    from r6.models import R6Resource

    # Injectable shapes stay refused, regardless of length.
    for bad in ("../../etc/passwd", "Jane Doe 1980-01-01 MRN 12345",
                "<script>alert(1)</script>", "id with spaces", "a/b", "a_b"):
        assert not pat.fullmatch(bad), bad

    # Real Epic-shaped ids (~109 chars per the 2026-07-08 export) are fine.
    assert pat.fullmatch("e-" + "a" * 107)

    # The ceiling tracks the column width, so validation and storage cannot
    # disagree: an id we accept must be an id we can store.
    width = R6Resource.__table__.c.id.type.length
    assert pat.fullmatch("a" * width)
    assert not pat.fullmatch("a" * (width + 1))


# ---------------------------------------------------------------------------
# #286 (council D8): what `_ingest_one` does with an id that is not a
# well-formed string. Four cases, pinned in the shape the ruling gave them.
# `invalid_id` is in REFUSED_OUTCOMES, so each refusal pages rather than
# counting as a skip; keep it there.
# ---------------------------------------------------------------------------
def test_a_blank_resource_id_is_refused_as_invalid_id(client, tenant_id):
    """`""` is not an absent id. It used to earn a fabricated UUID, which
    turned every re-ingest of the same resource into a duplicate row
    instead of an upsert on (tenant, type, id). Refused, and refused as a
    refusal (#286).
    """
    from r6.fasten.ingester import REFUSED_OUTCOMES, _ingest_one

    result, rid = _ingest_one(
        {"resourceType": "Observation", "id": "", "status": "final"},
        tenant_id)

    assert (result, rid) == ("invalid_id", None)
    assert "invalid_id" in REFUSED_OUTCOMES
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation").count() == 0


def test_an_absent_resource_id_gets_a_uuid_and_is_stored(client, tenant_id):
    """No `id` key at all: mint one. This is the only case that fabricates
    an id, and it is fine because there is nothing to upsert against."""
    from r6.fasten.ingester import _ingest_one

    result, rid = _ingest_one(
        {"resourceType": "Observation", "status": "final"}, tenant_id)

    assert result == "ok"
    assert uuid.UUID(rid)  # a well-formed UUID, not "" or "None"
    row = R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation", id=rid).first()
    assert row is not None and row.is_deleted is False


def test_an_integer_resource_id_keeps_coercing_to_a_string(client, tenant_id):
    """Pre-existing and live on the Fasten connector: `123` is stored as
    `"123"`. Not widened, not narrowed — pinned (#286)."""
    from r6.fasten.ingester import _ingest_one

    result, rid = _ingest_one(
        {"resourceType": "Observation", "id": 123, "status": "final"},
        tenant_id)

    assert (result, rid) == ("ok", "123")
    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation", id="123"
    ).first() is not None


def test_any_other_non_string_resource_id_is_refused(client, tenant_id):
    """`str()` of a bool or a float passes the id pattern — `True` becomes
    `"True"`, `1.5` becomes `"1.5"` — so the type has to be checked before
    the shape. `bool` is a subclass of `int`, so it is refused explicitly
    rather than riding the int coercion. Objects and lists never passed the
    pattern; they are here so the whole set is one pin (#286).

    MUTATION: drop the isinstance guard in _ingest_one -> the first two red.
    """
    from r6.fasten.ingester import _ingest_one

    for bad in (True, False, 1.5, {"a": 1}, [1]):
        result, rid = _ingest_one(
            {"resourceType": "Observation", "id": bad, "status": "final"},
            tenant_id)
        assert (result, rid) == ("invalid_id", None), repr(bad)

    assert R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation").count() == 0


# ---------------------------------------------------------------------------
# #293 / #306: a record we REFUSED is not a record we chose to skip, and the
# SHC path never got the rollback the Fasten path was patched for on
# 2026-07-08. Both call sites collapsed every non-'ok' outcome into `skipped`
# with no log line, so a real export carrying an id outside the FHIR charset
# would be dropped and the import would report success.
# ---------------------------------------------------------------------------
def _bundle(*resources):
    return {"resourceType": "Bundle",
            "entry": [{"resource": r} for r in resources]}


def test_shc_ingest_rolls_back_so_one_bad_entry_does_not_wedge_the_batch(
        app, tenant_id, monkeypatch):
    """The 2026-07-08 incident, on the path that never got the fix: a failed
    flush poisons the session, so every LATER entry fails too — one bad
    resource silently costs the rest of the import.

    Two assertions, deliberately. The surviving-row assertion is the real
    one, and it is only meaningful on the Postgres lane — SQLite does not
    poison a session the way Postgres does, which is the same reason this
    whole file exists and why CI runs it against real Postgres. The
    rollback-was-called assertion is the one that holds everywhere, so a
    future edit that deletes the line fails on both lanes rather than
    quietly only on the one nobody runs locally.
    """
    from models import db
    from r6.shc import routes as shc

    calls = {"n": 0}
    rollbacks = {"n": 0}
    real = __import__(
        "r6.fasten.ingester", fromlist=["_ingest_one"])._ingest_one
    real_rollback = db.session.rollback

    def flaky(resource, tid, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("flush failed on entry 1")
        return real(resource, tid, **kw)

    def counting_rollback():
        rollbacks["n"] += 1
        return real_rollback()

    monkeypatch.setattr("r6.fasten.ingester._ingest_one", flaky)
    monkeypatch.setattr(db.session, "rollback", counting_rollback)

    obs = {"resourceType": "Observation", "id": "shc-good-1",
           "status": "final", "code": {"coding": [{"code": "x"}]}}
    shc._ingest_bundle(app, [dict(obs), dict(obs, id="shc-good-2")],
                       tenant_id, "flexpa", "job1")

    assert calls["n"] == 2, "the batch stopped after the failure"
    assert rollbacks["n"] >= 1, (
        "the failed entry left the session un-rolled-back — the exact "
        "failure r6/fasten/ingester.py was patched for on 2026-07-08")
    survived = R6Resource.query.filter_by(
        tenant_id=tenant_id, resource_type="Observation",
        id="shc-good-2").first()
    assert survived is not None, "entry 2 was lost to a poisoned session"


def test_a_refused_resource_is_counted_and_logged_apart_from_a_skip(
        app, tenant_id, caplog):
    """`skipped` means 'a type we do not store' — routine. `invalid_id` and
    `forbidden` mean 'data we refused'. Collapsing them hid the only signal
    that a real export had been silently truncated."""
    import logging

    from r6.shc import routes as shc

    good = {"resourceType": "Observation", "id": "shc-ok-1",
            "status": "final", "code": {"coding": [{"code": "x"}]}}
    unsupported = {"resourceType": "NotAResourceWeStore", "id": "x1"}
    refused = {"resourceType": "Observation", "id": "has spaces/and-slash"}

    with caplog.at_level(logging.WARNING):
        counts = shc._ingest_bundle(
            app, [good, unsupported, refused], tenant_id, "flexpa", "job2")

    assert counts["ingested"] == 1
    assert counts["skipped"] == 1, "an unsupported type is a routine skip"
    assert counts["refused"] == 1, "refusing data is its own outcome"

    refusal_logs = [r.getMessage() for r in caplog.records
                    if "refused" in r.getMessage()]
    assert refusal_logs, "a refusal has to be findable without a DB dive"
    joined = " ".join(refusal_logs)
    assert "invalid_id" in joined and "Observation" in joined
    assert "has spaces" not in joined, (
        "the refused id was logged — that field carries names and MRNs in "
        "the wild, which is why it is the one thing that must not appear")


def test_the_import_summary_reports_refusals(app, tenant_id):
    """A count nobody can see is not a signal. The audit detail carries it,
    PHI-free, so a silent truncation shows up without a log dive."""
    from r6.shc import routes as shc

    seen = {}

    def capture(**kw):
        seen.update(kw)

    import r6.shc.routes as shcmod
    original = shcmod.record_audit_event
    shcmod.record_audit_event = capture
    try:
        shc._ingest_bundle(
            app, [{"resourceType": "Observation", "id": "bad id!"}],
            tenant_id, "flexpa", "job3")
    finally:
        shcmod.record_audit_event = original

    assert "refused=1" in seen.get("detail", "")
    assert "bad id" not in seen.get("detail", "")


def test_the_fasten_path_refuses_the_same_way_the_shc_path_does(app, caplog):
    """Three callers of `_ingest_one` already handle failure three different
    ways (#306). Both ingest paths are pinned to one refusal vocabulary here
    so a fix applied to one does not quietly skip the other — which is how
    the SHC path went two years without the rollback the Fasten path got."""
    import json as _json
    import logging
    from unittest.mock import patch

    from models import db
    from r6.fasten.ingester import stream_ingest
    from r6.fasten.models import FastenJob

    lines = [
        _json.dumps({"resourceType": "Observation", "id": "fasten-ok-1",
                     "status": "final", "code": {"coding": [{"code": "x"}]}}),
        _json.dumps({"resourceType": "NotAResourceWeStore", "id": "nope"}),
        _json.dumps({"resourceType": "Observation",
                     "id": "Jane Doe 1980-01-01 MRN 12345"}),
    ]

    class _Resp:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with app.app_context():
        job = FastenJob(task_id="refusal-job", org_connection_id="c1",
                        tenant_id="test-tenant", status="pending")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        with patch("r6.fasten.ingester.httpx.stream",
                   return_value=_Resp()), caplog.at_level(logging.WARNING):
            stream_ingest(app, job_id,
                          ["https://download.example.invalid/e.ndjson"],
                          "test-tenant")

        db.session.expire_all()
        done = db.session.get(FastenJob, job_id)
        assert done.ingested_resources == 1
        assert done.skipped_resources == 1, (
            "2 means the refused resource was folded back into the routine "
            "skips — the collapse this fixes")

    joined = caplog.text
    assert "refused a resource" in joined and "invalid_id" in joined
    assert "Jane Doe" not in joined and "MRN" not in joined, (
        "the refused id reached the log — it is refused precisely because "
        "it looks like this")


# ---------------------------------------------------------------------------
# #377: the other half of #293. A refusal is now named. A SKIP is not.
# `skipped` is one integer covering every type this store does not keep, so
# "which type did we drop?" has no answer anywhere — not in the audit trail,
# not in the log. #377's own precondition is "check the ingest counters
# before building anything", and the counters cannot be checked: an EHR that
# sends the medication list as MedicationStatement is indistinguishable from
# one whose export carried a few hundred ExplanationOfBenefit rows.
#
# The type name is the one caller-supplied field these tests care about, so
# it goes through an allowlist that CONSTRUCTS (docs/agent-task-guide.md §2)
# rather than being repeated verbatim — a real NDJSON line can put a name
# there as readily as it can put one in an id.
# ---------------------------------------------------------------------------
def _ndjson_stream(lines):
    """The httpx.stream context manager `stream_ingest` consumes."""
    class _Resp:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def _run_fasten_ingest(app, task_id, resources, caplog):
    """Drive the REAL Fasten path over an NDJSON export of `resources`.

    A unit test over `_ingest_one` proves an outcome code. It cannot prove
    the import summary carries it, which is the whole defect. Returns
    (job, summary_audit_detail).
    """
    import json as _json
    import logging
    from unittest.mock import patch

    from models import db
    import r6.fasten.ingester as ingester_mod
    from r6.fasten.models import FastenJob

    lines = [_json.dumps(r) for r in resources]
    seen: dict = {}

    def capture(**kw):
        if kw.get("event_type") == "fasten_import_complete":
            seen.update(kw)

    original = ingester_mod.record_audit_event
    ingester_mod.record_audit_event = capture
    try:
        with app.app_context():
            job = FastenJob(task_id=task_id, org_connection_id="c1",
                            tenant_id="test-tenant", status="pending")
            db.session.add(job)
            db.session.commit()
            job_id = job.id

            with patch("r6.fasten.ingester.httpx.stream",
                       return_value=_ndjson_stream(lines)), \
                    caplog.at_level(logging.INFO):
                ingester_mod.stream_ingest(
                    app, job_id,
                    ["https://download.example.invalid/e.ndjson"],
                    "test-tenant")

            db.session.expire_all()
            return db.session.get(FastenJob, job_id), seen.get("detail", "")
    finally:
        ingester_mod.record_audit_event = original


def test_a_skipped_medication_statement_is_named_in_the_import_summary(
        app, caplog):
    """#377. An EHR that records "what the patient is actually taking" as
    MedicationStatement — many do, because a statement of current use is not
    a prescription — has every one of those resources dropped at ingest.

    The drop itself is a policy question. The SILENCE is not: the patient
    asks their agent what they are taking and gets a confident answer over a
    hole, and nobody operating the system can tell that the hole is there.
    One integer cannot distinguish a feed sending medications we discard
    from an export carrying a few hundred billing rows we never wanted.
    """
    job, detail = _run_fasten_ingest(app, "skip-named-job", [
        {"resourceType": "Observation", "id": "skipnamed-ok-1",
         "status": "final", "code": {"coding": [{"code": "x"}]}},
        {"resourceType": "MedicationStatement", "id": "skipnamed-ms-1",
         "status": "recorded"},
        {"resourceType": "ExplanationOfBenefit", "id": "skipnamed-eob-1"},
        {"resourceType": "ExplanationOfBenefit", "id": "skipnamed-eob-2"},
    ], caplog)

    assert job.ingested_resources == 1
    assert job.skipped_resources == 3

    assert "MedicationStatement:1" in detail, (
        "the import summary counted three skips and named none of them — "
        "#377's own precondition ('check the ingest counters') cannot be "
        f"answered from {detail!r}")
    assert "ExplanationOfBenefit:2" in detail, (
        "a per-type count is what separates 'this feed sends medications we "
        "drop' from 'this export was mostly billing'")
    assert "MedicationStatement" in caplog.text, (
        "an operator reading the job's completion log still cannot see it")


def test_the_skipped_type_summary_cannot_carry_a_name_from_the_feed(
        app, caplog):
    """The allowlist half. `resourceType` is caller-supplied on every line of
    a real export, so naming it back into audit detail is the reflection rule
    (docs/agent-task-guide.md §2) with a different field — audit `detail` is
    exported into the auditor-facing compliance bundle.

    Unknown types still have to COUNT, or this trades one silence for
    another.
    """
    job, detail = _run_fasten_ingest(app, "skip-unnamed-job", [
        {"resourceType": "Jane Doe 1980-01-01 MRN 12345", "id": "un-1"},
        {"resourceType": "MedicationStatement", "id": "un-ms-1"},
    ], caplog)

    assert job.skipped_resources == 2, "an unnameable type still gets counted"
    assert "other:1" in detail, (
        "the unknown type vanished from the summary instead of being "
        "counted under a code-owned name")
    for leak in ("Jane Doe", "MRN", "1980-01-01"):
        assert leak not in detail, (
            f"{leak!r} reached the audit detail — the type name was repeated "
            "verbatim rather than constructed from the allowlist")
        assert leak not in caplog.text, f"{leak!r} reached the log"


def test_the_shc_path_names_skipped_types_the_same_way(app, tenant_id):
    """Both ingest paths, one vocabulary — the #306 lesson. The SHC path is
    the one that historically went years without a fix the Fasten path got.
    """
    from r6.shc import routes as shc

    seen: dict = {}

    def capture(**kw):
        seen.update(kw)

    import r6.shc.routes as shcmod
    original = shcmod.record_audit_event
    shcmod.record_audit_event = capture
    try:
        counts = shc._ingest_bundle(
            app,
            [{"resourceType": "Observation", "id": "shc-skipnamed-1",
              "status": "final", "code": {"coding": [{"code": "x"}]}},
             {"resourceType": "MedicationStatement", "id": "shc-ms-1"}],
            tenant_id, "flexpa", "job377")
    finally:
        shcmod.record_audit_event = original

    assert counts["ingested"] == 1 and counts["skipped"] == 1
    assert counts["skipped_types"] == {"MedicationStatement": 1}, (
        "the returned counts are the assertable surface (#293); a per-type "
        "breakdown no test can read is the next version of the same defect")
    assert "MedicationStatement:1" in seen.get("detail", "")


def test_ingest_context_tells_its_caller_what_it_dropped(client, tenant_headers):
    """The third ingest path, and the only SYNCHRONOUS one — the caller is
    holding the response. It answered `resource_count: 1` for a two-entry
    bundle and said nothing about the other entry, which is the same silence
    with a 201 on it.
    """
    import json as _json

    resp = client.post(
        '/r6/fhir/Bundle/$ingest-context',
        data=_json.dumps({
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [
                {'resource': {'resourceType': 'Patient', 'id': 'ctx-pt-377'}},
                {'resource': {'resourceType': 'MedicationStatement',
                              'id': 'ctx-ms-377'}},
            ],
        }),
        content_type='application/json', headers=tenant_headers)

    assert resp.status_code == 201
    body = resp.get_json()
    assert body['resource_count'] == 1
    assert body['skipped_count'] == 1, (
        "one of two entries was discarded and the response reported only "
        "what it kept")
    assert body['skipped_types'] == {'MedicationStatement': 1}
