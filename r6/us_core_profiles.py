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

The filter stamps no claims: it only decides which claims a record already
carries are let through, so an unprofiled record stays unprofiled. It is
idempotent, so two layers can apply it, as with the record-source tag
(r6/record_source.py): `to_fhir_json` for local exits that never redact, and
redaction for every payload, upstream included.

`classify` is the one place a claim is earned (#1058, owner ruling
2026-10-26). Real vendor feeds send no `meta.profile`, so the check-in and
ktc exports (r6/checkin.py collect) check an unclaimed record of the six
check-in types against a light US Core table and claim only what passes:
required elements and the code systems US Core binds each code to, per
type. It is not a validator. It reads the record as it will leave (after the
export profile and the dangling-reference drop), so it never vouches for an
element the export strips. A record that fails gets a reason code, and
HELD_REASONS says it in plain words; nothing of the record is in either.

r6/validator.py's per-type checks are not reused: they test base FHIR, not
US Core (Patient.gender and Condition.category go unchecked), and the
AllergyIntolerance one requires clinicalStatus even on an entered-in-error
record, where FHIR's ait-2 forbids it.
"""

from r6 import terminology as _terminology

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


# --- validate-then-claim (#1058) --------------------------------------------

_HL7 = 'http://terminology.hl7.org/CodeSystem/'
_CONDITION_CATEGORY = _HL7 + 'condition-category'
_US_CORE_CATEGORY = 'http://hl7.org/fhir/us/core/CodeSystem/condition-category'

#: Why a record stayed home, in words a patient reads. Fixed: keyed by the
#: reason code `classify` returns, and nothing of the record is in them.
HELD_REASONS = {
    'no-identifier': 'It has no record number from the place that keeps it.',
    'no-name': 'It does not list a name.',
    'no-gender': 'It does not list a sex or gender.',
    'no-standard-code': ('It is not coded in a standard way that a clinic '
                         'system can read.'),
    'no-category': ('It is not marked as a problem, a health concern or a '
                    'visit diagnosis.'),
    'status-unclear': 'Its status is missing or does not add up.',
    'missing-details': ('It is missing details a clinic system needs, such '
                        'as a date or a status.'),
    'no-insurer': "The insurance company's own record is not on file.",
    'entered-in-error': ('The place that keeps it marked it as entered in '
                         'error.'),
}

#: The code systems US Core binds each coded element to (SNOMED and the ICD
#: families for Condition.code; the substance systems for an allergen;
#: RxNorm for a medicine; CVX or NDC for a vaccine). A record coded only
#: outside them is not claimed: the binding is extensible, and whether a
#: standard code applied is not something we can check.
_CODE_SYSTEMS = {
    'Condition': frozenset({_terminology.SNOMED, _terminology.ICD10,
                            _terminology.ICD9}),
    'AllergyIntolerance': frozenset({
        _terminology.RXNORM, _terminology.SNOMED,
        'http://hl7.org/fhir/ndfrt', 'http://fdasis.nlm.nih.gov'}),
    'MedicationRequest': frozenset({_terminology.RXNORM}),
    'Immunization': frozenset({_terminology.CVX,
                               'http://hl7.org/fhir/sid/ndc'}),
}

_GENDERS = frozenset({'male', 'female', 'other', 'unknown'})
_MEDREQ_STATUS = frozenset({'active', 'on-hold', 'cancelled', 'completed',
                            'entered-in-error', 'stopped', 'draft',
                            'unknown'})
_MEDREQ_INTENT = frozenset({'proposal', 'plan', 'order', 'original-order',
                            'reflex-order', 'filler-order', 'instance-order',
                            'option'})
_IMMUNIZATION_STATUS = frozenset({'completed', 'entered-in-error',
                                  'not-done'})
_COVERAGE_STATUS = frozenset({'active', 'cancelled', 'draft',
                              'entered-in-error'})
_VERIFICATION = {
    'condition': _HL7 + 'condition-ver-status',
    'allergyintolerance': _HL7 + 'allergyintolerance-verification',
}


def _codes(concept, system=None):
    """The codes of `concept`'s codings, under `system` when given."""
    codings = concept.get('coding') if isinstance(concept, dict) else None
    return {c['code'] for c in codings or []
            if isinstance(c, dict) and isinstance(c.get('code'), str)
            and c['code'] and (system is None or _terminology.canonical_system(
                c.get('system')) == system)}


def _coded_in(concept, systems):
    return any(_codes(concept, s) for s in systems)


def _has_ref(value):
    return isinstance(value, dict) and isinstance(value.get('reference'), str)


def _status_fits(res, kind):
    """FHIR ait-1/ait-2 (AllergyIntolerance) and con-5 (Condition):
    clinicalStatus is absent when the record is entered-in-error, and for
    an allergy present otherwise."""
    erroneous = 'entered-in-error' in _codes(res.get('verificationStatus'),
                                             _VERIFICATION[kind])
    has_status = bool(_codes(res.get('clinicalStatus')))
    if erroneous:
        return not has_status
    return has_status or kind == 'condition'


def _patient(res):
    if not any(isinstance(i, dict) and isinstance(i.get('system'), str)
               and isinstance(i.get('value'), str) and i['value']
               for i in res.get('identifier') or []):
        return None, 'no-identifier'
    if not any(isinstance(n, dict) and (n.get('family') or n.get('given'))
               for n in res.get('name') or []):
        return None, 'no-name'
    if res.get('gender') not in _GENDERS:
        return None, 'no-gender'
    return _US_CORE + 'us-core-patient', None


def _condition(res):
    if not _coded_in(res.get('code'), _CODE_SYSTEMS['Condition']):
        return None, 'no-standard-code'
    categories = set()
    for cat in res.get('category') or []:
        categories |= _codes(cat, _CONDITION_CATEGORY)
        categories |= _codes(cat, _US_CORE_CATEGORY)
    if categories & {'problem-list-item', 'health-concern'}:
        claim = _US_CORE + 'us-core-condition-problems-health-concerns'
    elif 'encounter-diagnosis' in categories:
        claim = _US_CORE + 'us-core-condition-encounter-diagnosis'
    else:
        return None, 'no-category'
    if not _status_fits(res, 'condition'):
        return None, 'status-unclear'
    if not _has_ref(res.get('subject')):
        return None, 'missing-details'
    return claim, None


def _allergy(res):
    if not _coded_in(res.get('code'), _CODE_SYSTEMS['AllergyIntolerance']):
        return None, 'no-standard-code'
    if not _status_fits(res, 'allergyintolerance'):
        return None, 'status-unclear'
    if not _has_ref(res.get('patient')):
        return None, 'missing-details'
    return _US_CORE + 'us-core-allergyintolerance', None


def _medication_request(res):
    if not _coded_in(res.get('medicationCodeableConcept'),
                     _CODE_SYSTEMS['MedicationRequest']):
        return None, 'no-standard-code'
    if (res.get('status') not in _MEDREQ_STATUS
            or res.get('intent') not in _MEDREQ_INTENT
            or not _has_ref(res.get('subject'))):
        return None, 'missing-details'
    return _US_CORE + 'us-core-medicationrequest', None


def _immunization(res):
    if not _coded_in(res.get('vaccineCode'), _CODE_SYSTEMS['Immunization']):
        return None, 'no-standard-code'
    if (res.get('status') not in _IMMUNIZATION_STATUS
            or not _has_ref(res.get('patient'))
            or not (isinstance(res.get('occurrenceDateTime'), str)
                    or isinstance(res.get('occurrenceString'), str))
            or not isinstance(res.get('primarySource'), bool)):
        return None, 'missing-details'
    return _US_CORE + 'us-core-immunization', None


def _coverage(res):
    if not any(_has_ref(p) for p in res.get('payor') or []):
        return None, 'no-insurer'
    if (res.get('status') not in _COVERAGE_STATUS
            or not _has_ref(res.get('beneficiary'))
            or not _codes(res.get('relationship'),
                          _HL7 + 'subscriber-relationship')):
        return None, 'missing-details'
    return _US_CORE + 'us-core-coverage', None


def entered_in_error(resource):
    """True when the record's source withdrew it: `verificationStatus`
    entered-in-error on a Condition or AllergyIntolerance, `status`
    entered-in-error on anything else. Read on any coding system, so a
    misspelt system does not let one through. Refuted is not withdrawn
    (a finding ruled out), and is not matched here."""
    if not isinstance(resource, dict):
        return False
    if resource.get('resourceType') in ('Condition', 'AllergyIntolerance'):
        return 'entered-in-error' in _codes(resource.get('verificationStatus'))
    return resource.get('status') == 'entered-in-error'


#: The references each type requires: the ones a dangling-reference drop
#: could take away. The record's own patient is never dropped.
_REQUIRED_REFERENCES = {
    'Condition': (('subject', 'missing-details'),),
    'AllergyIntolerance': (('patient', 'missing-details'),),
    'MedicationRequest': (('subject', 'missing-details'),),
    'Immunization': (('patient', 'missing-details'),),
    'Coverage': (('payor', 'no-insurer'), ('beneficiary', 'missing-details')),
}


def lost_reference(resource):
    """The reason code when `resource` lacks a reference its type requires,
    else None. The re-check for a record that arrived claimed and lost a
    reference to the dangling-reference drop: the claim was the upstream's,
    and this checks only what our drop could have broken."""
    for key, reason in _REQUIRED_REFERENCES.get(
            resource.get('resourceType'), ()):
        value = resource.get(key)
        items = value if isinstance(value, list) else [value]
        if not any(_has_ref(i) for i in items):
            return reason
    return None


_CHECKS = {
    'Patient': _patient,
    'Condition': _condition,
    'AllergyIntolerance': _allergy,
    'MedicationRequest': _medication_request,
    'Immunization': _immunization,
    'Coverage': _coverage,
}


def classify(resource):
    """(claim, None) when `resource` passes the light US Core check for its
    type, else (None, reason), reason a HELD_REASONS key.

    `resource` is the record as it will leave: profiled, references outside
    the bundle already dropped. The claim is the bare canonical, since the
    check is not tied to one US Core version. A type outside the six is
    never claimed."""
    check = (_CHECKS.get(resource.get('resourceType'))
             if isinstance(resource, dict) else None)
    if check is None:
        return None, 'missing-details'
    return check(resource)
