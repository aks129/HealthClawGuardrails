"""Regression tests for POST /r6/fhir/internal/ingest-bundle (#227).

The file-upload / SHL import path lives behind this endpoint. It reuses
`_ingest_one` (same code path Fasten/SHC take, with parameterized
provenance so the audit event honestly says `direct-upload`), runs
synchronously so a patient watching an upload sees an honest per-entry
result, and rides the same fail-closed mint-secret gate as
`internal/seed` and `internal/purge-tenant`.

Coverage matrix for the release contract (see thread for the full spec):

  Tenant / auth:
    - Header is the ONLY tenant selector; body `tenant_id` is a legacy
      selector that returns 400 `legacy_body_selector`.
    - Non-public tenant without `X-Internal-Secret` → 403.
    - Non-public tenant WITH secret → 200 (mint gate passes).
  Limits:
    - `Content-Length` over cap → 413 (no body read).
    - No `Content-Length` (chunked or absent) + oversized stream → 413
      via `stream.read(max_bytes + 1)`; the stream is never allowed to
      spend unbounded memory.
    - `entry` count over cap → 400 `too_many_entries`.
    - env defaults apply when `INGEST_BUNDLE_MAX_*` are missing / junk.
  MIME:
    - Only `application/json` is accepted for the ENVELOPE (the body is
      `{bundle: {...}}`, not a raw FHIR resource). `fhir+json` on the
      envelope is a mislabel and would confuse the accept-list.
    - Charset parameters (`application/json; charset=utf-8`) accepted.
    - `text/plain` / missing Content-Type → 415.
  Fail-loud per-entry:
    - Unsupported resource type → per-entry `skipped` + `errors[]` code.
    - `entry.resource` not a JSON object → per-entry `failed` + code.
    - Per-entry ingest exception → per-entry `failed`, response carries
      an opaque `correlation_id`, exception text NEVER surfaces to the
      caller.
  Atomicity:
    - A mid-Bundle DB exception rolls back only the failing row via
      savepoint; earlier successful rows remain readable and `ingested`
      matches persisted rows.
  Provenance:
    - AuditEvent records `agent_id=direct-upload` for the upload path,
      NOT the default `fasten-connect` — direct uploads must not borrow
      Fasten's identity.
  Audit outcome:
    - `partial` when either `failed` OR `skipped` is non-zero.

The CareAgents-side ownership, streaming cap, MIME accept-list, and UI
result surface are covered in `tests/test_careagents.py`.
"""

from __future__ import annotations

import json

import pytest

from r6.models import R6Resource, AuditEventRecord

_ENDPOINT = "/r6/fhir/internal/ingest-bundle"


def _bundle(resources):
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": r} for r in resources]}


def _patient(pid="p-1", family="Testerson"):
    return {"resourceType": "Patient", "id": pid,
            "name": [{"family": family, "given": ["A"]}]}


def _condition(cid="c-1"):
    return {"resourceType": "Condition", "id": cid,
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                                 "code": "E11.9",
                                 "display": "Type 2 diabetes mellitus"}]}}


def _post(client, body, tenant="test-tenant",
          content_type="application/json"):
    return client.post(_ENDPOINT, data=json.dumps(body),
                       headers={"X-Tenant-Id": tenant,
                                "Content-Type": content_type})


# --- happy path --------------------------------------------------------------

def test_valid_bundle_ingests_and_is_readable_back(client, app,
                                                    tenant_headers):
    r = _post(client, {"bundle": _bundle([_patient(), _condition()])})
    assert r.status_code == 200, r.get_data(as_text=True)
    result = r.get_json()
    assert result["tenant_id"] == "test-tenant"
    assert result["entries"] == 2
    assert result["ingested"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["errors"] == []
    assert result["correlation_id"]  # always present, PHI-safe handle

    # Resources are actually written — reads against the same tenant see
    # them, so this is a real ingest not an accounting-only OK.
    got = client.get("/r6/fhir/Patient/p-1", headers=tenant_headers)
    assert got.status_code == 200
    got = client.get("/r6/fhir/Condition/c-1", headers=tenant_headers)
    assert got.status_code == 200


def test_charset_parameter_on_content_type_is_accepted(client):
    r = _post(client, {"bundle": _bundle([_patient("c-1")])},
              content_type="application/json; charset=utf-8")
    assert r.status_code == 200
    assert r.get_json()["ingested"] == 1


# --- tenant selector contract ------------------------------------------------

def test_body_tenant_id_is_rejected_as_legacy_selector(client):
    # The header is the ONLY authoritative tenant. A body `tenant_id`
    # even matching the header is refused so a request-shaping bug or
    # attacker-controlled body cannot become a cross-tenant write oracle.
    r = _post(client, {"tenant_id": "test-tenant",
                       "bundle": _bundle([_patient()])})
    assert r.status_code == 400
    assert r.get_json()["error"] == "legacy_body_selector"


def test_missing_tenant_header_returns_400(client):
    r = client.post(_ENDPOINT,
                    data=json.dumps({"bundle": _bundle([_patient()])}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_invalid_tenant_format_returns_400(client):
    r = client.post(_ENDPOINT,
                    data=json.dumps({"bundle": _bundle([_patient()])}),
                    headers={"X-Tenant-Id": "bad tenant!!",
                             "Content-Type": "application/json"})
    assert r.status_code == 400


# --- MIME accept-list --------------------------------------------------------

def test_missing_content_type_returns_415(client):
    r = client.post(_ENDPOINT,
                    data=json.dumps({"bundle": _bundle([_patient()])}),
                    headers={"X-Tenant-Id": "test-tenant"})
    assert r.status_code == 415
    assert r.get_json()["error"] == "content_type_required"


def test_fhir_plus_json_on_envelope_is_rejected(client):
    # `application/fhir+json` labels a raw FHIR resource; the envelope
    # body `{bundle: ...}` is not one. The patient-facing CareAgents
    # route accepts fhir+json for the raw Bundle from the browser; this
    # internal endpoint takes the wrapped call, so fhir+json here would
    # be a mislabel and is refused.
    r = _post(client, {"bundle": _bundle([_patient()])},
              content_type="application/fhir+json")
    assert r.status_code == 415


def test_text_plain_body_returns_415(client):
    r = client.post(_ENDPOINT, data="hello",
                    headers={"X-Tenant-Id": "test-tenant",
                             "Content-Type": "text/plain"})
    assert r.status_code == 415


# --- body shape --------------------------------------------------------------

def test_malformed_json_body_returns_400(client):
    r = client.post(_ENDPOINT, data="{not json",
                    headers={"X-Tenant-Id": "test-tenant",
                             "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_json"


def test_non_bundle_body_returns_400_not_a_bundle(client):
    r = _post(client, {"bundle": {"resourceType": "Patient",
                                   "id": "not-a-bundle"}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_a_bundle"


def test_missing_bundle_returns_400_not_a_bundle(client):
    r = _post(client, {})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_a_bundle"


# --- fail-loud per-entry -----------------------------------------------------

def test_unsupported_resource_type_is_skipped_with_reason(client):
    r = _post(client, {"bundle": _bundle([
        {"resourceType": "GarbageType", "id": "g-1"}])})
    assert r.status_code == 200
    result = r.get_json()
    assert result["ingested"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["errors"], "unsupported types must appear in errors[]"
    err = result["errors"][0]
    assert err["index"] == 0
    assert err["code"] == "unsupported_resource_type"
    assert "GarbageType" in err["message"]


def test_partial_bundle_reports_per_entry(client):
    r = _post(client, {"bundle": _bundle([
        _patient("mix-1"),
        {"resourceType": "GarbageType"},
        _condition("mix-c-1")])})
    assert r.status_code == 200
    result = r.get_json()
    assert result["ingested"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert 1 in [e["index"] for e in result["errors"]]


def test_entry_without_a_resource_object_fails_only_that_entry(client):
    body = {"bundle": {
        "resourceType": "Bundle", "type": "collection",
        "entry": [{"resource": _patient("ok-1")},
                  {"resource": "not-an-object"},
                  {"resource": _patient("ok-2")}]}}
    r = _post(client, body)
    assert r.status_code == 200
    result = r.get_json()
    assert result["ingested"] == 2
    assert result["failed"] == 1
    assert "invalid_entry" in [e["code"] for e in result["errors"]]


def test_flat_entry_without_resource_wrapper_is_rejected(client):
    # FHIR entries carry the resource under `entry.resource`. We used to
    # accept a "flat" entry as a convenience; that was removed because
    # it widens the surface for typos on the request side. A bare
    # entry (a Patient at the entry level) fails with `invalid_entry`.
    body = {"bundle": {"resourceType": "Bundle", "type": "collection",
                       "entry": [_patient("flat-1")]}}
    r = _post(client, body)
    assert r.status_code == 200
    result = r.get_json()
    assert result["ingested"] == 0
    assert result["failed"] == 1
    assert result["errors"][0]["code"] == "invalid_entry"


# --- caps: byte + entry ------------------------------------------------------

def test_content_length_over_cap_rejects_without_reading_body(client,
                                                              monkeypatch):
    monkeypatch.setenv("INGEST_BUNDLE_MAX_BYTES", "1024")
    payload = json.dumps({"bundle": _bundle(
        [_patient(f"p-{i}", "X" * 20) for i in range(50)])})
    assert len(payload) > 1024  # sanity
    r = client.post(_ENDPOINT, data=payload,
                    headers={"X-Tenant-Id": "test-tenant",
                             "Content-Type": "application/json",
                             "Content-Length": str(len(payload))})
    assert r.status_code == 413
    assert r.get_json()["error"] == "payload_too_large"


def test_streaming_cap_catches_oversized_body_when_content_length_absent(
        app, monkeypatch):
    # `Content-Length` is not a memory bound — chunked or length-absent
    # requests can still be arbitrarily large. `wsgi.input_terminated`
    # tells werkzeug the stream is raw (not `LimitedStream`), matching
    # what a chunked / Content-Length-absent request looks like at the
    # WSGI layer. The endpoint's `stream.read(max_bytes+1)` must catch
    # the overrun and refuse with 413.
    import io as _io
    monkeypatch.setenv("INGEST_BUNDLE_MAX_BYTES", "512")
    payload = ("{" + " " * 2000 + "}").encode("utf-8")  # >512 bytes
    with app.test_request_context(
            _ENDPOINT, method="POST",
            input_stream=_io.BytesIO(payload),
            headers={"X-Tenant-Id": "test-tenant",
                     "Content-Type": "application/json"},
            environ_overrides={"wsgi.input_terminated": True,
                               "CONTENT_LENGTH": ""}):
        from r6.routes import _read_body_with_hard_cap
        raw, err = _read_body_with_hard_cap(512)
        assert raw is None, "streaming read must refuse before returning body"
        assert err is not None
        body, status = err
        assert status == 413
        assert body["error"] == "payload_too_large"


def test_entry_count_over_cap_rejects(client, monkeypatch):
    monkeypatch.setenv("INGEST_BUNDLE_MAX_ENTRIES", "3")
    body = {"bundle": _bundle([_patient(f"cap-{i}") for i in range(4)])}
    r = _post(client, body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "too_many_entries"


def test_defaults_apply_when_env_missing_or_junk(client, monkeypatch):
    monkeypatch.setenv("INGEST_BUNDLE_MAX_BYTES", "not-a-number")
    monkeypatch.setenv("INGEST_BUNDLE_MAX_ENTRIES", "-42")
    r = _post(client, {"bundle": _bundle([_patient("d-1")])})
    assert r.status_code == 200
    assert r.get_json()["ingested"] == 1


# --- fail-closed mint gate ---------------------------------------------------

def test_non_public_tenant_without_secret_is_forbidden(client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "s3cret")
    r = client.post(_ENDPOINT,
                    data=json.dumps({"bundle": _bundle([_patient()])}),
                    headers={"X-Tenant-Id": "someones-real-tenant",
                             "Content-Type": "application/json"})
    assert r.status_code == 403


def test_non_public_tenant_with_secret_is_allowed(client, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", "s3cret")
    r = client.post(_ENDPOINT,
                    data=json.dumps({"bundle": _bundle([_patient("gated-1")])}),
                    headers={"X-Tenant-Id": "someones-real-tenant",
                             "Content-Type": "application/json",
                             "X-Internal-Secret": "s3cret"})
    assert r.status_code == 200
    assert r.get_json()["ingested"] == 1


# --- provenance: direct-upload audit event -----------------------------------

def test_ingest_records_direct_upload_provenance_not_fasten(client, app):
    _post(client, {"bundle": _bundle([_patient("prov-1")])})
    with app.app_context():
        # The per-resource AuditEventRecord for the create must attribute
        # to `direct-upload`, not the `_ingest_one` default `fasten-connect`.
        rows = AuditEventRecord.query.filter_by(
            tenant_id="test-tenant", resource_type="Patient",
            resource_id="prov-1").all()
        assert rows, "an AuditEventRecord for the direct upload must exist"
        assert any(r.agent_id == "direct-upload" for r in rows), (
            "direct-upload path must not borrow fasten-connect provenance; "
            f"got agent_ids={[r.agent_id for r in rows]}")


def test_bundle_summary_audit_outcome_is_partial_on_skipped_or_failed(
        client, app):
    # A skipped entry (unsupported type) is a signal too, not just a hard
    # failure — `partial` covers both. Previously `partial` only fired on
    # failed>0, hiding skipped rows behind a `success` label.
    r = _post(client, {"bundle": _bundle([
        _patient("sum-1"),
        {"resourceType": "GarbageType"}])})
    assert r.status_code == 200
    with app.app_context():
        # The endpoint records ONE `ingest_bundle` summary event per call.
        summaries = AuditEventRecord.query.filter_by(
            tenant_id="test-tenant", event_type="ingest_bundle",
            agent_id="direct-upload").all()
        assert summaries, "one summary AuditEventRecord is required"
        assert any(s.outcome == "partial" for s in summaries)


# --- PHI-safe errors: no exception text surfaces -----------------------------

def test_per_entry_exception_never_leaks_str_exc_to_caller(client,
                                                            monkeypatch):
    # Force `_ingest_one` to raise an exception whose message quotes a
    # (fake) SQL statement carrying PHI-shaped tokens. The response MUST
    # NOT echo the message; it MUST carry a stable code + correlation_id.
    from r6 import routes as r6_routes

    class _BadExc(RuntimeError):
        pass

    calls = {"n": 0}

    def _boom(resource, tenant_id, agent_id=None, detail=None):
        calls["n"] += 1
        raise _BadExc("INSERT INTO x VALUES ('SSN=123-45-6789','john@a.com')")

    monkeypatch.setattr('r6.fasten.ingester._ingest_one', _boom)

    body = {"bundle": _bundle([_patient("phi-1"), _patient("phi-2")])}
    r = client.post(_ENDPOINT, data=json.dumps(body),
                    headers={"X-Tenant-Id": "test-tenant",
                             "Content-Type": "application/json"})
    assert r.status_code == 200  # partial per-entry, not a batch fault
    result = r.get_json()
    assert result["failed"] == 2
    assert calls["n"] == 2
    for err in result["errors"]:
        assert err["code"] == "ingest_error"
        assert "correlation_id" in err
        # The exception text must not appear anywhere in the response.
        blob = json.dumps(err)
        assert "INSERT" not in blob
        assert "SSN=" not in blob
        assert "@a.com" not in blob


# --- atomicity: savepoint isolates a mid-Bundle failure ---------------------

def test_savepoint_isolates_a_mid_bundle_failure(client, app, monkeypatch):
    # A Bundle with three entries: the middle one is forced to raise
    # inside `_ingest_one`. Per-entry SAVEPOINT means:
    #   - the first entry stays committed and readable
    #   - the middle entry rolls back, is `failed`, is NOT readable
    #   - the third entry stays committed and readable
    # `ingested` in the response reflects only rows the caller can read.
    from r6.fasten import ingester as ingester_mod
    real = ingester_mod._ingest_one

    def _sometimes_boom(resource, tenant_id, agent_id='fasten-connect',
                        detail='Ingested via Fasten EHI export'):
        if resource.get('id') == 'atom-boom':
            raise RuntimeError('driver blew up on this row')
        return real(resource, tenant_id, agent_id=agent_id, detail=detail)

    monkeypatch.setattr('r6.fasten.ingester._ingest_one', _sometimes_boom)

    body = {"bundle": _bundle([
        _patient('atom-ok-1'),
        _patient('atom-boom'),
        _patient('atom-ok-2')])}
    r = _post(client, body)
    assert r.status_code == 200
    result = r.get_json()
    assert result["ingested"] == 2
    assert result["failed"] == 1

    with app.app_context():
        alive_ids = {row.id for row in R6Resource.query.filter_by(
            tenant_id="test-tenant", resource_type="Patient").all()}
        # Only the successful rows exist — the failed row was rolled
        # back by its savepoint and cannot be read.
        assert "atom-ok-1" in alive_ids
        assert "atom-ok-2" in alive_ids
        assert "atom-boom" not in alive_ids


# --- idempotency: same (tenant, type, id) upserts ----------------------------

def test_reingest_of_same_id_updates_rather_than_duplicates(client, app):
    _post(client, {"bundle": _bundle([_patient("dup-1", family="First")])})
    _post(client, {"bundle": _bundle([_patient("dup-1", family="Second")])})
    with app.app_context():
        rows = R6Resource.query.filter_by(
            tenant_id="test-tenant", resource_type="Patient",
            id="dup-1").all()
        assert len(rows) == 1  # upsert on (tenant, type, id)
