"""b6 setup: propose an intake form on a signed-in person's sample records.

No model is involved: the form is proposed exactly the way the agent's
`start_intake_form` tool proposes it (HealthClawClient.start_form_action), as
tests/test_beta_acceptance_rows.py does. Talks to the running engine over
HTTP with the local mint secret. Prints the action id.

Usage: propose_form.py <careagents connection id> [--allergy]

--allergy first adds ONE synthetic AllergyIntolerance to that tenant — the
same fixture the acceptance test uses (Chain(allergy=True)) — so the on-camera
review confirms an allergy that is on file rather than asking the person to
attest "no known allergies". Written through the engine's own model inside its
app context, as that test does; no HTTP write path is used.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

V = Path(__file__).resolve().parent.parent
ALLERGY = {"resourceType": "AllergyIntolerance", "id": "acc-allergy-1",
           "patient": {"reference": "Patient/demo-patient-rivera"},
           "clinicalStatus": {"coding": [{"code": "active"}]},
           "code": {"coding": [{"system": "http://snomed.info/sct",
                                "code": "91936005"}]}}


def main():
    conn_id = sys.argv[1]
    con = sqlite3.connect(V / "run" / "careagents.db")
    tenant = con.execute("select tenant_id from ca_connections where id=?",
                         (conn_id,)).fetchone()[0]
    if "--allergy" in sys.argv:
        from main import create_app
        from models import db
        from r6.models import R6Resource
        app = create_app({"LEGACY_BOOT_ON_CREATE": False})
        with app.app_context():
            db.session.add(R6Resource("AllergyIntolerance", json.dumps(ALLERGY),
                                      resource_id=ALLERGY["id"],
                                      tenant_id=tenant))
            db.session.commit()
    from careagents.healthclaw import HealthClawClient
    hc = HealthClawClient(base=os.environ["HEALTHCLAW_BASE"],
                          mint_secret=os.environ["HEALTHCLAW_MINT_SECRET"])
    print(hc.start_form_action(tenant))


if __name__ == "__main__":
    main()
