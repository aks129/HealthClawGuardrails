"""
HealthEx / Flexpa / generic FHIR bundle importer.

Posts a FHIR R4 transaction bundle through the HealthClaw guardrail proxy
with proper step-up auth headers, then retrieves and summarises the resulting
context envelope.

Usage:
    python scripts/import_healthex.py \\
        --bundle-file my-records.json \\
        --tenant-id my-patient \\
        --step-up-secret $STEP_UP_SECRET

The bundle format expected is a FHIR R4 transaction Bundle:
    {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": { "resourceType": "Patient", ... },
                "request": { "method": "POST", "url": "Patient" }
            },
            ...
        ]
    }
"""

import argparse
import json
import os
import sys

import requests

# Allow running from repo root without installing as a package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from r6.stepup import generate_step_up_token  # noqa: E402 — after sys.path insert


DEFAULT_BASE_URL = "http://localhost:5000/r6/fhir"


def build_parser():
    p = argparse.ArgumentParser(
        description="Import a FHIR bundle into the HealthClaw guardrail store"
    )
    p.add_argument(
        "--bundle-file",
        required=True,
        help="Path to a FHIR R4 transaction Bundle JSON file",
    )
    p.add_argument(
        "--tenant-id",
        default="desktop-demo",
        help="Tenant ID to store resources under (default: desktop-demo)",
    )
    p.add_argument(
        "--step-up-secret",
        default=os.environ.get("STEP_UP_SECRET", ""),
        help="HMAC secret for step-up token (or set STEP_UP_SECRET env var)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("FHIR_LOCAL_BASE_URL", DEFAULT_BASE_URL),
        help=f"HealthClaw FHIR base URL (default: {DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print headers without sending",
    )
    return p


def import_bundle(bundle, tenant_id, step_up_secret, base_url, dry_run=False):
    """
    POST bundle to /Bundle/$ingest-context with guardrail headers.

    Returns the context envelope dict on success, raises on failure.
    """
    if not step_up_secret:
        sys.exit(
            "ERROR: STEP_UP_SECRET is required. Pass --step-up-secret or set "
            "the STEP_UP_SECRET environment variable."
        )

    # Re-use the exact same token logic as the server — no re-implementation
    os.environ["STEP_UP_SECRET"] = step_up_secret
    token = generate_step_up_token(tenant_id, agent_id="import_healthex")

    headers = {
        "Content-Type": "application/fhir+json",
        "X-Tenant-ID": tenant_id,
        "X-Step-Up-Token": token,
        "X-Human-Confirmed": "true",
    }

    url = f"{base_url.rstrip('/')}/Bundle/$ingest-context"

    if dry_run:
        print("DRY RUN — would POST to:", url)
        print("Headers:", json.dumps(headers, indent=2))
        print("Bundle entries:", len(bundle.get("entry", [])))
        return None

    print(f"POSTing bundle ({len(bundle.get('entry', []))} entries) to {url} ...")
    resp = requests.post(url, json=bundle, headers=headers, timeout=30)

    if resp.status_code not in (200, 201):
        sys.exit(
            f"ERROR: Ingest failed with HTTP {resp.status_code}\n{resp.text[:500]}"
        )

    return resp.json()


def fetch_context(context_id, tenant_id, base_url):
    """Retrieve and return the context envelope."""
    url = f"{base_url.rstrip('/')}/context/{context_id}"
    resp = requests.get(
        url,
        headers={"X-Tenant-ID": tenant_id},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def print_summary(context):
    """Print a human-readable summary of the ingested context envelope."""
    if not context:
        return
    print("\n=== Context Envelope ===")
    print(f"  ID:             {context.get('context_id', '?')}")
    print(f"  Tenant:         {context.get('tenant_id', '?')}")
    print(f"  Resources:      {context.get('resource_count', '?')}")
    print(f"  Created:        {context.get('created_at', '?')}")

    resource_types = context.get("resource_types", [])
    if resource_types:
        print(f"  Resource types: {', '.join(resource_types)}")

    patient_ref = context.get("patient_reference")
    if patient_ref:
        print(f"  Patient ref:    {patient_ref}")

    print(
        "\nUse this context_id with the MCP fhir_search or context_get tools "
        "to query the imported data."
    )
    print(f"\n  context_id = {context.get('context_id')}")


def main():
    args = build_parser().parse_args()

    # Load bundle
    bundle_path = args.bundle_file
    if not os.path.exists(bundle_path):
        sys.exit(f"ERROR: Bundle file not found: {bundle_path}")

    with open(bundle_path) as f:
        try:
            bundle = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: Invalid JSON in bundle file: {e}")

    if bundle.get("resourceType") != "Bundle":
        sys.exit(
            f"ERROR: Expected resourceType Bundle, got {bundle.get('resourceType')}"
        )

    result = import_bundle(
        bundle=bundle,
        tenant_id=args.tenant_id,
        step_up_secret=args.step_up_secret,
        base_url=args.base_url,
        dry_run=args.dry_run,
    )

    if args.dry_run or result is None:
        return

    context_id = result.get("context_id")
    print(f"\nIngested successfully. context_id = {context_id}")

    if context_id:
        context = fetch_context(context_id, args.tenant_id, args.base_url)
        print_summary(context or result)
    else:
        print("Response:", json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
