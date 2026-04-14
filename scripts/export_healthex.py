"""
Export personal health data from the HealthClaw local FHIR store to a
de-identified, curatr-pre-tagged FHIR R4 transaction Bundle.

Pulls all clinical resource types via the Flask REST API (full pagination),
applies patient-controlled de-identification, flags known Curatr issue
patterns without modifying the stored data, and writes the bundle to
exports/healthex-<date>.json.

Usage:
    # Basic export for a tenant
    python scripts/export_healthex.py --tenant-id ev-personal

    # Export and immediately import into a second tenant
    python scripts/export_healthex.py \\
        --tenant-id ev-personal \\
        --import \\
        --import-tenant ev-archive \\
        --step-up-secret $STEP_UP_SECRET

    # Export specific resource types only
    python scripts/export_healthex.py \\
        --tenant-id ev-personal \\
        --types Condition Observation AllergyIntolerance Immunization

    # Export to a specific output path
    python scripts/export_healthex.py \\
        --tenant-id ev-personal \\
        --output /tmp/my-records.json

De-identification applied (HIPAA Safe Harbor subset):
    - Patient.name       → removed
    - Patient.address    → removed
    - Patient.telecom    → removed
    - Patient.identifier → EHR-issued MRNs/EPICs removed; healthclaw ID injected
    - Patient.photo      → removed
    - Patient.contact    → removed
    - Patient.birthDate  → PRESERVED (patient-controlled export)
    - All other fields   → unchanged

Curatr pre-tags injected as extensions (not modifications):
    - smoking_contradiction  LOINC 72166-2 with conflicting SNOMED answers
    - h_flag_titer           Lab with interpretation H/HH or text flag
    - missing_result         Observation with no value[x] element
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import date

import requests

DEFAULT_BASE_URL = "http://localhost:5000/r6/fhir"
DEFAULT_TYPES = [
    "Patient",
    "Condition",
    "Observation",
    "AllergyIntolerance",
    "Immunization",
    "MedicationRequest",
    "Procedure",
    "DiagnosticReport",
    "CarePlan",
    "Coverage",
    "Encounter",
]

# FHIR pagination page size — 200 is the server max per CLAUDE.md
_PAGE_SIZE = 200

# LOINC code for tobacco smoking status
_LOINC_SMOKING = "72166-2"

# SNOMED codes that mean "current smoker" (partial — common codes)
_SNOMED_CURRENT_SMOKER = {
    "77176002",   # Smoker
    "65568007",   # Cigarette smoker
    "8517006",    # Ex-smoker
    "449868002",  # Smokes tobacco daily
    "428041000124106",  # Occasional tobacco smoker
}
_SNOMED_NEVER_SMOKER = {
    "266919005",  # Never smoked tobacco
    "221000119102",  # Never smoked tobacco
}

# Curatr pre-tag extension URL
_PRETAG_URL = "https://healthclaw.example.org/fhir/StructureDefinition/curatr-pretag"

# Identifier system for healthclaw synthetic IDs
_HC_ID_SYSTEM = "urn:healthclaw:patient"

# Systems that are EHR-proprietary and should be stripped on de-identification
_STRIP_IDENTIFIER_SYSTEMS = {
    # Epic
    "http://open.epic.com/FHIR/StructureDefinition/patient-dstu2-fhir-id",
    "http://open.epic.com/FHIR/StructureDefinition/patient-fhir-id",
    # OID-based internal systems (Epic, Cerner, etc.)
}


def build_parser():
    p = argparse.ArgumentParser(
        description="Export personal health data from HealthClaw FHIR store"
    )
    p.add_argument(
        "--tenant-id",
        default="desktop-demo",
        help="Tenant whose data to export (default: desktop-demo)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("FHIR_LOCAL_BASE_URL", DEFAULT_BASE_URL),
        help=f"HealthClaw FHIR base URL (default: {DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--types",
        nargs="*",
        default=DEFAULT_TYPES,
        help="Resource types to export (default: full clinical set)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Output bundle path (default: exports/healthex-<date>.json)",
    )
    p.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="After export, call import_healthex.py to ingest the bundle",
    )
    p.add_argument(
        "--import-tenant",
        default=None,
        help="Tenant to import into (default: same as --tenant-id)",
    )
    p.add_argument(
        "--step-up-secret",
        default=os.environ.get("STEP_UP_SECRET", ""),
        help="HMAC secret for import step-up token (required with --import)",
    )
    p.add_argument(
        "--no-deidentify",
        action="store_true",
        help="Skip de-identification (keeps Patient name/address/telecom)",
    )
    p.add_argument(
        "--no-pretag",
        action="store_true",
        help="Skip Curatr pre-tagging",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resource counts without writing output",
    )
    return p


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_all_resources(
    base_url: str,
    resource_type: str,
    tenant_id: str,
) -> list[dict]:
    """
    Fetch all resources of a type for a tenant, following FHIR pagination links.

    Returns a flat list of resource dicts (not entries).
    """
    headers = {"X-Tenant-ID": tenant_id, "Accept": "application/fhir+json"}
    params = {"_count": _PAGE_SIZE, "_sort": "-_lastUpdated"}

    url = f"{base_url.rstrip('/')}/{resource_type}"
    resources = []

    while url:
        bundle = _get(url, headers, params if "?" not in url else None)
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            if res:
                resources.append(res)

        # Follow pagination
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                url = link.get("url")
                break

    return resources


# ---------------------------------------------------------------------------
# De-identification
# ---------------------------------------------------------------------------

def _strip_oid_identifiers(identifiers: list[dict]) -> list[dict]:
    """Remove EHR-issued identifiers; keep or inject healthclaw ID."""
    kept = []
    has_hc = False
    for ident in identifiers:
        system = ident.get("system", "")
        id_type = ident.get("type", {}).get("text", "")
        # Keep healthclaw-issued ones
        if system == _HC_ID_SYSTEM:
            has_hc = True
            kept.append(ident)
            continue
        # Drop OID-namespaced systems (urn:oid:...) — EHR-internal
        if system.startswith("urn:oid:"):
            continue
        # Drop known Epic FHIR ID systems
        if system in _STRIP_IDENTIFIER_SYSTEMS:
            continue
        # Drop by type label (MRN, EPIC, FHIR, CEID, EID, EMPI, etc.)
        if id_type.upper() in {
            "MRN", "EPIC", "FHIR", "FHIR STU3", "CEID", "EID", "EMPI",
            "WEPIC", "MEDIPAC", "INTERNAL", "WPRINTERNAL", "EXTERNAL",
        }:
            continue
        kept.append(ident)
    return kept, has_hc


def deidentify_resource(resource: dict, hc_patient_id: str | None = None) -> dict:
    """
    Apply patient-controlled de-identification to a FHIR resource.

    Patient demographics (name, address, telecom, photo, contact, EHR MRNs)
    are removed. birthDate and gender are preserved.

    A healthclaw synthetic identifier is injected on Patient resources.
    """
    rt = resource.get("resourceType")
    res = dict(resource)  # shallow copy; deep fields mutated below

    if rt != "Patient":
        return res

    # Strip PII fields
    for field in ("name", "address", "telecom", "photo", "contact"):
        res.pop(field, None)

    # Clean identifiers — strip EHR internals, keep/add healthclaw ID
    raw_idents = res.get("identifier", [])
    kept, has_hc = _strip_oid_identifiers(raw_idents)

    if not has_hc:
        synth_id = hc_patient_id or str(uuid.uuid4())
        kept.insert(0, {
            "use": "secondary",
            "system": _HC_ID_SYSTEM,
            "value": synth_id,
        })

    res["identifier"] = kept
    return res


# ---------------------------------------------------------------------------
# Curatr pre-tagging
# ---------------------------------------------------------------------------

def _add_pretag(resource: dict, tag_code: str, tag_display: str) -> None:
    """Inject a curatr pre-tag extension onto a resource (in-place)."""
    ext = resource.setdefault("extension", [])
    ext.append({
        "url": _PRETAG_URL,
        "valueCodeableConcept": {
            "coding": [{
                "system": _PRETAG_URL,
                "code": tag_code,
                "display": tag_display,
            }],
            "text": tag_display,
        },
    })


def _has_value(resource: dict) -> bool:
    """Return True if an Observation has any value[x] element."""
    for key in resource:
        if key.startswith("value"):
            return True
    return False


def _smoking_observations(observations: list[dict]) -> list[dict]:
    """Return all tobacco smoking status observations."""
    return [
        obs for obs in observations
        if any(
            c.get("code") == _LOINC_SMOKING
            for c in obs.get("code", {}).get("coding", [])
        )
    ]


def _snomed_smoking_code(obs: dict) -> str | None:
    """Return the SNOMED code from valueCodeableConcept, or None."""
    for coding in obs.get("valueCodeableConcept", {}).get("coding", []):
        if "snomed" in coding.get("system", "").lower():
            return coding.get("code")
    return None


def _has_h_flag(obs: dict) -> bool:
    """
    Return True if an Observation carries an H/HH high-value flag.

    Checks interpretation.coding[].code and valueString for common patterns.
    """
    for interp in obs.get("interpretation", []):
        for c in interp.get("coding", []):
            if c.get("code") in ("H", "HH", "HU"):
                return True
    vs = obs.get("valueString", "")
    if isinstance(vs, str) and (">>>" in vs or vs.strip().startswith("H")):
        return True
    return False


def pretag_curatr_issues(resources: list[dict]) -> list[dict]:
    """
    Scan resources for known Curatr issue patterns and inject pre-tag extensions.

    Returns the same list with extensions added where issues are detected.
    Does NOT modify stored data — extensions exist only in the export bundle.
    """
    observations = [r for r in resources if r.get("resourceType") == "Observation"]

    # --- 1. Smoking contradiction ---
    smoking_obs = _smoking_observations(observations)
    if len(smoking_obs) >= 2:
        codes = [_snomed_smoking_code(o) for o in smoking_obs]
        has_never = any(c in _SNOMED_NEVER_SMOKER for c in codes if c)
        has_ever = any(
            c in _SNOMED_CURRENT_SMOKER or (c and c not in _SNOMED_NEVER_SMOKER)
            for c in codes if c
        )
        if has_never and has_ever:
            for obs in smoking_obs:
                _add_pretag(
                    obs,
                    "smoking_contradiction",
                    "Conflicting smoking status records detected — review recommended",
                )

    # --- 2. H-flag antibody titers / high-value labs ---
    for obs in observations:
        if _has_h_flag(obs):
            _add_pretag(
                obs,
                "h_flag_titer",
                "High-value flag present — clinical review recommended",
            )

    # --- 3. Missing result value ---
    for obs in observations:
        if not _has_value(obs):
            _add_pretag(
                obs,
                "missing_result",
                "Observation has no result value — may indicate pending or cancelled result",
            )

    return resources


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------

def to_transaction_bundle(resources: list[dict]) -> dict:
    """Wrap resources in a FHIR R4 transaction Bundle with PUT entries."""
    entries = []
    seen: set[tuple] = set()
    for resource in resources:
        rt = resource.get("resourceType", "Unknown")
        rid = resource.get("id", "")
        key = (rt, rid)
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "resource": resource,
            "request": {
                "method": "PUT",
                "url": f"{rt}/{rid}" if rid else rt,
            },
        })
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": entries,
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarise(resources: list[dict]) -> None:
    from collections import Counter
    counts: Counter = Counter(r.get("resourceType", "?") for r in resources)
    pretag_counts: Counter = Counter()
    for r in resources:
        for ext in r.get("extension", []):
            if ext.get("url") == _PRETAG_URL:
                code = (
                    ext.get("valueCodeableConcept", {})
                       .get("coding", [{}])[0]
                       .get("code", "?")
                )
                pretag_counts[code] += 1

    print("Resource counts:")
    for rt, n in sorted(counts.items()):
        print(f"  {rt}: {n}")
    print(f"  TOTAL: {sum(counts.values())}")

    if pretag_counts:
        print("\nCuratr pre-tags:")
        for tag, n in sorted(pretag_counts.items()):
            print(f"  {tag}: {n}")


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------

def run_import(bundle_path: str, import_tenant: str, step_up_secret: str, base_url: str) -> None:
    """Call import_healthex.py as a subprocess."""
    script = os.path.join(os.path.dirname(__file__), "import_healthex.py")
    cmd = [
        sys.executable, script,
        "--bundle-file", bundle_path,
        "--tenant-id", import_tenant,
        "--step-up-secret", step_up_secret,
        "--base-url", base_url,
    ]
    print(f"\nRunning import: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(f"Import failed with exit code {result.returncode}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = build_parser().parse_args()
    base_url = args.base_url.rstrip("/")
    today = date.today().isoformat()

    # Resolve output path
    if args.output:
        output_path = args.output
    else:
        os.makedirs("exports", exist_ok=True)
        output_path = f"exports/healthex-{today}.json"

    print(f"Exporting tenant '{args.tenant_id}' from {base_url}")
    print(f"Resource types: {', '.join(args.types)}")
    print()

    all_resources: list[dict] = []

    for resource_type in args.types:
        try:
            resources = fetch_all_resources(base_url, resource_type, args.tenant_id)
            print(f"  {resource_type}: {len(resources)}")
            all_resources.extend(resources)
        except requests.HTTPError as exc:
            print(f"  {resource_type}: HTTP {exc.response.status_code} — skipped")
        except requests.ConnectionError:
            sys.exit(
                f"\nERROR: Cannot connect to {base_url}\n"
                "Make sure the HealthClaw stack is running (python main.py or docker-compose up)"
            )

    print(f"\nTotal resources fetched: {len(all_resources)}")

    # De-identification
    if not args.no_deidentify:
        hc_id = str(uuid.uuid4())
        all_resources = [deidentify_resource(r, hc_id) for r in all_resources]
        print("De-identification applied (name/address/telecom/EHR identifiers removed)")

    # Curatr pre-tagging
    if not args.no_pretag:
        all_resources = pretag_curatr_issues(all_resources)
        from collections import Counter
        tag_counts = Counter(
            ext.get("valueCodeableConcept", {}).get("coding", [{}])[0].get("code", "?")
            for r in all_resources
            for ext in r.get("extension", [])
            if ext.get("url") == _PRETAG_URL
        )
        if tag_counts:
            print(f"Curatr pre-tags applied: {dict(tag_counts)}")

    if args.dry_run:
        print("\nDry run — not writing output.")
        print()
        summarise(all_resources)
        return

    bundle = to_transaction_bundle(all_resources)

    with open(output_path, "w") as f:
        json.dump(bundle, f, indent=2)

    print(f"\nWritten: {output_path} ({len(bundle['entry'])} entries)")
    print()
    summarise(all_resources)

    if args.do_import:
        import_tenant = args.import_tenant or args.tenant_id
        if not args.step_up_secret:
            sys.exit(
                "\nERROR: --step-up-secret (or STEP_UP_SECRET env var) is required with --import"
            )
        run_import(output_path, import_tenant, args.step_up_secret, base_url)


if __name__ == "__main__":
    main()
