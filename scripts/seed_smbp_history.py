#!/usr/bin/env python3
"""Deliver the three-year SMBP demo history to a tenant.

    python scripts/seed_smbp_history.py \
        --base-url https://app.healthclaw.io \
        --tenant-id desktop-demo \
        --internal-secret "$INTERNAL_TOKEN_MINT_SECRET"

Posts to /r6/fhir/internal/seed, which requires the internal secret for a
caller-supplied bundle. That gate is the fix for the hole this script was
being written against: before it, this bundle would have landed in a public
tenant with no credential at all.

Idempotent because every resource carries a fixed id and the seed skips ids
it already holds — so re-running is a no-op, and `created` coming back as 0
on a second run is the expected result, not a failure.

Synthetic composites only. Nothing here is traceable to a real person.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from r6.smbp.demo_history import smbp_history_resources  # noqa: E402


def _bundle(resources):
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": r} for r in resources]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:5099")
    ap.add_argument("--tenant-id", default="desktop-demo")
    ap.add_argument("--internal-secret",
                    default=os.environ.get("INTERNAL_TOKEN_MINT_SECRET", ""))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the summary and write nothing")
    args = ap.parse_args()

    resources = smbp_history_resources()

    patients = [r for r in resources if r["resourceType"] == "Patient"]
    obs = [r for r in resources if r["resourceType"] == "Observation"]
    print(f"{len(resources)} resources: {len(patients)} patients, "
          f"{len(obs)} observations")
    for p in patients:
        name = p["name"][0]
        mine = [o for o in obs
                if o["subject"]["reference"] == f"Patient/{p['id']}"]
        print(f"  {name['given'][0]} {name['family']:<12} {len(mine):>3} readings")

    if args.dry_run:
        return 0

    if not args.internal_secret:
        print("\nERROR: --internal-secret (or INTERNAL_TOKEN_MINT_SECRET) is "
              "required. A caller-supplied bundle takes the ingest gate.",
              file=sys.stderr)
        return 2

    import requests

    resp = requests.post(
        f"{args.base_url.rstrip('/')}/r6/fhir/internal/seed",
        json={"tenant_id": args.tenant_id, "bundle": _bundle(resources)},
        headers={"Content-Type": "application/json",
                 "X-Internal-Secret": args.internal_secret},
        timeout=120,
    )
    if resp.status_code != 201:
        # Print the status and body, not the request: the request carries the
        # secret, and a failed seed is exactly when someone pastes the whole
        # traceback into an issue.
        print(f"\nSEED FAILED: HTTP {resp.status_code}\n{resp.text[:500]}",
              file=sys.stderr)
        return 1

    body = resp.json()
    print(f"\ncreated {body.get('created_count')} new resources in "
          f"{body.get('tenant_id')}")
    print("(0 on a re-run is correct — every resource has a fixed id)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
