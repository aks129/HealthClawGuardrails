"""
Convert a Fasten Health export to a standard FHIR R4 transaction Bundle.

Fasten Health exports use a proprietary envelope:
  { "providers": [{ "name": "...", "fhir": { "Patient": [...], "Observation": [...] } }] }

This script flattens that into a FHIR Bundle with PUT entries, de-duplicating
by (resourceType, id) and keeping the most recently updated version per resource.

Usage:
    python scripts/convert_fasten.py \\
        --input ~/Downloads/health-records-2026-01-15.json \\
        --output healthclaw-bundle-ev-2026.json

    # Merge multiple Fasten exports into one bundle
    python scripts/convert_fasten.py \\
        --input "~/Downloads/health-records-2026-01-15 (1).json" \\
                "~/Downloads/health-records-2026-01-15 (3).json" \\
        --output healthclaw-bundle-ev-2026.json

    # Limit resource types (useful for large exports)
    python scripts/convert_fasten.py \\
        --input ~/Downloads/health-records-2026-01-15.json \\
        --types Patient Condition Observation Immunization MedicationRequest AllergyIntolerance \\
        --output healthclaw-bundle-ev-2026.json
"""

import argparse
import json
import os
import sys


def parse_args():
    p = argparse.ArgumentParser(
        description='Convert Fasten Health export to FHIR transaction Bundle'
    )
    p.add_argument(
        '--input', '-i', nargs='+', required=True,
        help='Path(s) to Fasten Health export JSON file(s)',
    )
    p.add_argument(
        '--output', '-o', default='healthclaw-bundle-ev-2026.json',
        help='Output Bundle path (default: healthclaw-bundle-ev-2026.json)',
    )
    p.add_argument(
        '--types', nargs='*',
        default=[
            'Patient', 'Condition', 'Observation', 'Immunization',
            'MedicationRequest', 'AllergyIntolerance', 'DiagnosticReport',
            'Procedure', 'CarePlan', 'Coverage', 'Encounter',
        ],
        help='Resource types to include (default: clinical set)',
    )
    p.add_argument(
        '--all-types', action='store_true',
        help='Include all resource types (overrides --types)',
    )
    return p.parse_args()


def _last_updated(resource: dict) -> str:
    """Return meta.lastUpdated or empty string for sorting."""
    return resource.get('meta', {}).get('lastUpdated', '')


def load_fasten_export(path: str, allowed_types: set | None) -> dict[tuple, dict]:
    """
    Load a Fasten Health export file.

    Returns:
        dict mapping (resourceType, id) -> resource dict
        (most-recently-updated version wins on collision)
    """
    with open(path) as f:
        data = json.load(f)

    resources: dict[tuple, dict] = {}

    # Fasten format: {"providers": [{"name": "...", "fhir": {...}}]}
    providers = data.get('providers', [])
    if not providers:
        # Might already be a FHIR Bundle
        if data.get('resourceType') == 'Bundle':
            for entry in data.get('entry', []):
                res = entry.get('resource', {})
                rt = res.get('resourceType')
                rid = res.get('id')
                if rt and rid:
                    if allowed_types is None or rt in allowed_types:
                        key = (rt, rid)
                        existing = resources.get(key)
                        if existing is None or _last_updated(res) >= _last_updated(existing):
                            resources[key] = res
        return resources

    for provider in providers:
        pname = provider.get('name', 'unknown')
        fhir = provider.get('fhir', {})
        for resource_type, resource_list in fhir.items():
            if allowed_types is not None and resource_type not in allowed_types:
                continue
            if not isinstance(resource_list, list):
                continue
            for resource in resource_list:
                rid = resource.get('id')
                if not rid:
                    continue
                key = (resource_type, rid)
                existing = resources.get(key)
                if existing is None or _last_updated(resource) >= _last_updated(existing):
                    resources[key] = resource

    return resources


def to_transaction_bundle(resources: dict[tuple, dict]) -> dict:
    """Wrap resources in a FHIR R4 transaction Bundle with PUT entries."""
    entries = []
    for (resource_type, resource_id), resource in sorted(resources.items()):
        entries.append({
            'resource': resource,
            'request': {
                'method': 'PUT',
                'url': f'{resource_type}/{resource_id}',
            },
        })

    return {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': entries,
    }


def summarise(resources: dict[tuple, dict]) -> None:
    counts: dict[str, int] = {}
    for (rt, _) in resources:
        counts[rt] = counts.get(rt, 0) + 1
    print('Resource counts:')
    for rt, n in sorted(counts.items()):
        print(f'  {rt}: {n}')
    print(f'  TOTAL: {sum(counts.values())}')


def main():
    args = parse_args()
    allowed_types = None if args.all_types else set(args.types)

    merged: dict[tuple, dict] = {}
    for path in args.input:
        path = os.path.expanduser(path)
        print(f'Loading {os.path.basename(path)} ...')
        resources = load_fasten_export(path, allowed_types)
        for key, res in resources.items():
            existing = merged.get(key)
            if existing is None or _last_updated(res) >= _last_updated(existing):
                merged[key] = res
        print(f'  {len(resources)} resources loaded')

    print()
    summarise(merged)
    print()

    bundle = to_transaction_bundle(merged)
    with open(args.output, 'w') as f:
        json.dump(bundle, f, indent=2)
    print(f'Written: {args.output} ({len(bundle["entry"])} entries)')


if __name__ == '__main__':
    main()
