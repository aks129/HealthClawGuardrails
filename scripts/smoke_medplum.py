"""Live smoke: prove the HealthClaw guardrails wrap a Medplum-backed FHIR store.

Runs against a HealthClaw Flask instance configured with MEDPLUM_BASE_URL /
MEDPLUM_CLIENT_ID / MEDPLUM_CLIENT_SECRET (see
docs/recipes/healthclaw-in-front-of-medplum.md). Creates a SYNTHETIC patient in
Medplum through HealthClaw, reads it back, and verifies the guardrails.

Usage:
    python scripts/smoke_medplum.py --base-url https://your-healthclaw \
        --tenant-id <tenant> --step-up-token <token>

Exit code 0 = all guardrail checks passed.
"""

import argparse
import json
import sys

SYNTHETIC_PATIENT = {
    "resourceType": "Patient",
    "name": [{"family": "Testpatient", "given": ["Smoke"]}],
    "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                    "value": "000-00-1234"}],
    "telecom": [{"system": "phone", "value": "555-000-1234"}],
    "birthDate": "1980-01-01",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--tenant-id", required=True)
    ap.add_argument("--step-up-token", required=True,
                    help="tenant-bound step-up token (POST /r6/fhir/internal/step-up-token)")
    args = ap.parse_args()

    import requests
    base = args.base_url.rstrip("/")
    read_hdr = {"X-Tenant-Id": args.tenant_id}
    write_hdr = {**read_hdr, "Content-Type": "application/fhir+json",
                 "X-Step-Up-Token": args.step_up_token,
                 "X-Human-Confirmed": "true"}

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. Write is gated: create without step-up must be refused before Medplum.
    r = requests.post(f"{base}/r6/fhir/Patient", headers=read_hdr,
                      data=json.dumps(SYNTHETIC_PATIENT))
    check("write blocked without step-up (401)", r.status_code == 401,
          f"got {r.status_code}")

    # 2. Guardrailed write reaches Medplum.
    r = requests.post(f"{base}/r6/fhir/Patient", headers=write_hdr,
                      data=json.dumps(SYNTHETIC_PATIENT))
    ok_create = r.status_code in (200, 201)
    pid = (r.json() or {}).get("id") if ok_create else None
    check("guardrailed create -> Medplum (201)", ok_create and bool(pid),
          f"id={pid}" if pid else f"status {r.status_code}")
    if not pid:
        _finish(results)

    # 3. Read back through HealthClaw is redacted.
    r = requests.get(f"{base}/r6/fhir/Patient/{pid}", headers=read_hdr)
    body = r.json() if r.status_code == 200 else {}
    blob = json.dumps(body)
    fam = (body.get("name", [{}])[0] or {}).get("family", "")
    check("read returns 200", r.status_code == 200, f"status {r.status_code}")
    check("name redacted (initial only)", fam == "T.", f"family={fam!r}")
    check("SSN masked in read", "000-00-1234" not in blob)
    check("phone redacted in read", "555-000-1234" not in blob)
    check("Medplum-sourced", body.get("_source") == "upstream")

    # 4. Audit trail recorded.
    a = requests.get(f"{base}/r6/fhir/AuditEvent?_count=10", headers=read_hdr)
    audit_ok = a.status_code == 200 and (a.json().get("total", 0) or
                                         len(a.json().get("entry", []))) >= 1
    check("access audited (AuditEvent present)", audit_ok)

    _finish(results)


def _finish(results):
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} guardrail checks passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
