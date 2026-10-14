"""How a record arrived: `R6Resource.source` and its `meta.tag` (FHI-42).

The engine records the CHANNEL a row came in through — a connected portal
(Fasten), a file the person uploaded, the made-up sample, or a SMART Health
Card — as a closed code set, and emits it as one code-only `meta.tag` in our
own system. It never names the clinic: that comes from upstream text or a
connection label, and both can identify a person (owner decision, 2026-10).

What these tests hold:
  - every ingest caller passes its own code, and none can forget to
    (`source=` is a required keyword on `_ingest_one`);
  - a code outside the set is refused at write time, on every ORM path;
  - nothing in an upload — the resource's own `meta`, or a body field — can
    set or override the code, and no upstream `meta.source`, tag `display`
    or `meta.security` surfaces on read, search or the share bundle;
  - the code is per row, so per tenant;
  - a row with no source (pre-migration, or engine-authored) emits no tag;
  - the tag survives redaction, and redaction still strips what it strips.
"""

from __future__ import annotations

import json

import pytest

from models import db
from r6.models import RECORD_SOURCE_SYSTEM, RECORD_SOURCES, R6Resource

_INGEST = "/r6/fhir/internal/ingest-bundle"

# Synthetic PHI canaries an upstream feed could put in `meta`.
_CANARY_URI = "https://portal.example.invalid/patients/Rosa-Kowalski-1971"
_CANARY_TAG = "Dr Canary Clinic for Rosa Kowalski"
_CANARY_SEC = "Rosa Kowalski restricted"


def _tag(code):
    return {"system": RECORD_SOURCE_SYSTEM, "code": code}


def _hostile_meta():
    """Everything an upstream can stamp into `meta`, including a tag in OUR
    system claiming a different channel."""
    return {
        "source": _CANARY_URI,
        "tag": [
            {"system": RECORD_SOURCE_SYSTEM, "code": "fasten",
             "display": _CANARY_TAG},
            {"system": "http://upstream.example/tags", "code": "x",
             "display": _CANARY_TAG},
        ],
        "security": [{"system": "http://upstream.example/sec",
                      "code": "R", "display": _CANARY_SEC}],
    }


def _condition(cid="src-c-1", meta=None):
    resource = {
        "resourceType": "Condition", "id": cid,
        "subject": {"reference": "Patient/src-p-1"},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "38341003"}]},
    }
    if meta is not None:
        resource["meta"] = meta
    return resource


def _upload(client, resources, tenant="test-tenant", **extra_body):
    body = {"bundle": {"resourceType": "Bundle", "type": "collection",
                       "entry": [{"resource": r} for r in resources]}}
    body.update(extra_body)
    r = client.post(_INGEST, data=json.dumps(body),
                    headers={"X-Tenant-Id": tenant,
                             "Content-Type": "application/json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _row(tenant, rtype, rid):
    db.session.expire_all()
    return R6Resource.query.filter_by(
        tenant_id=tenant, resource_type=rtype, id=rid).one()


# --- the code set and the write-time refusal ---------------------------------

def test_the_code_set_is_closed_and_names_channels_only():
    assert RECORD_SOURCES == frozenset(
        {"fasten", "upload", "sample", "connected-app"})
    assert RECORD_SOURCE_SYSTEM == (
        "https://healthclaw.io/fhir/CodeSystem/record-source")


def test_ingest_one_without_source_is_a_type_error(app):
    """No default: a new caller that forgets the channel fails loudly here,
    rather than storing an untagged row nobody notices.

    MUTATION: give `source` a default in `_ingest_one` -> red."""
    from r6.fasten.ingester import _ingest_one

    with pytest.raises(TypeError):
        _ingest_one(_condition(), "test-tenant")


@pytest.mark.parametrize("bad", [
    "fasten-dr-smith", "Fasten", "", " upload", "portal", 1, True,
])
def test_an_unknown_source_is_refused_and_nothing_is_stored(app, bad):
    """A clinic name smuggled in as a code is exactly what the closed set
    exists to stop.

    MUTATION: drop the `@validates('source')` check -> red."""
    from r6.fasten.ingester import _ingest_one
    from r6.models import AuditEventRecord

    with pytest.raises(ValueError):
        _ingest_one(_condition(), "test-tenant", source=bad)
    db.session.rollback()
    assert R6Resource.query.filter_by(tenant_id="test-tenant").count() == 0
    assert AuditEventRecord.query.filter_by(
        tenant_id="test-tenant").count() == 0


def test_an_unknown_source_is_refused_on_an_existing_row_too(app):
    from r6.fasten.ingester import _ingest_one

    _ingest_one(_condition(), "test-tenant", source="upload")
    db.session.commit()
    row = _row("test-tenant", "Condition", "src-c-1")
    version, content = row.version_id, row.resource_json
    with pytest.raises(ValueError):
        _ingest_one(_condition(meta={"source": _CANARY_URI}), "test-tenant",
                    source="fasten-dr-smith")
    # Refused BEFORE the upsert touched the row: a caller that catches the
    # error and carries on without a rollback must not flush new content.
    # MUTATION: drop the up-front check in `_ingest_one` -> red.
    assert (row.version_id, row.resource_json) == (version, content)
    assert row not in db.session.dirty
    db.session.rollback()
    assert _row("test-tenant", "Condition", "src-c-1").source == "upload"


def test_the_model_refuses_an_unknown_source_on_every_orm_write(app):
    """The refusal lives on the model, so a writer other than `_ingest_one`
    cannot store an off-list code either."""
    with pytest.raises(ValueError):
        R6Resource(resource_type="Condition", resource_json="{}",
                   resource_id="x", tenant_id="test-tenant",
                   source="Springfield Clinic")
    row = R6Resource(resource_type="Condition", resource_json="{}",
                     resource_id="x", tenant_id="test-tenant")
    with pytest.raises(ValueError):
        row.source = "Springfield Clinic"
    row.source = None  # NULL stays allowed: untagged rows are legal


# --- every caller passes its own code -----------------------------------------

def test_a_direct_upload_is_tagged_upload(client):
    _upload(client, [_condition()])
    assert _row("test-tenant", "Condition", "src-c-1").source == "upload"


def test_a_connected_app_import_is_tagged_connected_app(app):
    """`/shc/ingest` is the SmartHealthConnect bridge (Flexpa, Health
    Skillz, SMART-on-FHIR pulls), not a signed SMART Health Card."""
    from r6.shc import routes as shc

    counts = shc._ingest_bundle(app, [_condition()], "test-tenant",
                                "flexpa", "job-fhi42")
    assert counts["ingested"] == 1
    assert _row("test-tenant", "Condition",
                "src-c-1").source == "connected-app"


def test_a_fasten_import_is_tagged_fasten(app, caplog):
    from tests.test_ingest_resilience import _run_fasten_ingest

    _run_fasten_ingest(app, "fhi42-job", [_condition()], caplog)
    assert _row("test-tenant", "Condition", "src-c-1").source == "fasten"


def test_seeded_rows_are_tagged_sample_fresh_and_revived(app):
    from r6.seed import seed_demo_data

    assert seed_demo_data("test-tenant") > 0
    db.session.expire_all()
    rows = R6Resource.query.filter_by(tenant_id="test-tenant").all()
    assert rows and {r.source for r in rows} == {"sample"}

    # The revive branch goes through `_ingest_one`, and says `sample` too.
    patient = next(r for r in rows if r.resource_type == "Patient")
    patient.is_deleted = True
    patient.source = None
    db.session.commit()
    seed_demo_data("test-tenant")
    assert _row("test-tenant", "Patient", patient.id).source == "sample"


def test_a_caller_supplied_seed_bundle_is_untagged(app):
    """Only the built-in demo set is ours to call made up. A bundle a caller
    hands `/internal/seed` is content somebody else chose: no channel we can
    vouch for, so no tag — on the fresh row and on the revive.

    MUTATION: tag every seeded row `sample` regardless of `resources` -> red."""
    from r6.seed import seed_demo_data

    supplied = [_condition("seed-c-1")]
    assert seed_demo_data("test-tenant", resources=supplied) == 1
    row = _row("test-tenant", "Condition", "seed-c-1")
    assert row.source is None
    assert "tag" not in row.to_fhir_json()["meta"]

    row.is_deleted = True
    row.source = "upload"
    db.session.commit()
    seed_demo_data("test-tenant", resources=supplied)
    assert _row("test-tenant", "Condition", "seed-c-1").source is None


def test_a_seed_bundle_over_http_is_untagged(client, monkeypatch,
                                             tenant_headers):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "fhi42-seed-secret")
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": _condition("seed-c-2")}]}
    r = client.post("/r6/fhir/internal/seed",
                    data=json.dumps({"tenant_id": "test-tenant",
                                     "bundle": bundle}),
                    headers={"Content-Type": "application/json",
                             "X-Internal-Secret": "fhi42-seed-secret"})
    assert r.status_code == 201, r.get_data(as_text=True)
    assert _row("test-tenant", "Condition", "seed-c-2").source is None
    got = client.get("/r6/fhir/Condition/seed-c-2", headers=tenant_headers)
    assert "tag" not in got.get_json()["meta"]


def test_the_demo_history_cli_tags_its_rows_sample(app):
    """`flask seed-demo-history` loads our own synthetic BP history in
    process, a trusted code path, so it says `sample` explicitly.

    MUTATION: drop `source='sample'` from the CLI call -> red."""
    result = app.test_cli_runner().invoke(
        args=["seed-demo-history", "--tenant-id", "test-tenant"])
    assert result.exit_code == 0, result.output
    db.session.expire_all()
    rows = R6Resource.query.filter_by(tenant_id="test-tenant").all()
    assert rows and {r.source for r in rows} == {"sample"}


def test_context_builder_rows_stay_untagged(client, auth_headers):
    """`$ingest-context` builds an agent's context envelope; it is not a
    channel a person brought records through, so its rows carry no source."""
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": _condition("ctx-c-1")}]}
    r = client.post("/r6/fhir/Bundle/$ingest-context",
                    data=json.dumps(bundle),
                    content_type="application/json", headers=auth_headers)
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    assert _row("test-tenant", "Condition", "ctx-c-1").source is None


# --- re-ingest: the latest channel wins ------------------------------------

def test_an_upload_re_ingested_through_fasten_says_fasten(client,
                                                          tenant_headers):
    """The upsert already overwrites the content; the channel goes with it.

    MUTATION: drop `existing.source = source` in `_ingest_one` -> red."""
    from r6.fasten.ingester import _ingest_one

    _upload(client, [_condition()])
    assert _row("test-tenant", "Condition", "src-c-1").source == "upload"

    assert _ingest_one(_condition(), "test-tenant",
                       source="fasten")[0] == "ok"
    db.session.commit()

    assert _row("test-tenant", "Condition", "src-c-1").source == "fasten"
    got = client.get("/r6/fhir/Condition/src-c-1", headers=tenant_headers)
    assert got.get_json()["meta"]["tag"] == [_tag("fasten")]


# --- a rewrite outside ingest clears the tag ----------------------------------
# The tag never claims a channel we cannot vouch for. Once anything but an
# ingest rewrites the content, it is no longer what that channel delivered.

def test_a_put_over_an_ingested_row_clears_the_tag(client, auth_headers,
                                                   tenant_headers):
    """MUTATION: stop `update_resource` clearing `source` -> red."""
    from r6.fasten.ingester import _ingest_one

    patient = {"resourceType": "Patient", "id": "src-p-2",
               "name": [{"family": "Testerson", "given": ["A"]}]}
    assert _ingest_one(dict(patient), "test-tenant",
                       source="fasten")[0] == "ok"
    db.session.commit()

    r = client.put("/r6/fhir/Patient/src-p-2",
                   data=json.dumps(dict(patient, gender="female")),
                   content_type="application/json", headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "tag" not in r.get_json()["meta"]
    assert _row("test-tenant", "Patient", "src-p-2").source is None
    got = client.get("/r6/fhir/Patient/src-p-2", headers=tenant_headers)
    assert "tag" not in got.get_json()["meta"]

    # Re-ingest through a channel sets it again.
    assert _ingest_one(dict(patient), "test-tenant",
                       source="upload")[0] == "ok"
    db.session.commit()
    assert _row("test-tenant", "Patient", "src-p-2").source == "upload"


def test_the_demo_loop_rewrite_clears_the_tag(app):
    from r6.fasten.ingester import _ingest_one
    from r6.routes import _DEMO_LOOP_PATIENT_ID, _demo_loop_upsert

    patient = {"resourceType": "Patient", "id": _DEMO_LOOP_PATIENT_ID,
               "name": [{"family": "Demo", "given": ["D"]}]}
    _ingest_one(dict(patient), "test-tenant", source="sample")
    db.session.commit()

    _demo_loop_upsert(dict(patient, gender="other"), "test-tenant")
    db.session.commit()
    assert _row("test-tenant", "Patient",
                _DEMO_LOOP_PATIENT_ID).source is None


def test_a_curatr_fix_clears_the_tag(app):
    """The fix writes through a conditional UPDATE, not `update_resource`, so
    it clears the column itself.

    MUTATION: drop `source=None` from the curatr UPDATE -> red."""
    from r6.curatr import apply_fix
    from r6.fasten.ingester import _ingest_one

    record = {"resourceType": "Condition", "id": "src-c-fix",
              "subject": {"reference": "Patient/src-p-1"},
              "code": {"coding": [{"system": "http://snomed.info/sct",
                                   "code": "44054006"}]},
              "clinicalStatus": {"coding": [{"code": "active"}]}}
    _ingest_one(record, "test-tenant", source="fasten")
    db.session.commit()

    result = apply_fix(
        "Condition", "src-c-fix",
        [{"field_path": "Condition.clinicalStatus.coding[0].code",
          "new_value": "resolved"}],
        "fix it", "test-tenant", action_ref="act-fhi42")
    assert "error" not in result, result
    row = _row("test-tenant", "Condition", "src-c-fix")
    assert row.version_id == 2
    assert row.source is None
    assert "tag" not in row.to_fhir_json()["meta"]


# --- an upload cannot set or override the tag ---------------------------------

def test_an_upload_cannot_choose_its_own_source(client, tenant_headers,
                                                auth_headers):
    """The resource's own `meta` claims `fasten` in our system, and the
    request body carries `source` and `meta` beside the bundle. The stored
    channel is still `upload`, and the output carries exactly our one tag:
    no upstream `meta.source`, tag display or security label reaches read,
    search, or the share bundle."""
    _upload(client, [_condition(meta=_hostile_meta())],
            source="fasten", meta={"tag": [_tag("fasten")]})

    assert _row("test-tenant", "Condition", "src-c-1").source == "upload"

    read = client.get("/r6/fhir/Condition/src-c-1", headers=tenant_headers)
    assert read.status_code == 200
    assert read.get_json()["meta"]["tag"] == [_tag("upload")]
    assert "source" not in read.get_json()["meta"]
    assert "security" not in read.get_json()["meta"]

    search = client.get("/r6/fhir/Condition", headers=tenant_headers)
    assert search.status_code == 200
    entries = search.get_json()["entry"]
    assert [e["resource"]["meta"]["tag"] for e in entries] == [
        [_tag("upload")]]

    share = client.post("/r6/fhir/$share-bundle",
                        json={"resource_types": ["Condition"],
                              "profile": "deidentified"},
                        headers=auth_headers)
    assert share.status_code == 200, share.get_data(as_text=True)
    shared = share.get_json()["entry"][0]["resource"]
    ours = [t for t in shared["meta"]["tag"]
            if t.get("system") == RECORD_SOURCE_SYSTEM]
    assert ours == [_tag("upload")]

    for body in (read.get_data(as_text=True), search.get_data(as_text=True),
                 share.get_data(as_text=True)):
        for canary in (_CANARY_URI, _CANARY_TAG, _CANARY_SEC,
                       "upstream.example"):
            assert canary not in body


def test_a_direct_fhir_write_cannot_claim_a_source(client, auth_headers,
                                                   tenant_headers):
    """An agent writing through the FHIR API is not a channel. A `meta.tag`
    in our system in its body is dropped like the rest of a stored `meta`."""
    patient = {"resourceType": "Patient", "id": "src-p-9",
               "name": [{"family": "Testerson", "given": ["A"]}],
               "meta": {"tag": [_tag("fasten")], "source": _CANARY_URI}}
    r = client.post("/r6/fhir/Patient", data=json.dumps(patient),
                    content_type="application/json", headers=auth_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    rid = r.get_json()["id"]
    assert _row("test-tenant", "Patient", rid).source is None
    got = client.get(f"/r6/fhir/Patient/{rid}", headers=tenant_headers)
    assert "tag" not in got.get_json()["meta"]
    assert _CANARY_URI not in got.get_data(as_text=True)


# --- per tenant ---------------------------------------------------------------

def test_the_tag_is_per_tenant(client, tenant_headers, other_tenant_headers):
    """Same (type, id) in two tenants through two channels: each tenant
    reads its own channel, never the other's."""
    from r6.fasten.ingester import _ingest_one

    _upload(client, [_condition()])
    assert _ingest_one(_condition(), "other-tenant",
                       source="fasten")[0] == "ok"
    db.session.commit()

    mine = client.get("/r6/fhir/Condition/src-c-1", headers=tenant_headers)
    theirs = client.get("/r6/fhir/Condition/src-c-1",
                        headers=other_tenant_headers)
    assert mine.get_json()["meta"]["tag"] == [_tag("upload")]
    assert theirs.get_json()["meta"]["tag"] == [_tag("fasten")]

    search = client.get("/r6/fhir/Condition", headers=other_tenant_headers)
    tags = [e["resource"]["meta"]["tag"] for e in search.get_json()["entry"]]
    assert tags == [[_tag("fasten")]]


# --- an untagged row ----------------------------------------------------------

def test_a_row_with_no_source_emits_no_tag(client, tenant_headers):
    """A pre-migration row (NULL) emits no tag at all — never a default, and
    never a guess. There is no backfill.

    MUTATION: emit the tag unconditionally in `to_fhir_json` -> red."""
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_condition()),
                     resource_id="src-c-1", tenant_id="test-tenant")
    db.session.add(row)
    db.session.commit()

    assert "tag" not in row.to_fhir_json()["meta"]
    got = client.get("/r6/fhir/Condition/src-c-1", headers=tenant_headers)
    assert got.status_code == 200
    assert "tag" not in got.get_json()["meta"]


def test_the_tag_is_code_only(app):
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_condition()),
                     resource_id="src-c-1", tenant_id="test-tenant",
                     source="connected-app")
    assert row.to_fhir_json()["meta"]["tag"] == [
        {"system": RECORD_SOURCE_SYSTEM, "code": "connected-app"}]


# --- redaction ----------------------------------------------------------------

def test_the_tag_survives_redaction_and_redaction_still_strips(app):
    """The tag is ours, so redaction keeps it and adds no label to it; the
    rest of the resource is redacted exactly as before."""
    from r6.redaction import apply_patient_controlled_redaction, apply_redaction

    patient = {
        "resourceType": "Patient", "id": "src-p-1",
        "name": [{"family": "Kowalski", "given": ["Rosa"]}],
        "birthDate": "1971-03-02",
        "identifier": [{"system": "http://example.org/mrn",
                        "value": "MRN-CANARY-1"}],
        "telecom": [{"system": "phone", "value": "555-0100"}],
        "address": [{"line": ["1 Canary Way"], "city": "Springfield",
                     "state": "IL"}],
        "meta": _hostile_meta(),
    }
    row = R6Resource(resource_type="Patient",
                     resource_json=json.dumps(patient),
                     resource_id="src-p-1", tenant_id="test-tenant",
                     source="fasten")

    redacted = apply_redaction(row.to_fhir_json())
    assert redacted["meta"]["tag"] == [_tag("fasten")]
    assert redacted["name"] == [{"family": "K.", "given": ["R."]}]
    assert redacted["birthDate"] == "1971"
    assert "value" not in redacted["identifier"][0]
    assert redacted["telecom"][0]["value"] == "[Redacted]"
    assert "line" not in redacted["address"][0]
    assert "city" not in redacted["address"][0]
    text = json.dumps(redacted)
    for canary in ("Kowalski", "Rosa", "MRN-CANARY-1", "555-0100",
                   "Canary Way", _CANARY_URI, _CANARY_TAG, _CANARY_SEC):
        assert canary not in text

    shared = apply_patient_controlled_redaction(row.to_fhir_json(), "src-p-1")
    assert _tag("fasten") in shared["meta"]["tag"]
    assert {t["code"] for t in shared["meta"]["tag"]} == {
        "fasten", "ANONYED", "patient-controlled"}


def test_validate_accepts_a_resource_carrying_our_tag(client,
                                                     tenant_headers):
    """A client that reads a record and posts it back to `$validate` must
    not be refused over a tag in a system the validator does not know."""
    _upload(client, [_condition()])
    got = client.get("/r6/fhir/Condition/src-c-1", headers=tenant_headers)
    r = client.post("/r6/fhir/Condition/$validate",
                    data=json.dumps(got.get_json()),
                    content_type="application/json", headers=tenant_headers)
    assert r.status_code == 200, r.get_data(as_text=True)


# --- only our own stamping step emits the record-source system (#956 F1-F3) ---

def _ours(obj):
    """Every tag in our system, at any depth."""
    found = []
    if isinstance(obj, list):
        for item in obj:
            found += _ours(item)
    elif isinstance(obj, dict):
        if obj.get("system") == RECORD_SOURCE_SYSTEM:
            found.append(obj)
        for value in obj.values():
            found += _ours(value)
    return found


def _nested_hostile():
    """A resource whose own meta, a contained resource and a contained
    Bundle's entry all claim our system and carry a meta.source URI."""
    hostile_meta = {"source": _CANARY_URI,
                    "tag": [{"system": RECORD_SOURCE_SYSTEM,
                             "code": "fasten-dr-smith"},
                            {"system": "http://upstream.example/t",
                             "code": "keep-me"}]}
    return {
        "resourceType": "Condition", "id": "src-c-1",
        "subject": {"reference": "Patient/src-p-1"},
        "meta": hostile_meta,
        "contained": [
            {"resourceType": "Observation", "id": "o1", "status": "final",
             "meta": json.loads(json.dumps(hostile_meta))},
            {"resourceType": "Bundle", "id": "b1", "type": "collection",
             "entry": [{"resource": {
                 "resourceType": "Observation", "id": "o2",
                 "status": "final",
                 "meta": json.loads(json.dumps(hostile_meta))}}]},
        ],
    }


def test_to_fhir_json_strips_our_system_and_meta_source_at_every_depth(app):
    """Local exits that never redact (exports, labs, care gaps) still get
    exactly one record-source tag: ours, from the column.

    MUTATION: drop the nested strip in `to_fhir_json` -> red."""
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_nested_hostile()),
                     resource_id="src-c-1", tenant_id="test-tenant",
                     source="upload")
    out = row.to_fhir_json()
    assert _ours(out) == [_tag("upload")]
    assert out["meta"]["tag"] == [_tag("upload")]
    text = json.dumps(out)
    assert _CANARY_URI not in text and "fasten-dr-smith" not in text
    # Only our system is stripped: another system's nested tag stays.
    assert "keep-me" in text


def test_to_fhir_json_never_emits_an_off_list_column_value(app):
    """The read side does not trust the column: a value outside the set,
    however it got there, emits no tag.

    MUTATION: stamp any non-NULL column value -> red."""
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_condition()),
                     resource_id="src-c-1", tenant_id="test-tenant")
    row.__dict__["source"] = "Springfield Clinic"  # past the validator
    out = row.to_fhir_json()
    assert _ours(out) == []
    # Nor does the carrier redaction re-stamps from: an off-list value never
    # leaves the model, so no later stamp can be fed it.
    assert out.record_source is None


def test_stamp_refuses_anything_outside_the_set():
    """The last guard, in front of every stamp: a value outside the set, or
    not a string, writes nothing, even off a forged carrier.

    MUTATION: let `stamp` take any non-empty value -> red."""
    from r6.record_source import StoredResource, stamp
    from r6.redaction import apply_redaction

    for bad in ("Springfield Clinic", "fasten-dr-smith", 1, True, ["fasten"]):
        assert _ours(stamp({"resourceType": "Condition"}, bad)) == []
        forged = StoredResource({"resourceType": "Condition", "id": "x"})
        forged.record_source = bad
        assert _ours(apply_redaction(forged)) == []


def test_redaction_strips_our_system_and_meta_source_from_any_payload():
    """Upstream data reaches the agent only through redaction, so redaction
    removes every record-source tag and every meta.source, at every depth,
    top level included. A plain dict is never re-stamped.

    MUTATION: drop the walker's tag strip, or its meta.source pop -> red."""
    from r6.redaction import apply_patient_controlled_redaction, apply_redaction

    upstream = _nested_hostile()
    upstream["meta"]["tag"].append(_tag("fasten"))  # a valid-looking claim
    for out in (apply_redaction(upstream),
                apply_patient_controlled_redaction(upstream, "src-p-1")):
        assert _ours(out) == []
        text = json.dumps(out)
        assert _CANARY_URI not in text and "fasten-dr-smith" not in text


def test_the_stamp_survives_redaction_only_from_the_store(app):
    """Redaction re-stamps from the row the store read, never from JSON: a
    copy of a stored resource, made as a plain dict, carries no trust.

    MUTATION: re-stamp from the top-level JSON tag -> red.
    MUTATION: drop the re-stamp in either redaction function -> red."""
    from r6.redaction import apply_patient_controlled_redaction, apply_redaction

    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_nested_hostile()),
                     resource_id="src-c-1", tenant_id="test-tenant",
                     source="fasten")
    assert _ours(apply_redaction(row.to_fhir_json())) == [_tag("fasten")]
    shared = apply_patient_controlled_redaction(row.to_fhir_json(), "p")
    assert _ours(shared) == [_tag("fasten")]

    for copy in (dict(row.to_fhir_json()),
                 json.loads(json.dumps(row.to_fhir_json()))):
        assert _ours(apply_redaction(copy)) == []
        assert _ours(apply_patient_controlled_redaction(copy, "p")) == []


def test_the_intake_share_keeps_the_stamp(client, auth_headers):
    _upload(client, [_condition()])
    share = client.post("/r6/fhir/$share-bundle",
                        json={"resource_types": ["Condition"]},
                        headers=auth_headers)
    assert share.status_code == 200, share.get_data(as_text=True)
    assert _ours(share.get_json()) == [_tag("upload")]
