"""Which `meta.profile` claims a resource may carry out (FHI-52).

A profile claim is a canonical URL, and an upstream can put anything in that
field: a URL with a patient's name in its path, an object, a number. So a
claim leaves the engine only if it is on a closed list, and nothing is copied
through because an upstream wrote it.

- `CANONICALS` maps each allowed StructureDefinition URL to its IG. Grow it
  one row at a time with a test, never by prefix.
- `VERSIONS` lists the published versions per IG. A claim `url|version`
  keeps its version only if that IG published it; otherwise the bare URL is
  emitted. Anything whose base is not in `CANONICALS` is dropped.

We stamp no claims (validate-then-claim is deferred): this only decides which
claims a record already carries are let through, so an unprofiled record
stays unprofiled. The filter is idempotent, so two layers can apply it, as
with the record-source tag (r6/record_source.py): `to_fhir_json` for local
exits that never redact, and redaction for every payload, upstream included.
"""

_US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'

CANONICALS = {
    _US_CORE + 'us-core-patient': 'us-core',
    _US_CORE + 'us-core-condition-problems-health-concerns': 'us-core',
    _US_CORE + 'us-core-condition-encounter-diagnosis': 'us-core',
    _US_CORE + 'us-core-allergyintolerance': 'us-core',
    _US_CORE + 'us-core-medicationrequest': 'us-core',
    _US_CORE + 'us-core-immunization': 'us-core',
    _US_CORE + 'us-core-coverage': 'us-core',
    # CARIN Digital Insurance Card: not US Core, but the check-in request
    # accepts it for Coverage (owner ruling, FHI-52).
    'http://hl7.org/fhir/us/insurance-card/StructureDefinition/'
    'C4DIC-Coverage': 'c4dic',
}

VERSIONS = {
    'us-core': frozenset({'3.1.1', '4.0.0', '5.0.1', '6.1.0', '7.0.0',
                          '8.0.0'}),
    'c4dic': frozenset({'1.0.0', '1.1.0'}),
}


def keep_profiles(value):
    """The allow-listed claims in `value`, de-duplicated and sorted.

    A non-list, and any element that is not a string, gives nothing. Each
    claim is split once on `|`; the base must match a row exactly (no case
    folding, no trimming), and a version its IG did not publish is dropped
    in favour of the bare URL."""
    if not isinstance(value, list):
        return []
    kept = set()
    for claim in value:
        if not isinstance(claim, str):
            continue
        base, sep, version = claim.partition('|')
        ig = CANONICALS.get(base)
        if ig is None:
            continue
        kept.add(claim if sep and version in VERSIONS[ig] else base)
    return sorted(kept)


def filter_meta(meta):
    """Replace `meta.profile` with its allow-listed claims, in place; an
    empty result removes the key."""
    if not isinstance(meta, dict) or 'profile' not in meta:
        return
    kept = keep_profiles(meta['profile'])
    if kept:
        meta['profile'] = kept
    else:
        meta.pop('profile')


def filter_all(obj):
    """`filter_meta` on every `meta` dict at any depth, in place."""
    if isinstance(obj, list):
        for item in obj:
            filter_all(item)
    elif isinstance(obj, dict):
        filter_meta(obj.get('meta'))
        for value in obj.values():
            filter_all(value)
