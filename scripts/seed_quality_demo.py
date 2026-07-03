"""Seed a synthetic hypertensive panel so the NQF 0018 population measure has a
believable BP-control rate for the demo. Composite (fictional) patients only.

The panel is designed to land near a realistic safety-net NQF 0018 number:
11 hypertensive patients, 1 pregnancy-excluded -> 10 in the scored denominator,
7 controlled (<140/90) -> 70% BP control.

Usage (against a running server; winters-demo is a public demo tenant):
    python scripts/seed_quality_demo.py --base-url https://app.healthclaw.io \
        --tenant-id winters-demo
"""

import argparse
import json

TENANT = "winters-demo"

# (label, birth_date, controlled_systolic/diastolic, exclusion)
_PANEL = [
    ("htn-01", "1968-02-10", 128, 78, None),
    ("htn-02", "1955-07-22", 134, 84, None),
    ("htn-03", "1972-11-03", 138, 88, None),
    ("htn-04", "1960-05-14", 130, 82, None),
    ("htn-05", "1949-09-30", 136, 86, None),
    ("htn-06", "1981-03-19", 132, 80, None),
    ("htn-07", "1958-12-01", 139, 89, None),
    ("htn-08", "1965-06-25", 150, 96, None),   # uncontrolled
    ("htn-09", "1974-08-08", 148, 92, None),   # uncontrolled
    ("htn-10", "1970-01-15", 158, 98, None),   # uncontrolled
    ("htn-11", "1992-04-04", 126, 76, "pregnancy"),  # excluded
]


def build_quality_cohort():
    """Pure: return the cohort as (patient, condition, [pregnancy?], observation)
    groups plus the expected NQF 0018 numbers."""
    groups = []
    for (label, dob, sys_v, dia_v, excl) in _PANEL:
        conditions = [{
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                                 "code": "I10", "display": "Essential hypertension"}]},
        }]
        if excl == "pregnancy":
            conditions.append({
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"code": "active"}]},
                "code": {"coding": [{"system": "http://snomed.info/sct",
                                     "code": "77386006", "display": "Pregnancy"}]},
            })
        observation = {
            "resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
            "effectiveDateTime": "2026-09-15",
            "component": [
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                 "valueQuantity": {"value": sys_v, "unit": "mm[Hg]"}},
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                 "valueQuantity": {"value": dia_v, "unit": "mm[Hg]"}},
            ],
        }
        groups.append({"label": label, "birth_date": dob,
                       "conditions": conditions, "observation": observation,
                       "excluded": excl is not None})
    denom = sum(1 for g in groups if not g["excluded"])
    numer = sum(1 for (l, d, s, di, e) in _PANEL
                if e is None and s < 140 and di < 90)
    return {"groups": groups, "denominator": denom, "numerator": numer,
            "exclusions": sum(1 for g in groups if g["excluded"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:5000")
    ap.add_argument("--tenant-id", default=TENANT)
    ap.add_argument("--step-up-token")
    args = ap.parse_args()

    import requests
    base = args.base_url.rstrip("/")
    hdr = {"X-Tenant-Id": args.tenant_id, "Content-Type": "application/fhir+json"}

    token = args.step_up_token
    if not token:
        r = requests.post(f"{base}/r6/fhir/internal/step-up-token",
                          headers={"X-Tenant-Id": args.tenant_id,
                                   "Content-Type": "application/json"},
                          data=json.dumps({"tenant_id": args.tenant_id}))
        token = r.json().get("step_up_token") or r.json().get("token")
    whdr = {**hdr, "X-Step-Up-Token": token or ""}

    cohort = build_quality_cohort()
    created = 0
    for g in cohort["groups"]:
        pr = requests.post(f"{base}/r6/fhir/Patient", headers=whdr,
                           data=json.dumps({"resourceType": "Patient",
                                            "birthDate": g["birth_date"]}))
        pid = pr.json().get("id")
        if not pid:
            print(f"  patient create failed for {g['label']}: HTTP {pr.status_code}")
            continue
        ref = {"reference": f"Patient/{pid}"}
        for cond in g["conditions"]:
            requests.post(f"{base}/r6/fhir/Condition", headers=whdr,
                          data=json.dumps({**cond, "subject": ref}))
        requests.post(f"{base}/r6/fhir/Observation", headers=whdr,
                      data=json.dumps({**g["observation"], "subject": ref}))
        created += 1
    print(f"Seeded {created} hypertensive patients into {args.tenant_id}. "
          f"Expected NQF 0018: {cohort['numerator']}/{cohort['denominator']} "
          f"controlled ({round(cohort['numerator']/cohort['denominator']*100)}%), "
          f"{cohort['exclusions']} excluded.")


if __name__ == "__main__":
    main()
