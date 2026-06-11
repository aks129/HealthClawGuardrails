#!/usr/bin/env python3
"""Pull FHIR R4 records from MEDENT and redact PHI in-process.

Requires a valid token cache from medent_oauth.py authorize.
Auto-refreshes the access token when it is expired (refresh_token has 24h TTL).

Output format: JSON file with structure compatible with import_healthex.py:
  {
    "records": { "Condition": [...], "Observation": [...], ... },
    "_meta": { "practice_id": "...", "exported_at": "...", "redaction_stats": {...} }
  }

Usage:
  python scripts/export_medent_fhir.py --tenant-id ev-personal
  python scripts/export_medent_fhir.py --tenant-id ev-personal --output ~/medent.json
  python scripts/export_medent_fhir.py --tenant-id ev-personal --no-redact  # dev only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_MEDENT_FHIR_BASE = "https://www.medentfhir.com/fhir/R4"

# US Core STU3 resource types MEDENT supports
_RESOURCE_TYPES = [
    "Patient",
    "AllergyIntolerance",
    "Condition",
    "DiagnosticReport",
    "DocumentReference",
    "Immunization",
    "MedicationRequest",
    "Observation",
    "Procedure",
]

# Observation category filters for clean separation
_OBS_CATEGORIES = ["laboratory", "vital-signs", "social-history"]

TOKEN_CACHE = Path(os.environ.get(
    "MEDENT_TOKEN_CACHE",
    str(Path.home() / ".healthclaw" / "medent_tokens.json")))
CLIENT_CACHE = Path(os.environ.get(
    "MEDENT_CLIENT_CACHE",
    str(Path.home() / ".healthclaw" / "medent_client.json")))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def _ensure_fresh_token(cached: dict) -> dict:
    """Refresh the access token if it has expired. Updates cache in-place."""
    import httpx

    now = datetime.now(timezone.utc).timestamp()
    if cached.get("expires_at", 0) > now:
        return cached  # still valid

    print("access token expired, refreshing ...", file=sys.stderr)

    refresh_token = cached.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "access token expired and no refresh_token — run medent_oauth.py authorize again")

    practice_id = cached.get("practice_id", "").strip()
    if not practice_id:
        raise RuntimeError("practice_id missing from token cache")

    client = _load_json(CLIENT_CACHE)
    client_id = client.get("client_id", os.environ.get("MEDENT_CLIENT_ID", "")).strip()

    refresh_url = f"{_MEDENT_FHIR_BASE}/{practice_id}/token"
    resp = httpx.post(
        refresh_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    expires_in = int(body.get("expires_in", 900))
    cached.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "expires_at": now + expires_in - 60,
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save_json(TOKEN_CACHE, cached)
    print("  token refreshed", file=sys.stderr)
    return cached


def _fhir_get(url: str, params: dict, token: str) -> dict:
    import httpx
    resp = httpx.get(
        url,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        },
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_resource(fhir_base: str, resource_type: str, patient_mrn: str,
                    token: str, count: int = 200) -> list[dict]:
    """Fetch all pages of a resource type for this patient."""
    params: dict = {"_count": count}
    if patient_mrn:
        if resource_type == "Patient":
            params["_id"] = patient_mrn
        else:
            params["patient"] = patient_mrn

    url = f"{fhir_base}/{resource_type}"
    all_entries: list[dict] = []
    page = 0

    while url:
        page += 1
        bundle = _fhir_get(url, params if page == 1 else {}, token)
        entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
        all_entries.extend(entries)

        # Follow Bundle.link[rel=next] for paging
        next_url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                next_url = link.get("url")
                break
        url = next_url  # None stops the loop

    return all_entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant-id", default="ev-personal")
    parser.add_argument("--output", default=None,
                        help="Output path (default: ~/.healthclaw/exports/medent-<date>.json)")
    parser.add_argument("--no-redact", action="store_true",
                        help="Skip PHI redaction (dev/testing only)")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print output JSON")
    args = parser.parse_args()

    # ── Load + refresh token ──────────────────────────────────────────────────
    cached = _load_json(TOKEN_CACHE)
    if not cached:
        print(
            f"error: no token cache at {TOKEN_CACHE}\n"
            "  run: python scripts/medent_oauth.py authorize --practice-id <id>",
            file=sys.stderr,
        )
        return 1

    try:
        cached = _ensure_fresh_token(cached)
    except Exception as exc:
        print(f"error: token refresh failed — {exc}", file=sys.stderr)
        return 1

    access_token = cached["access_token"]
    practice_id = cached.get("practice_id", "").strip()
    patient_mrn = cached.get("patient_mrn", "").strip()

    if not practice_id:
        print("error: practice_id missing from token cache", file=sys.stderr)
        return 1

    fhir_base = f"{_MEDENT_FHIR_BASE}/{practice_id}"
    print(f"MEDENT FHIR base: {fhir_base}", file=sys.stderr)
    print(f"patient MRN: {patient_mrn or '(not in token — querying without filter)'}", file=sys.stderr)
    print(f"tenant: {args.tenant_id}", file=sys.stderr)

    # ── Pull resources ────────────────────────────────────────────────────────
    records: dict[str, list] = {}
    errors: dict[str, str] = {}

    for rtype in _RESOURCE_TYPES:
        print(f"  fetching {rtype} ...", end=" ", file=sys.stderr)
        try:
            entries = _fetch_resource(fhir_base, rtype, patient_mrn, access_token)
            records[rtype] = entries
            print(f"{len(entries)}", file=sys.stderr)
        except Exception as exc:
            errors[rtype] = str(exc)
            print(f"error: {exc}", file=sys.stderr)

    total = sum(len(v) for v in records.values())
    print(f"\nfetched {total} resources across {len(records)} types", file=sys.stderr)

    # ── Redact ────────────────────────────────────────────────────────────────
    redaction_stats: dict = {}

    if not args.no_redact:
        redact_script = Path(__file__).parent / "healthclaw_redact.py"
        if not redact_script.exists():
            redact_script = Path.home() / ".healthclaw" / "healthclaw_redact.py"

        if redact_script.exists():
            sys.path.insert(0, str(redact_script.parent))
            try:
                from healthclaw_redact import redact  # type: ignore
                redacted_records: dict[str, list] = {}
                for rtype, entries in records.items():
                    cleaned = []
                    for entry in entries:
                        redacted_entry, stats = redact(entry)
                        cleaned.append(redacted_entry)
                        for k, v in stats.items():
                            redaction_stats[k] = redaction_stats.get(k, 0) + v
                    redacted_records[rtype] = cleaned
                records = redacted_records
                total_redacted = sum(redaction_stats.values())
                print(f"redacted {total_redacted} PHI fields", file=sys.stderr)
            except Exception as exc:
                print(f"warning: redaction failed — {exc}", file=sys.stderr)
                print("  PHI redaction skipped — output may contain identifiable data",
                      file=sys.stderr)
        else:
            print("warning: healthclaw_redact.py not found — PHI not redacted", file=sys.stderr)
    else:
        print("WARNING: --no-redact active — output contains raw PHI", file=sys.stderr)

    # ── Write output ──────────────────────────────────────────────────────────
    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        exports_dir = Path.home() / ".healthclaw" / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = exports_dir / f"medent-{today}.json"

    output = {
        "records": records,
        "errors": errors,
        "_meta": {
            "source": "medent_fhir",
            "practice_id": practice_id,
            "tenant_id": args.tenant_id,
            "redacted": not args.no_redact,
            "redaction_stats": redaction_stats,
            "total_resources": total,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }

    indent = 2 if args.pretty else None
    out_path.write_text(json.dumps(output, indent=indent, ensure_ascii=False))
    out_path.chmod(0o600)

    print(f"\nOutput: {out_path}", file=sys.stderr)
    print(f"  size: {out_path.stat().st_size:,} bytes", file=sys.stderr)
    if errors:
        print(f"  errors ({len(errors)}): {', '.join(errors.keys())}", file=sys.stderr)

    # Print to stdout for the bot to parse
    print(f"Output: {out_path}")
    print(f"  resources: {total}")
    print(f"  types: {', '.join(f'{k}:{len(v)}' for k, v in records.items() if v)}")
    if errors:
        print(f"  failed: {', '.join(errors.keys())}")
    if redaction_stats:
        print(f"  fields redacted: {sum(redaction_stats.values())}")
    print(f"\nNext: tell your bot `/import {out_path}` to ingest these records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
