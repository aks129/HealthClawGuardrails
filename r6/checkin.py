"""The `checkin` export: SMART Health Check-in's closed tables (FHI-53).

A check-in hands a clinic only the items the patient ticked, once. The engine
side is a short-lived, single-use token minted for an exact set of resource
types, and a `$share-bundle` profile that exports only those types, for one
named patient, and only rows that carry an allow-listed US Core or C4DIC
profile claim (r6/us_core_profiles.py). What each released resource keeps is
r6/access.py `_checkin_profile`.

One function, `operation`, builds the token's `op` claim for both the mint
and the export, so the two cannot disagree on spelling.
"""

#: The six items a check-in may ask for. Organization is deliberately absent:
#: payor Organizations are fetched by the ids the exported Coverage
#: references, never by type, which would export every one in the tenant.
CHECKIN_TYPES = frozenset({
    'Patient', 'Condition', 'AllergyIntolerance', 'MedicationRequest',
    'Immunization', 'Coverage',
})

#: The `aud` claim of a check-in token. No other mint sets it, so the cached
#: general CareAgents token (no `aud`) is refused by this profile.
AUDIENCE = 'checkin-export'

#: Fixed; the caller does not choose it.
TOKEN_TTL_SECONDS = 120

#: A serialised bundle above this is refused (413), never truncated: a short
#: list read as complete is the worse failure. Leaves headroom for transport
#: overhead under the wallet's 512 KB.
MAX_BUNDLE_BYTES = 400_000

#: The agent id the export audits under. Fixed words: nothing the requester
#: wrote reaches the audit row.
AUDIT_AGENT = 'careagents-checkin'

V2_0203 = 'http://terminology.hl7.org/CodeSystem/v2-0203'
COVERAGE_CLASS = 'http://terminology.hl7.org/CodeSystem/coverage-class'
NAIC_SYSTEM = 'urn:oid:2.16.840.1.113883.6.300'

_US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/'

#: Which claims make a row of each type exportable. A claim for another
#: type does not count: a Condition claiming us-core-patient is unprofiled.
CLAIMS_BY_TYPE = {
    'Patient': frozenset({_US_CORE + 'us-core-patient'}),
    'Condition': frozenset({
        _US_CORE + 'us-core-condition',
        _US_CORE + 'us-core-condition-problems-health-concerns',
        _US_CORE + 'us-core-condition-encounter-diagnosis',
    }),
    'AllergyIntolerance': frozenset({_US_CORE + 'us-core-allergyintolerance'}),
    'MedicationRequest': frozenset({_US_CORE + 'us-core-medicationrequest'}),
    'Immunization': frozenset({_US_CORE + 'us-core-immunization'}),
    'Coverage': frozenset({
        _US_CORE + 'us-core-coverage',
        'http://hl7.org/fhir/us/insurance-card/StructureDefinition/'
        'C4DIC-Coverage',
    }),
}


#: The `op` the export checks when the request names no valid set of types.
#: No mint produces it (parse_types refuses an empty list), so the grant
#: refuses such a request exactly as it refuses any other mismatch.
NO_OPERATION = 'checkin:'


def parse_types(value):
    """The requested types as a sorted list, or None if the request is not
    a non-empty JSON array of CHECKIN_TYPES strings."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(t, str) and t in CHECKIN_TYPES for t in value):
        return None
    return sorted(set(value))


def operation(types):
    """The `op` claim for exactly this set of types."""
    return 'checkin:' + ','.join(sorted(set(types)))


def has_claim(resource):
    """True when the resource carries an allow-listed claim for its own type.

    Reads the profile list `to_fhir_json` already filtered; the base before
    any `|version` is what is compared."""
    wanted = CLAIMS_BY_TYPE.get(resource.get('resourceType'))
    meta = resource.get('meta')
    claims = meta.get('profile') if isinstance(meta, dict) else None
    if not wanted or not isinstance(claims, list):
        return False
    return any(isinstance(c, str) and c.partition('|')[0] in wanted
               for c in claims)
