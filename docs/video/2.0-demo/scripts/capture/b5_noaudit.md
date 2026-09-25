b5 fail-closed capture (2026-09-25):
- copied run/engine.db to run/engine_noaudit.db
- sqlite: alter table audit_events rename to audit_events_switched_off
- started a second engine on :5098 against that copy (same code, 76fff1d)
- GET /r6/fhir/Patient/demo-patient-rivera (X-Tenant-Id: desktop-demo)
- status: see b5_noaudit_status.txt (500); body: b5_noaudit_body.txt (generic
  500 page, no patient data); log line: b5_noaudit_error.txt
- engine on :5098 stopped afterwards
