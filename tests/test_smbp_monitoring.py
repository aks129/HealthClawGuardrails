import json

from r6.smbp.models import SMBPSession
from r6.models import db


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
