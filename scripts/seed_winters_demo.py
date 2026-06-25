"""Seed the synthetic `winters-demo` tenant for the SMBP demos.

Composite patients only — no detail traceable to a real person. Marisol
(smartphone, Spanish) trends to a confirmed-hypertension home average ~138/88;
Mr. Ray (landline, English) includes a 164/98 escalation reading.

Usage (against a running server, with a step-up token):
    python scripts/seed_winters_demo.py --base-url http://localhost:5000 \
        --tenant-id winters-demo --step-up-token <token>
"""

import argparse
import json

TENANT = "winters-demo"


def _two_per_day(start_day, systolics_am, systolics_pm, dia_am, dia_pm):
    """Build (systolic, diastolic, effectiveDateTime) tuples, 2/day."""
    out = []
    for i, (sa, sp, da, dp) in enumerate(
            zip(systolics_am, systolics_pm, dia_am, dia_pm)):
        day = f"2026-06-{start_day + i:02d}"
        out.append((sa, da, f"{day}T08:00:00Z"))
        out.append((sp, dp, f"{day}T20:00:00Z"))
    return out


def build_demo_dataset():
    """Return the composite demo dataset (pure data; no I/O)."""
    marisol_am_s = [136, 138, 134, 140, 137, 135, 139, 136, 138, 134, 137, 139, 135, 138]
    marisol_pm_s = [140, 142, 138, 141, 139, 140, 142, 138, 141, 139, 140, 142, 138, 140]
    marisol_am_d = [86, 88, 85, 89, 87, 85, 88, 86, 87, 85, 88, 89, 86, 87]
    marisol_pm_d = [90, 91, 88, 90, 89, 90, 91, 88, 90, 89, 90, 91, 88, 90]
    marisol_readings = _two_per_day(1, marisol_am_s, marisol_pm_s,
                                    marisol_am_d, marisol_pm_d)

    ray = [
        (150, 92, "2026-06-01T08:00:00Z"), (158, 96, "2026-06-01T20:00:00Z"),
        (152, 94, "2026-06-02T08:00:00Z"), (164, 98, "2026-06-02T20:00:00Z"),
        (156, 95, "2026-06-03T08:00:00Z"), (159, 97, "2026-06-03T20:00:00Z"),
    ]

    return {
        "tenant_id": TENANT,
        "patients": [
            {"label": "Marisol", "patient_ref": "Patient/marisol",
             "language": "es", "readings": marisol_readings},
            {"label": "Mr. Ray", "patient_ref": "Patient/mr-ray",
             "language": "en", "readings": ray},
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:5000")
    ap.add_argument("--tenant-id", default=TENANT)
    ap.add_argument("--step-up-token", required=True)
    args = ap.parse_args()

    import requests
    data = build_demo_dataset()
    headers = {"X-Tenant-Id": args.tenant_id,
               "X-Step-Up-Token": args.step_up_token,
               "Content-Type": "application/json"}
    for p in data["patients"]:
        requests.post(f"{args.base_url}/r6/smbp/enroll",
                      headers={"X-Tenant-Id": args.tenant_id,
                               "Content-Type": "application/json"},
                      data=json.dumps({"patient_ref": p["patient_ref"],
                                       "language": p["language"]}))
        for (s, d, when) in p["readings"]:
            requests.post(f"{args.base_url}/r6/smbp/reading", headers=headers,
                          data=json.dumps({"patient_ref": p["patient_ref"],
                                           "systolic": s, "diastolic": d,
                                           "effective": when}))
    print(f"Seeded {args.tenant_id}: "
          f"{sum(len(p['readings']) for p in data['patients'])} readings")


if __name__ == "__main__":
    main()
