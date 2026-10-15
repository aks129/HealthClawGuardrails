"""One rule for what counts as a record, on the upload line and the card (#948).

An upload of 1 Patient and 3 Observations said "4 new records added" while the
records card under it said "3 records". The card is `record_count`, which sums
`HealthClawClient.COUNTED_TYPES` — the health records a person can ask about.
The upload line subtracted documents from the engine's `ingested` and nothing
else, so the Patient resource, which is the person and not a record about
them, was counted in one place and not the other.

The rule both now use: an upload adds the landed entries whose type is in
`COUNTED_TYPES`. These tests pin the two numbers to each other, once against a
fake engine and once against the real one, since a fake can only prove the
number is computed, not that the engine's own count agrees with it.

Synthetic data only.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from careagents.hub import count_line
from careagents.models import Connection
from tests.test_careagents import FakeClient, _login, _make_direct_conn
from tests.test_careagents import cfg as _cfg_fixture
from tests.test_careagents import svc as _svc_fixture

cfg = _cfg_fixture
svc = _svc_fixture


def _patient_and_observations(n):
    entries = [{"resource": {"resourceType": "Patient", "id": "p-1",
                             "name": [{"family": "Testperson"}]}}]
    entries += [{"resource": {
        "resourceType": "Observation", "id": f"o-{i}", "status": "final",
        "subject": {"reference": "Patient/p-1"},
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "4548-4"}]},
        "effectiveDateTime": f"2025-0{i + 1}-01",
        "valueQuantity": {"value": 7 + i, "unit": "%"}}}
        for i in range(n)]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def _card_count(svc, conn_id):
    with svc.session() as s:
        return s.query(Connection).filter_by(id=conn_id).one().last_count


def test_the_upload_line_and_the_card_count_the_same_records(
        cfg, svc, monkeypatch):
    """MUTATION: count every landed entry that is not a document (the old
    rule) -> red at 4 against the card's 3."""
    from careagents.app import create_app
    fake = FakeClient()
    fake.counted = 3          # what record_count answers: the Observations
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    r = c.post(f"/api/connections/{conn_id}/upload",
               data=json.dumps(_patient_and_observations(3)),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["ingested"] == 4            # the engine's fact, unchanged
    assert d["records_added"] == 3       # the upload line
    assert _card_count(svc, conn_id) == 3  # the card
    assert count_line(_card_count(svc, conn_id)) == "3 records"


def _upload(cfg, svc, monkeypatch, bundle, counted):
    from careagents.app import create_app
    fake = FakeClient()
    fake.counted = counted
    app = create_app(config=cfg, client=fake, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)
    r = c.post(f"/api/connections/{conn_id}/upload",
               data=json.dumps(bundle),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json(), conn_id



def test_a_patient_only_upload_adds_no_records_and_says_why(
        cfg, svc, monkeypatch):
    """The person themself is not a health record, so an upload of the
    Patient alone adds none. A bare "No new records added." read as a failed
    upload (QA on #953), so the line says what the file held, and the hub
    keeps it on screen: nothing counted landed, so nothing reloads.

    MUTATION: drop the reason -> red."""
    d, conn_id = _upload(cfg, svc, monkeypatch,
                         _patient_and_observations(0), counted=0)
    assert d["records_added"] == 0
    assert d["documents_added"] == 0
    assert _card_count(svc, conn_id) == 0
    assert d["uncounted_note"] == (
        "This file had personal details but no health records.")
    got = _home(d)
    assert got["line"] == (
        "No new records added. This file had personal details but no "
        "health records.")
    assert got["reloads"] is False


def test_an_upload_of_other_kinds_only_says_why(cfg, svc, monkeypatch):
    """Landed, and nothing the card counts: say so, without calling an
    Encounter "personal details"."""
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Encounter", "id": "e-1",
                      "status": "finished"}}]}
    d, _ = _upload(cfg, svc, monkeypatch, bundle, counted=0)
    assert d["records_added"] == 0
    assert d["uncounted_note"] == (
        "This file had no health records of a kind shown here yet.")
    assert _home(d) == {
        "line": ("No new records added. This file had no health records "
                 "of a kind shown here yet."),
        "reloads": False}


def test_an_upload_with_records_reloads_and_carries_its_line(
        cfg, svc, monkeypatch):
    d, _ = _upload(cfg, svc, monkeypatch, _patient_and_observations(3),
                   counted=3)
    assert d["records_added"] == 3
    assert "uncounted_note" not in d, d
    assert _home(d) == {"line": "3 new records added.", "reloads": True}


def test_a_documents_only_upload_keeps_the_document_wording():
    """"You can read" is true here, and only here: documents arrived."""
    d = {"ingested": 2, "records_added": 0, "documents_added": 2,
         "uncounted_note": ("Notes and documents arrived, and they are not "
                            "readable here yet.")}
    assert _home(d) == {
        "line": ("No new records you can read. Notes and documents arrived, "
                 "and they are not readable here yet."),
        "reloads": False}


@pytest.mark.parametrize("d, reloads", [
    ({"ingested": 4, "records_added": 3}, True),
    ({"ingested": 1, "records_added": 1}, True),
    # The QA case: landed, nothing counted. It used to reload on ingested.
    ({"ingested": 1, "records_added": 0}, False),
    ({"ingested": 12, "records_added": 0, "documents_added": 12}, False),
    ({"ingested": 0, "records_added": 0}, False),
    # A body without the count is not evidence of records.
    ({"ingested": 4}, False),
])
def test_the_reload_decision_follows_the_record_count(d, reloads):
    """MUTATION: decide on `ingested > 0` again -> red on the QA case."""
    assert _home(d)["reloads"] is reloads


def test_the_upload_handler_reloads_only_through_the_decision():
    """The upload path must decide through uploadReloads and carry its line
    across the reload; no other reload may sit in it. Since #860 the
    decision is made in sendFile and the change handler acts on it."""
    src = _HOME_JS.read_text()
    a = src.index("async function sendFile(")
    b = src.index("// --- what the last action did", a)
    path = src[a:b]
    assert "reloads: uploadReloads(d)" in path
    assert "if (out.reloads) return carryNotice(out.line);" in path
    assert "location.reload" not in path
    assert "ing > 0" not in path


_HOME_JS = (pathlib.Path(__file__).resolve().parents[1]
            / "careagents" / "static" / "home.js")

# readableCountLine and uploadReloads, cut out of home.js by name and run
# under node, so the test cannot drift from what ships.
_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function cut(name) {
  const a = src.indexOf('function ' + name + '(');
  const b = src.indexOf('\n  }\n', a);
  if (a < 0 || b < 0) throw new Error(name + ' not found');
  return src.slice(a, b + 4);
}
const { readableCountLine, uploadReloads } = new Function(
  cut('readableCountLine') + cut('uploadReloads')
  + 'return { readableCountLine, uploadReloads };')();
const d = JSON.parse(process.argv[2]);
const readable = typeof d.records_added === 'number'
  ? d.records_added : (d.ingested | 0);
process.stdout.write(JSON.stringify({
  line: readableCountLine(readable, d.uncounted_note, d.documents_added),
  reloads: uploadReloads(d),
}));
"""


def _home(d):
    """The upload line and the reload decision, as home.js makes them for
    the server's answer `d`."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", _HARNESS, "--", str(_HOME_JS),
                          json.dumps(d)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_two_counts_agree_against_the_real_engine(cfg, svc, monkeypatch):
    """Cross-layer: the real engine's `record_count`, which the card shows,
    and the upload's `records_added` come out equal for the issue's shape.

    MUTATION: the old rule (`landed - documents`) -> red, 4 against 3.
    """
    import requests as _requests

    from careagents.app import create_app
    from careagents.healthclaw import HealthClawClient
    from main import create_app as engine_create_app
    from models import db

    monkeypatch.setenv("PUBLIC_TENANTS", "test-tenant,ca-count948")
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
    real.new_tenant_id = lambda: "ca-count948"
    app = create_app(config=cfg, client=real, accounts=svc)
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c, svc, monkeypatch)
    conn_id = _make_direct_conn(c)

    r = c.post(f"/api/connections/{conn_id}/upload",
               data=json.dumps(_patient_and_observations(3)),
               headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["ingested"] == 4, d
    assert _card_count(svc, conn_id) == 3
    assert d["records_added"] == _card_count(svc, conn_id)
