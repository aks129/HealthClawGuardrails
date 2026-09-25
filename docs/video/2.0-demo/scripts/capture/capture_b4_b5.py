"""b4 + b5 capture: one synthetic Patient, read two ways, then the audit rows.

1. Read `demo-patient-rivera` straight out of the engine's SQLite row
   (`r6_resources.resource_json`) — what is stored. No API, so no audit row.
2. Read the same Patient through the running engine at :5099 — what an AI
   client gets back after redaction.
3. Two more reads an assistant would make (Observation, MedicationRequest).
4. Query `audit_events` for rows recorded since step 2 began.

Writes capture/b4_redaction.json and capture/b5_audit.json. Every value on the
b4/b5 slides is generated from these files; nothing is typed by hand.
Synthetic demo record only (r6/seed.py built-ins, loaded by `seed-demo`).
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "run" / "engine.db"
BASE = "http://127.0.0.1:5099/r6/fhir"
TENANT = "desktop-demo"
PID = "demo-patient-rivera"
H = {"X-Tenant-Id": TENANT}


def fmt_name(p):
    n = (p.get("name") or [{}])[0]
    return " ".join(list(n.get("given") or []) + [n.get("family", "")]).strip()


def fmt_phone(p):
    t = [x for x in p.get("telecom") or [] if x.get("system") == "phone"]
    return t[0].get("value", "(none)") if t else "(none)"


def fmt_addr(p):
    a = (p.get("address") or [{}])[0]
    parts = [", ".join(a.get("line") or []), a.get("city"), a.get("state"),
             a.get("postalCode")]
    return ", ".join(x for x in parts if x) or "(none)"


def main():
    con = sqlite3.connect(DB)
    row = con.execute(
        "select resource_json from r6_resources where tenant_id=? and "
        "resource_type='Patient' and id=? and not is_deleted", (TENANT, PID)
    ).fetchone()
    stored = json.loads(row[0])

    t0 = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ")
    time.sleep(0.05)
    r = requests.get(f"{BASE}/Patient/{PID}", headers=H, timeout=10)
    r.raise_for_status()
    returned = r.json()
    bundles = {}
    for path in (f"Observation?patient=Patient/{PID}", f"MedicationRequest?patient=Patient/{PID}"):
        rr = requests.get(f"{BASE}/{path}", headers=H, timeout=10)
        rr.raise_for_status()
        bundles[path.split("?")[0]] = rr.json()
        time.sleep(0.35)

    # The health fact that passes through: the A1c result, stored vs returned.
    def a1c(obs):
        q = obs.get("valueQuantity") or {}
        label = ((obs.get("code") or {}).get("coding") or [{}])[0].get("display") or "(no label)"
        return f"{label} {q.get('value')} {q.get('unit')}"
    stored_a1c = json.loads(con.execute(
        "select resource_json from r6_resources where tenant_id=? and "
        "resource_type='Observation' and id='demo-obs-a1c'", (TENANT,)
    ).fetchone()[0])
    returned_a1c = next(e["resource"] for e in bundles["Observation"].get("entry", [])
                        if e["resource"].get("id") == "demo-obs-a1c")

    rows = [
        {"label": "Name", "stored": fmt_name(stored), "returned": fmt_name(returned)},
        {"label": "Phone", "stored": fmt_phone(stored), "returned": fmt_phone(returned)},
        {"label": "Address", "stored": fmt_addr(stored), "returned": fmt_addr(returned)},
        {"label": "Birth date", "stored": stored.get("birthDate"),
         "returned": returned.get("birthDate")},
        {"label": "Test result", "stored": a1c(stored_a1c), "returned": a1c(returned_a1c)},
    ]
    (HERE / "b4_redaction.json").write_text(json.dumps({
        "captured_at": t0, "tenant": TENANT, "patient_id": PID,
        "db_row_source": f"sqlite {DB.name}: r6_resources.resource_json",
        "api_request": f"GET {BASE}/Patient/{PID}  (X-Tenant-Id: {TENANT})",
        "api_status": r.status_code,
        "stored_resource": stored, "returned_resource": returned,
        "stored_a1c": stored_a1c, "returned_a1c": returned_a1c, "rows": rows,
    }, indent=2))

    audit = [dict(zip(("recorded", "event_type", "resource_type", "outcome"), a))
             for a in con.execute(
                 "select recorded, event_type, resource_type, outcome from "
                 "audit_events where tenant_id=? and recorded > ? "
                 "order by recorded", (TENANT, t0))]
    (HERE / "b5_audit.json").write_text(json.dumps({
        "query": "select recorded, event_type, resource_type, outcome from "
                 "audit_events where tenant_id='desktop-demo' and recorded > "
                 "<start of the b4 read> order by recorded",
        "rows": audit}, indent=2))
    for x in rows:
        print(x)
    for a in audit:
        print(a)


if __name__ == "__main__":
    main()
