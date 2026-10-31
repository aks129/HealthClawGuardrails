from r6.smbp.models import SMBPSession
from r6.models import db
import pytest

pytestmark = pytest.mark.extension


def test_smbp_session_persists(app):
    with app.app_context():
        s = SMBPSession(tenant_id="t1", patient_ref="Patient/p1",
                        language="es", days=14)
        db.session.add(s)
        db.session.commit()
        got = SMBPSession.query.filter_by(tenant_id="t1").first()
        assert got is not None
        assert got.patient_ref == "Patient/p1"
        assert got.language == "es"
        assert got.days == 14
        assert got.id  # uuid assigned


from r6.smbp.monitoring import build_bp_observation, slot_of, averages, adherence


def test_build_bp_observation_shape():
    obs = build_bp_observation("Patient/p1", 142, 88, "2026-06-01T08:00:00Z")
    assert obs["resourceType"] == "Observation"
    assert obs["code"]["coding"][0]["code"] == "85354-9"
    comps = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"]
             for c in obs["component"]}
    assert comps["8480-6"] == 142  # systolic
    assert comps["8462-4"] == 88   # diastolic
    assert obs["subject"] == {"reference": "Patient/p1"}
    assert obs["effectiveDateTime"] == "2026-06-01T08:00:00Z"


def test_slot_of_am_pm():
    assert slot_of("2026-06-01T08:00:00Z") == "AM"
    assert slot_of("2026-06-01T19:30:00Z") == "PM"


def _obs(s, d, when):
    return build_bp_observation("Patient/p1", s, d, when)


def test_averages_am_pm_overall():
    obs = [_obs(140, 90, "2026-06-01T08:00:00Z"),
           _obs(150, 100, "2026-06-01T20:00:00Z"),
           _obs(130, 80, "2026-06-02T08:00:00Z")]
    a = averages(obs)
    assert a["am"] == {"systolic": 135, "diastolic": 85}   # (140+130)/2, (90+80)/2
    assert a["pm"] == {"systolic": 150, "diastolic": 100}
    assert a["overall"] == {"systolic": 140, "diastolic": 90}  # (140+150+130)/3, (90+100+80)/3
    assert a["valid_days"] == 2


def test_adherence_rate():
    # 14-day session prescribes 2 readings/day = 28; we have 3
    obs = [_obs(140, 90, "2026-06-01T08:00:00Z"),
           _obs(150, 100, "2026-06-01T20:00:00Z"),
           _obs(130, 80, "2026-06-02T08:00:00Z")]
    a = adherence(days=14, observations=obs)
    assert a["completed"] == 3
    assert a["prescribed"] == 28
    assert a["rate"] == round(3 / 28, 2)
