import json

from r6.models import R6Resource, db
from r6.smbp.monitoring import build_bp_observation


def test_enroll_creates_session(client, tenant_headers):
    resp = client.post("/r6/smbp/enroll", headers=tenant_headers,
                       json={"patient_ref": "Patient/p1", "language": "es"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["language"] == "es"
    assert body["id"]


def test_reading_requires_step_up(client, tenant_headers):
    resp = client.post("/r6/smbp/reading", headers=tenant_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 142,
                             "diastolic": 88, "effective": "2026-06-01T08:00:00Z"})
    assert resp.status_code == 401


def test_reading_logs_observation_and_classifies(client, auth_headers, tenant_id, app):
    resp = client.post("/r6/smbp/reading", headers=auth_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 168,
                             "diastolic": 104, "effective": "2026-06-02T20:00:00Z"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["triage"]["band"] == "followup"
    with app.app_context():
        n = R6Resource.query.filter_by(resource_type="Observation",
                                       tenant_id=tenant_id).count()
        assert n >= 1


def test_emergency_reading_flagged_in_response(client, auth_headers):
    resp = client.post("/r6/smbp/reading", headers=auth_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 190,
                             "diastolic": 122, "effective": "2026-06-03T08:00:00Z",
                             "symptoms": ["chest_pain"]})
    assert resp.status_code == 201
    assert resp.get_json()["triage"]["emergency"] is True


def _seed_session_and_readings(client, app, tenant_id, auth_headers):
    enroll = client.post("/r6/smbp/enroll", headers={**auth_headers},
                         json={"patient_ref": "Patient/p1", "language": "en"})
    session_id = enroll.get_json()["id"]
    with app.app_context():
        for s, d, when in [(142, 90, "2026-06-01T08:00:00Z"),
                           (150, 96, "2026-06-01T20:00:00Z"),
                           (134, 86, "2026-06-02T08:00:00Z")]:
            obs = build_bp_observation("Patient/p1", s, d, when)
            db.session.add(R6Resource(resource_type="Observation",
                                      resource_json=json.dumps(obs),
                                      tenant_id=tenant_id))
        db.session.commit()
    return session_id


def test_report_html_and_pdf(client, app, tenant_id, auth_headers, tenant_headers):
    session_id = _seed_session_and_readings(client, app, tenant_id, auth_headers)
    html = client.get(f"/r6/smbp/report/{session_id}", headers=tenant_headers)
    assert html.status_code == 200
    assert b"135/85" in html.data
    pdf = client.get(f"/r6/smbp/report/{session_id}?format=pdf", headers=tenant_headers)
    assert pdf.status_code == 200
    assert pdf.data[:4] == b"%PDF"
