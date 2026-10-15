"""Which `meta.profile` claims a resource may carry out (FHI-52).

A profile claim is a canonical URL, and an upstream can put anything in that
field: a URL with a patient's name in its path, an object, a number. So a
claim leaves the engine only if it is on a closed list, and nothing is copied
through because an upstream wrote it.

- `CANONICALS` maps each allowed StructureDefinition URL to the versions
  that published it. Grow it one row at a time with a test, never by prefix.
- A claim `url|version` keeps its version only if that version published
  that canonical; otherwise the bare URL is emitted. Anything whose base is
  not in `CANONICALS` is dropped.

We stamp no claims (validate-then-claim is deferred): this only decides which
claims a record already carries are let through, so an unprofiled record
stays unprofiled. The filter is idempotent, so two layers can apply it, as
with the record-source tag (r6/record_source.py): `to_fhir_json` for local
exits that never redact, and redaction for every payload, upstream included.
"""

_US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'

VERSIONS = {
    'us-core': frozenset({'3.1.1', '4.0.0', '5.0.1', '6.1.0', '7.0.0',
                          '8.0.0'}),
    'c4dic': frozenset({'1.0.0', '1.1.0'}),
}

_ALL = VERSIONS['us-core']
# US Core 5.0.1 split Condition in two; 3.1.1 and 4.0.0 had one profile.
_SPLIT_CONDITION = frozenset({'5.0.1', '6.1.0', '7.0.0', '8.0.0'})
_LEGACY_CONDITION = frozenset({'3.1.1', '4.0.0'})
# us-core-coverage first appeared in 6.0.0.
_COVERAGE = frozenset({'6.1.0', '7.0.0', '8.0.0'})

# Each canonical with the versions that published it: a claim pairing a
# canonical with any other version is reduced to the bare URL.
CANONICALS = {
    _US_CORE + 'us-core-patient': _ALL,
    _US_CORE + 'us-core-condition': _LEGACY_CONDITION,
    _US_CORE + 'us-core-condition-problems-health-concerns': _SPLIT_CONDITION,
    _US_CORE + 'us-core-condition-encounter-diagnosis': _SPLIT_CONDITION,
    _US_CORE + 'us-core-allergyintolerance': _ALL,
    _US_CORE + 'us-core-medicationrequest': _ALL,
    _US_CORE + 'us-core-immunization': _ALL,
    _US_CORE + 'us-core-coverage': _COVERAGE,
    # Payor and prescriber references in a check-in (owner ruling, FHI-52).
    _US_CORE + 'us-core-organization': _ALL,
    _US_CORE + 'us-core-practitioner': _ALL,
    # CARIN Digital Insurance Card: not US Core, but the check-in request
    # accepts it for Coverage (owner ruling, FHI-52).
    'http://hl7.org/fhir/us/insurance-card/StructureDefinition/'
    'C4DIC-Coverage': VERSIONS['c4dic'],
}


def keep_profiles(value):
    """The allow-listed claims in `value`, de-duplicated and sorted.

    A non-list, and any element that is not a string, gives nothing. Each
    claim is split once on `|`; the base must match a row exactly (no case
    folding, no trimming), and a version that did not publish that canonical
    is dropped in favour of the bare URL."""
    if not isinstance(value, list):
        return []
    kept = set()
    for claim in value:
        if not isinstance(claim, str):
            continue
        base, sep, version = claim.partition('|')
        published = CANONICALS.get(base)
        if published is None:
            continue
        kept.add(claim if sep and version in published else base)
    return sorted(kept)


def filter_meta(meta):
    """Replace `meta.profile` with its allow-listed claims, in place; an
    empty result removes the key.

    Every `_`-prefixed companion element in `meta` goes too, `_profile`
    first among them: it holds an `id` and `extension` per claim, which is
    upstream text, and after the sort it would describe the wrong claim.
    We rebuild none of them."""
    if not isinstance(meta, dict):
        return
    for key in [k for k in meta if isinstance(k, str) and k.startswith('_')]:
        meta.pop(key)
    if 'profile' not in meta:
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
