#!/usr/bin/env python3
"""
scripts/seed_demo_tenant.py

Seed the desktop-demo tenant with sample FHIR data. Can be run:
  - Standalone against a running server (HTTP mode, default)
  - Directly against the database (DB mode, for deploy hooks)

Usage:
    # HTTP mode — calls the running server's /internal/seed endpoint
    python scripts/seed_demo_tenant.py
    python scripts/seed_demo_tenant.py --base-url https://healthclaw.example.railway.app
    python scripts/seed_demo_tenant.py --tenant-id my-tenant

    # DB mode — writes directly to the database (no running server needed)
    python scripts/seed_demo_tenant.py --db-mode

    # With a custom bundle file
    python scripts/seed_demo_tenant.py --bundle-file exports/healthex-2026-04-08.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path for DB mode imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def seed_http(base_url: str, tenant_id: str, bundle: dict | None = None) -> dict:
    """Seed via HTTP POST to the running server."""
    import httpx

    url = f"{base_url.rstrip('/')}/r6/fhir/internal/seed"
    body: dict = {"tenant_id": tenant_id}
    if bundle:
        body["bundle"] = bundle

    print(f"POST {url}")
    resp = httpx.post(url, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def seed_db(tenant_id: str, bundle: dict | None = None) -> int:
    """Seed directly via SQLAlchemy (no running server needed)."""
    from main import app
    from r6.seed import seed_demo_data

    resources = None
    if bundle:
        entries = bundle.get("entry", [])
        resources = [e.get("resource") for e in entries if e.get("resource")]

    with app.app_context():
        return seed_demo_data(tenant_id, resources=resources)


def main():
    p = argparse.ArgumentParser(description="Seed demo tenant with sample FHIR data")
    p.add_argument("--tenant-id", default="desktop-demo",
                   help="Tenant to seed (default: desktop-demo)")
    p.add_argument("--base-url", default=os.environ.get("HEALTHCLAW_URL", "http://localhost:5000"),
                   help="Server base URL for HTTP mode (default: http://localhost:5000)")
    p.add_argument("--bundle-file", default=None,
                   help="JSON bundle file to import instead of built-in demo data")
    p.add_argument("--db-mode", action="store_true",
                   help="Write directly to database (no running server needed)")
    args = p.parse_args()

    bundle = None
    if args.bundle_file:
        with open(args.bundle_file) as f:
            bundle = json.load(f)
        print(f"Loaded bundle: {len(bundle.get('entry', []))} entries from {args.bundle_file}")

    if args.db_mode:
        count = seed_db(args.tenant_id, bundle)
        print(f"Seeded {count} resources into tenant '{args.tenant_id}' (DB mode)")
    else:
        result = seed_http(args.base_url, args.tenant_id, bundle)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
