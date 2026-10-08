"""Security review of `GET /AppointmentBrief?voice=sample` (PR #908).

V2: the voice never skips or changes the audit, and a broken audit still
blocks the read. V6: the voice never opens another tenant. Injection: the
value is never echoed. And the open question this file pins: the engine
honours `voice=sample` on ANY tenant, including one whose records were
ingested rather than seeded, so a caller can word a person's own urgent
result as "the sample person's". Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from r6.models import AuditEventRecord
from tests.test_brief_routes import _URL, _store


def _audit_rows(app, tenant_id):
    with app.app_context():
        return [(r.event_type, r.resource_type, r.resource_id, r.detail)
                for r in AuditEventRecord.query.filter_by(
                    tenant_id=tenant_id).order_by(AuditEventRecord.id)]


def _strings(body):
    out = []
    for section in body.get("extension", []):
        for e in section.get("extension", []):
            if e.get("url") == "field":
                f = json.loads(e["valueString"])
                out += [f["label"], f["value"]]
            elif "valueString" in e:
                out.append(e["valueString"])
    return out


def _ingested_creatinine_rise(app, tenant_id):
    """A tenant as an import leaves it: no seed ids, no seed audit rows."""
    now = datetime.now(timezone.utc)
    _store(app, {"resourceType": "Patient", "id": "imported-p1",
                 "birthDate": "1970-01-01", "gender": "female"}, tenant_id)
    for n, (days_ago, value) in enumerate(((6, 0.8), (0, 1.3)), start=1):
        _store(app, {
            "resourceType": "Observation", "id": f"imported-cr-{n}",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "2160-0"}]},
            "subject": {"reference": "Patient/imported-p1"},
            "valueQuantity": {"value": value, "unit": "mg/dL",
                              "system": "http://unitsofmeasure.org",
                              "code": "mg/dL"},
            "effectiveDateTime": (now - timedelta(days=days_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        }, tenant_id)


# --- V2 ----------------------------------------------------------------------

def test_voice_sample_writes_the_same_audit_row(app, client, tenant_id,
                                                tenant_headers):
    _ingested_creatinine_rise(app, tenant_id)
    before = len(_audit_rows(app, tenant_id))
    assert client.get(_URL, headers=tenant_headers).status_code == 200
    plain = _audit_rows(app, tenant_id)[before:]
    mid = len(_audit_rows(app, tenant_id))
    assert client.get(_URL + "?voice=sample",
                      headers=tenant_headers).status_code == 200
    voiced = _audit_rows(app, tenant_id)[mid:]
    assert plain and voiced == plain
    assert all("sample" not in str(d or "") for *_x, d in voiced)


def test_broken_audit_blocks_the_voiced_brief(app, client, tenant_id,
                                              tenant_headers, monkeypatch):
    _ingested_creatinine_rise(app, tenant_id)

    def boom(*a, **k):
        raise RuntimeError("simulated audit insert failure")
    monkeypatch.setattr("r6.audit._new_audit_event", boom)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    r = client.get(_URL + "?voice=sample", headers=tenant_headers)
    assert r.status_code >= 500
    assert "creatinine" not in r.get_data(as_text=True)


# --- V6 ----------------------------------------------------------------------

def test_voice_does_not_open_another_tenant(app, client, tenant_id,
                                            monkeypatch):
    """A non-public tenant without its token is refused with or without the
    voice (read auth on, as production requires)."""
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    _ingested_creatinine_rise(app, "victim-tenant")
    for q in ("", "?voice=sample"):
        r = client.get(_URL + q, headers={"X-Tenant-Id": "victim-tenant"})
        assert r.status_code in (401, 403), q
        assert "creatinine" not in r.get_data(as_text=True)


def test_voice_cannot_cross_tenants_with_a_foreign_token(
        app, client, other_tenant_headers, monkeypatch):
    """other-tenant's valid token, aimed at victim-tenant's header, fails;
    and other-tenant's own brief never shows victim's rise."""
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    _ingested_creatinine_rise(app, "victim-tenant")
    forged = dict(other_tenant_headers, **{"X-Tenant-Id": "victim-tenant"})
    r = client.get(_URL + "?voice=sample", headers=forged)
    assert r.status_code in (401, 403)
    body = client.get(_URL + "?voice=sample",
                      headers=other_tenant_headers).get_data(as_text=True)
    assert "creatinine rose" not in body


# --- injection -----------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "?voice=%3Cscript%3Ealert(1)%3C/script%3E",
    "?voice=sample%0aX-Injected:1",
    "?voice=patient&voice=sample",
    "?voice[]=sample",
    "?voice=" + "s" * 5000,
])
def test_voice_value_is_never_echoed_and_never_selects_sample(
        app, client, tenant_id, tenant_headers, q):
    _ingested_creatinine_rise(app, tenant_id)
    plain = client.get(_URL, headers=tenant_headers).get_json()
    r = client.get(_URL + q, headers=tenant_headers)
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "script" not in text and "Injected" not in text
    assert r.get_json() == plain


# --- the open question: a REAL tenant in the sample voice -----------------------

def test_engine_words_an_ingested_tenants_urgent_result_as_the_sample_persons(
        app, client, tenant_id, tenant_headers):
    """FINDING (not a V1/V2/V3/V6 break): nothing in the engine ties
    `voice=sample` to a seeded tenant. Any caller already authorised to read
    a tenant can have the engine say a person's own urgent creatinine rise
    belongs to "the sample person". CareAgents only asks on kind=="sample",
    but the parameter is public on the engine's FHIR surface."""
    _ingested_creatinine_rise(app, tenant_id)
    body = client.get(_URL + "?voice=sample", headers=tenant_headers).get_json()
    [line] = [s for s in _strings(body) if "creatinine rose" in s]
    assert "the sample person's creatinine rose" in line
    assert "your" not in line.lower()
