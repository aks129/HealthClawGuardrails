"""Documents: what the upload card claims, and what a read hands a tool (#226).

Two halves of one issue. A patient's connected records include documents
(visit notes, discharge summaries). They are stored, and no tool can open one.

**The upload card.** The refresh path already stopped counting documents and
says so (PR #561). The upload card on the same page did not: it rendered the
engine's `ingested` as "N records added", so uploading 5 Conditions and 12
DocumentReferences said "17 records added" and a refresh then said 5, with
nothing explaining the drop (the second comment on #226). The upload response
now carries `records_added` — what the patient can reach — and the same
`uncounted_note` sentence the poll sends, produced by one helper.

**The read a tool gets.** MCP `fhir_read`/`fhir_search` already accept
DocumentReference and return the standard redacted read. These tests pin what
that read hands over for a document with a patient's name planted in every
free-text slot: metadata only (status, type code, date, author reference) and
no attachment body — including an Attachment with no `contentType`, which
leaked until #782 taught r6/redaction.py to recognise Attachments by shape.
"""

from __future__ import annotations

import base64
import json


from tests.test_careagents import FakeClient, _login, _make_direct_conn
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture

PLANTED = "Zebediah Quarrington"
_BODY_B64 = base64.b64encode(
    f"Visit note for {PLANTED}".encode()).decode()


# --- the upload card ---------------------------------------------------------

def _bundle(conditions, documents):
    entries = [{"resource": {"resourceType": "Condition", "id": f"c-{i}"}}
               for i in range(conditions)]
    entries += [{"resource": {"resourceType": "DocumentReference",
                              "id": f"d-{i}", "status": "current"}}
                for i in range(documents)]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def _upload(cfg, svc, monkeypatch, bundle, result=None, tenant_documents=0):
    """Upload `bundle`; `tenant_documents` is the tenant's document total the
    engine reports afterwards (what `uncounted_record_count` answers)."""
    from careagents.app import create_app
    fake = FakeClient()
    fake.uncounted = tenant_documents
    if result is not None:
        fake.ingest_bundle_result = result
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(f"/api/connections/{conn_id}/upload",
               data=json.dumps(bundle),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def test_upload_of_conditions_and_documents_counts_only_the_readable(
        cfg, svc, monkeypatch):
    """MUTATION: report `ingested` as `records_added` -> red at 17.

    The issue's own measured shape: 5 Conditions + 12 DocumentReferences.
    """
    d = _upload(cfg, svc, monkeypatch, _bundle(5, 12), tenant_documents=12)
    assert d["ingested"] == 17          # the engine's fact, unchanged
    assert d["records_added"] == 5      # what the patient can reach
    assert d["uncounted_note"] == (
        "Notes and documents are not yet readable here.")


def test_upload_of_only_documents_says_they_arrived_unreadable(
        cfg, svc, monkeypatch):
    """MUTATION: drop the documents-only arm of the shared helper -> red.

    "0 records added" alone is indistinguishable from an upload that did
    nothing, for a person who just watched twelve notes go in.
    """
    d = _upload(cfg, svc, monkeypatch, _bundle(0, 12), tenant_documents=12)
    assert d["records_added"] == 0
    assert d["uncounted_note"] == (
        "Notes and documents arrived, and they are not readable here yet.")


def test_upload_without_documents_carries_no_clause(cfg, svc, monkeypatch):
    """MUTATION: emit the clause unconditionally -> red."""
    d = _upload(cfg, svc, monkeypatch, _bundle(5, 0))
    assert d["records_added"] == 5
    assert "uncounted_note" not in d, d


def test_upload_to_a_tenant_already_holding_notes_carries_the_caveat(
        cfg, svc, monkeypatch):
    """MUTATION: pass the upload's own document count as `uncounted` instead
    of the tenant total -> red.

    The poll carries the standing caveat whenever the tenant holds notes; the
    upload card must say the same thing, or the next refresh contradicts it.
    """
    d = _upload(cfg, svc, monkeypatch, _bundle(5, 0), tenant_documents=12)
    assert d["records_added"] == 5
    assert d["uncounted_note"] == (
        "Notes and documents are not yet readable here.")


def test_upload_hedges_when_the_document_total_cannot_be_read(
        cfg, svc, monkeypatch):
    """Unknown is never zero (#403): a failed probe is named, not hidden."""
    from careagents.app import create_app
    from careagents.healthclaw import HealthClawError
    fake = FakeClient()

    def down(_tenant):
        raise HealthClawError("search DocumentReference failed (503)", 503)
    fake.uncounted_record_count = down
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(f"/api/connections/{conn_id}/upload",
               data=json.dumps(_bundle(5, 0)),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200
    assert r.get_json()["uncounted_note"] == (
        "We could not check whether notes or documents were left out.")


def test_a_document_the_engine_refused_is_not_counted_as_arrived(
        cfg, svc, monkeypatch):
    """MUTATION: count DocumentReference entries without consulting the
    engine's per-index errors -> red (it would say documents arrived).

    Entry 1 is the only document and the engine failed it, so nothing
    unreadable landed and no clause is owed.
    """
    result = {"tenant_id": "ca-x", "entries": 2, "ingested": 1,
              "skipped": 0, "failed": 1,
              "errors": [{"index": 1, "code": "ingest_error",
                          "correlation_id": "c-1"}]}
    d = _upload(cfg, svc, monkeypatch, _bundle(1, 1), result=result)
    assert d["records_added"] == 1
    assert "uncounted_note" not in d, d


def test_upload_split_holds_against_the_real_engine(cfg, svc, monkeypatch):
    """Cross-layer: the fakes above prove the split given an engine answer;
    this proves the engine's real answer supports it — DocumentReference is
    an uploadable type, and a refused entry carries its `index`.

    MUTATION: have `_documents_landed` ignore `errors[]` -> red here too,
    because the document at index 2 is a real engine refusal.
    """
    import requests as _requests

    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db

    monkeypatch.setenv("PUBLIC_TENANTS", "test-tenant,ca-docs226")
    engine_app = engine_create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "LEGACY_BOOT_ON_CREATE": False})
    with engine_app.app_context():
        db.create_all()
    engine = engine_app.test_client()

    def _to_requests(resp):
        r = _requests.Response()
        r.status_code = resp.status_code
        r._content = resp.get_data() or b""
        r.headers.update(resp.headers.to_wsgi_list())
        return r

    class _Relay:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            return _to_requests(engine.post(url.replace("http://engine", ""),
                                            json=json, data=data,
                                            headers=headers or {}))

        def get(self, url, params=None, headers=None, timeout=None):
            return _to_requests(engine.get(url.replace("http://engine", ""),
                                           query_string=params or {},
                                           headers=headers or {}))

    real = HealthClawClient(base="http://engine", mint_secret="unused")
    real.http = _Relay()
    real.new_tenant_id = lambda: "ca-docs226"
    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Condition", "id": "c-1",
                      "subject": {"reference": "Patient/p-1"},
                      "code": {"coding": [{"system": "http://snomed.info/sct",
                                           "code": "38341003"}]}}},
        {"resource": {"resourceType": "DocumentReference", "id": "d-1",
                      "status": "current",
                      "subject": {"reference": "Patient/p-1"},
                      "content": [{"attachment": {
                          "contentType": "text/plain"}}]}},
        # A document the engine refuses (id outside the FHIR id charset):
        # it must not be counted as a document that arrived.
        {"resource": {"resourceType": "DocumentReference", "id": "bad id!",
                      "status": "current"}},
    ]}
    r = c.post(f"/api/connections/{conn_id}/upload", data=json.dumps(bundle),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert (d["ingested"], d["skipped"]) == (2, 1), d
    assert d["records_added"] == 1
    assert d["uncounted_note"] == (
        "Notes and documents are not yet readable here.")


def test_the_poll_and_the_upload_share_one_sentence_helper():
    """MUTATION: inline a copy of the sentence in the upload route -> red.

    The second comment on #226 asks for exactly this: one helper producing
    the sentence, called from both, so the two counters cannot drift again.
    Source-level, like the other copy fences in tests/test_careagents.py.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "careagents" / "app.py").read_text()
    for sentence in ("Notes and documents are not yet readable here.",
                     "Notes and documents arrived, and they are not ",
                     "We could not check whether notes or documents "):
        assert src.count(sentence) == 1, sentence
    assert src.count("_uncounted_note(") == 3   # one def, two callers


def test_home_js_renders_the_upload_with_the_refresh_wording():
    """MUTATION: have the upload card render `d.ingested` again -> red.

    Source-level only; there is no JS harness in this repo.
    """
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1]
          / "careagents" / "static" / "home.js").read_text()
    assert "d.records_added" in js
    assert "`${ing} record${ing === 1" not in js
    # One sentence builder for both counters.
    assert js.count("readableCountLine(") == 3   # one def, two callers
    # The lead is a whole sentence now, so the upload parts are joined as
    # sentences; " · " after a full stop rendered "added. · 3 not saved".
    assert 'parts.join(" · ")' not in js
    assert 'parts.join(" ")' in js


# --- the read a tool gets ----------------------------------------------------

def _document(attachment):
    return {
        "resourceType": "DocumentReference",
        "status": "current",
        "description": f"Discharge summary for {PLANTED}",
        "type": {"coding": [{"system": "http://loinc.org",
                             "code": "18842-5", "display": PLANTED}],
                 "text": PLANTED},
        "subject": {"reference": "Patient/p-226", "display": PLANTED},
        "author": [{"reference": "Practitioner/pr-226", "display": PLANTED}],
        "date": "2025-01-02T10:00:00Z",
        "content": [{"attachment": attachment}],
    }


def _store_and_read(client, auth_headers, tenant_id, attachment):
    headers = dict(auth_headers)
    headers["X-Human-Confirmed"] = "true"
    created = client.post("/r6/fhir/DocumentReference",
                          json=_document(attachment), headers=headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    rid = created.get_json()["id"]
    read = client.get(f"/r6/fhir/DocumentReference/{rid}",
                      headers={"X-Tenant-Id": tenant_id})
    search = client.get("/r6/fhir/DocumentReference",
                        headers={"X-Tenant-Id": tenant_id})
    assert read.status_code == 200 and search.status_code == 200
    return read, search


def test_a_document_reaches_a_tool_as_metadata_only(
        client, auth_headers, tenant_id):
    """MUTATION: stop popping `title` (or `data`, or `url`) in the attachment
    branch of r6/redaction.py::_redact_recursive -> red.

    The name is planted in every free-text slot a real feed uses. What the
    read and the search hand to fhir_read/fhir_search is the document's
    metadata; the body, its link and its title never leave the engine.
    """
    read, search = _store_and_read(client, auth_headers, tenant_id, {
        "contentType": "text/plain", "data": _BODY_B64,
        "url": f"https://ehr.example/notes?who={PLANTED}",
        "title": f"Note for {PLANTED}"})
    for resp in (read, search):
        body = resp.get_data(as_text=True)
        assert "Zebediah" not in body
        assert _BODY_B64 not in body
        assert "ehr.example" not in body
    doc = read.get_json()
    assert doc["content"] == [{"attachment": {"contentType": "text/plain"}}]
    # The metadata a tool can list survives.
    assert doc["status"] == "current"
    assert doc["type"]["coding"][0]["code"] == "18842-5"
    assert doc["author"] == [{"reference": "Practitioner/pr-226"}]
    assert doc["date"].startswith("2025-01-02")


def test_an_attachment_without_content_type_is_stripped_too(
        client, auth_headers, tenant_id):
    read, search = _store_and_read(client, auth_headers, tenant_id, {
        "data": _BODY_B64, "title": f"Note for {PLANTED}"})
    for resp in (read, search):
        body = resp.get_data(as_text=True)
        assert "Zebediah" not in body
        assert _BODY_B64 not in body
