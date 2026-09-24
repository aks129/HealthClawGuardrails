"""Tests for the SMBP reminder scheduler (decision engine + /reminders/due).

The scheduler decides WHO is due for a reading reminder on their prescribed
cadence. Pure decision functions (reminder_due / due_reminders) plus a
read-shaped Flask endpoint that returns a PHI-safe overview (counts + labels,
never a phone number).
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from r6.models import R6Resource, db
from r6.smbp.monitoring import build_bp_observation
from r6.smbp.scheduler import reminder_due, due_reminders


NOW = datetime(2026, 6, 15, 9, 0, 0)  # naive UTC, matches the model columns


def _enroll(**overrides):
    """A SMBPSession-shaped test double with sensible defaults."""
    base = dict(
        patient_ref="Patient/p1",
        language="en",
        days=14,
        prescribed=28,
        completed=3,
        started=NOW - timedelta(days=5),
        last_reading_at=NOW - timedelta(days=2),
        last_reminded_at=None,
        phone="+15551230000",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- reminder_due ---------------------------------------------------------

def test_due_when_behind_cadence():
    # Last reading 2 days ago, daily cadence, not done, never reminded -> due.
    assert reminder_due(_enroll(), NOW) is True


def test_not_due_when_up_to_date():
    # Logged a reading an hour ago -> within the daily interval -> not due.
    e = _enroll(last_reading_at=NOW - timedelta(hours=1))
    assert reminder_due(e, NOW) is False


def test_never_due_when_completed_meets_prescribed():
    # Course finished (completed >= prescribed) -> never remind, even if idle.
    e = _enroll(completed=28, prescribed=28,
                last_reading_at=NOW - timedelta(days=9))
    assert reminder_due(e, NOW) is False


def test_not_due_when_already_reminded_this_interval():
    # Behind on readings, but reminded 2h ago on a daily cadence -> hold off.
    e = _enroll(last_reading_at=NOW - timedelta(days=3),
                last_reminded_at=NOW - timedelta(hours=2))
    assert reminder_due(e, NOW) is False


def test_due_again_when_last_reminder_older_than_interval():
    e = _enroll(last_reading_at=NOW - timedelta(days=3),
                last_reminded_at=NOW - timedelta(days=2))
    assert reminder_due(e, NOW) is True


def test_default_cadence_is_daily_when_field_absent():
    # A bare SMBPSession-like object (no cadence attr) defaults to daily.
    e = SimpleNamespace(patient_ref="Patient/p2", language="en", days=14,
                        started=NOW - timedelta(days=4))
    # No readings logged since start (4 days) -> behind on a daily cadence.
    assert reminder_due(e, NOW) is True


def test_twice_daily_cadence_shortens_interval():
    # 8h since last reading: not due daily, but due on a 12h... still <12h? -> not due
    e = _enroll(cadence="twice_daily", last_reading_at=NOW - timedelta(hours=8))
    assert reminder_due(e, NOW) is False
    e2 = _enroll(cadence="twice_daily", last_reading_at=NOW - timedelta(hours=13))
    assert reminder_due(e2, NOW) is True


# --- due_reminders --------------------------------------------------------

def test_due_reminders_builds_sms_payload_with_phone_in_payload_phone():
    reminders = due_reminders([_enroll()], NOW)
    assert len(reminders) == 1
    item = reminders[0]
    assert item["patient_ref"] == "Patient/p1"
    action = item["reminder_action_payload"]
    assert action["kind"] == "sms"
    # Phone in payload.phone, NOT payload.to (which is the PHI-safe label).
    assert action["payload"]["phone"] == "+15551230000"
    assert action["payload"]["to"] == "patient"
    assert action["payload"]["phone"] != action["payload"]["to"]


def test_due_reminders_skips_up_to_date_and_completed():
    enrollments = [
        _enroll(patient_ref="Patient/behind"),                       # due
        _enroll(patient_ref="Patient/current",
                last_reading_at=NOW - timedelta(hours=1)),           # not due
        _enroll(patient_ref="Patient/done", completed=28),           # not due
    ]
    reminders = due_reminders(enrollments, NOW)
    refs = [r["patient_ref"] for r in reminders]
    assert refs == ["Patient/behind"]


def test_due_reminders_never_leaks_phone_into_patient_ref_or_summary():
    reminders = due_reminders([_enroll()], NOW)
    # The top-level listing keys never carry the number.
    item = reminders[0]
    assert "+15551230000" not in json.dumps(
        {k: v for k, v in item.items() if k != "reminder_action_payload"})


# --- route: GET /r6/smbp/reminders/due ------------------------------------

def _seed(client, app, tenant_id, auth_headers, patient_ref, readings):
    client.post("/r6/smbp/enroll", headers=auth_headers,
                json={"patient_ref": patient_ref, "language": "en"})
    with app.app_context():
        for s, d, when in readings:
            obs = build_bp_observation(patient_ref, s, d, when)
            db.session.add(R6Resource(resource_type="Observation",
                                      resource_json=json.dumps(obs),
                                      tenant_id=tenant_id))
        db.session.commit()


def test_reminders_due_endpoint_returns_count_and_no_phone(
        client, app, tenant_id, auth_headers, tenant_headers):
    # One enrolled patient with a stale reading (a while ago) -> due.
    _seed(client, app, tenant_id, auth_headers, "Patient/stale",
          [(140, 90, "2026-01-01T08:00:00Z")])
    resp = client.get("/r6/smbp/reminders/due", headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "due" in body and isinstance(body["due"], int)
    assert body["due"] >= 1
    for item in body["reminders"]:
        assert "patient_ref" in item
        assert "lang" in item
        assert "has_contact" in item
    # No phone number anywhere in the response body.
    assert "555" not in json.dumps(body)


def test_reminders_due_requires_tenant_header(client):
    resp = client.get("/r6/smbp/reminders/due")
    assert resp.status_code == 400


def test_reminders_due_read_auth_for_nonpublic_tenant(client, app, monkeypatch):
    monkeypatch.setenv("READ_AUTH_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_TENANTS", "")
    resp = client.get("/r6/smbp/reminders/due",
                      headers={"X-Tenant-Id": "private-smbp"})
    assert resp.status_code == 401
    assert resp.get_json()["issue"][0]["code"] == "security"


def test_a_deleted_reading_does_not_make_the_patient_up_to_date(
        client, app, tenant_id, auth_headers, tenant_headers):
    """The patient's only live reading is months old, so a reminder is due.
    A deleted reading from an hour ago must not count as the latest one and
    silence it.

    MUTATION: drop `is_deleted=False` from reminders_due's query -> red."""
    ref = "Patient/deleted-recent"
    _seed(client, app, tenant_id, auth_headers, ref,
          [(140, 90, "2026-01-01T08:00:00Z")])
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with app.app_context():
        row = R6Resource(resource_type="Observation",
                         resource_json=json.dumps(
                             build_bp_observation(ref, 128, 78, recent)),
                         tenant_id=tenant_id)
        row.is_deleted = True
        db.session.add(row)
        db.session.commit()

    resp = client.get("/r6/smbp/reminders/due", headers=tenant_headers)
    assert resp.status_code == 200
    refs = [item["patient_ref"] for item in resp.get_json()["reminders"]]
    assert ref in refs, "a deleted reading silenced a due reminder"
