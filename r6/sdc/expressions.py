"""FHIRPath evaluation for SDC populate/extract.

Thin wrapper over fhirpathpy. Evaluation failures return None rather than
raising, so a single bad expression in a Questionnaire never aborts the
whole populate/extract run (the caller records an issue instead).

THE %patient PROJECTION — read this before widening `_PROJECTED_*` below.

`build_context` does NOT put the stored Patient into the FHIRPath
environment. It puts a NEW dict carrying only the elements an intake form is
entitled to ask for (council ruling D10): name.given, name.family, birthDate,
gender, telecom (phone/email), address (line/city/state/postalCode). An
expression cannot reach what is not in the object, so `%patient.identifier`
and `%patient.photo` resolve to nothing without this module ever inspecting
the expression text.

That is deliberate, and it is the whole design. A denylist over FHIRPath
source — "reject an expression mentioning `identifier`" — is one `where()`,
one `descendants()`, one alias away from being wrong, and it would fail open
(an expression it did not understand would still run against the whole
record). A projection fails closed by construction: the only way to widen
what `%patient` can answer is to add an element to the allowlist here, in a
diff a reviewer sees.

There is deliberately no `%resources`. Handing a questionnaire author the
tenant's clinical bundle as an environment variable is the unbounded read
D10 names; the populate engine reaches auto-loaded content through its own
code-matching and list-group paths, which are bounded by the questionnaire's
own structure.

AND NOTHING IN THIS MODULE MAY EVALUATE A CALLER'S EXPRESSION AGAINST THE
REAL RECORD. Not to answer a yes/no question, not to decide whether to warn.
`resolves_outside_projection` reads a constant probe for exactly that reason
— its docstring has the reproduction. A bool derived from a patient's data
and handed to the caller is a read of that data; repeat it once per
questionnaire item and it is a fast one.
"""

import logging
from functools import lru_cache

import fhirpathpy

logger = logging.getLogger(__name__)

#: telecom entries survive the projection only for these systems, and only
#: `system` + `value` travel with them.
_PROJECTED_TELECOM_SYSTEMS = ('phone', 'email')

#: The only address elements an intake form may read.
_PROJECTED_ADDRESS_KEYS = ('line', 'city', 'state', 'postalCode')

#: Top-level scalars that survive whole.
_PROJECTED_SCALARS = ('birthDate', 'gender')


def patient_projection(subject):
    """Return the bounded `%patient` object for `subject`, or None.

    THE ONE PROPERTY: the returned dict is NEW, and every value in it was
    copied element by element from an allowlisted path. No key of `subject`
    reaches the result by default — `identifier`, `photo`, `contact`,
    `name.text`, `extension` and everything else are absent because nothing
    here copies them, not because something removed them.

    `resourceType` is carried as a type tag, not as data: fhirpathpy raises
    KeyError evaluating the resource-root form (`Patient.name.family`) against
    a dict without it, and `evaluate` would swallow that as "no value", which
    is a silent behaviour change for any Questionnaire not using `%patient`.
    """
    if not isinstance(subject, dict):
        return None

    projection = {'resourceType': 'Patient'}

    names = []
    for entry in subject.get('name') or []:
        if not isinstance(entry, dict):
            continue
        kept = {}
        given = [g for g in (entry.get('given') or []) if isinstance(g, str)]
        if given:
            kept['given'] = given
        if isinstance(entry.get('family'), str):
            kept['family'] = entry['family']
        if kept:
            names.append(kept)
    if names:
        projection['name'] = names

    for key in _PROJECTED_SCALARS:
        value = subject.get(key)
        if isinstance(value, str):
            projection[key] = value

    telecom = []
    for entry in subject.get('telecom') or []:
        if not isinstance(entry, dict):
            continue
        if entry.get('system') not in _PROJECTED_TELECOM_SYSTEMS:
            continue
        if not isinstance(entry.get('value'), str):
            continue
        telecom.append({'system': entry['system'], 'value': entry['value']})
    if telecom:
        projection['telecom'] = telecom

    addresses = []
    for entry in subject.get('address') or []:
        if not isinstance(entry, dict):
            continue
        kept = {}
        for key in _PROJECTED_ADDRESS_KEYS:
            value = entry.get(key)
            if key == 'line':
                lines = [ln for ln in (value or []) if isinstance(ln, str)]
                if lines:
                    kept['line'] = lines
            elif isinstance(value, str):
                kept[key] = value
        if kept:
            addresses.append(kept)
    if addresses:
        projection['address'] = addresses

    return projection


def build_context(subject=None):
    """Build the FHIRPath environment-variable context.

    %patient and %subject both resolve to the BOUNDED projection of the
    populate subject, and nothing else is in the environment. See the module
    docstring for why this is a projection rather than a filter.

    Both names share one projection object: FHIRPath evaluation reads its
    environment, it never writes to it.
    """
    projection = patient_projection(subject)
    if projection is None:
        return {}
    return {'patient': projection, 'subject': projection}


#: The record `resolves_outside_projection` probes. It is a CONSTANT, and it
#: has to stay one — see that function's docstring. Every element the
#: projection withholds is populated here with a placeholder, so an
#: expression reaching for one resolves against this and not against a
#: patient. Nothing here is anybody's data.
_PROBE_PATIENT = {
    'resourceType': 'Patient',
    'id': 'projection-probe',
    'name': [{'given': ['Probe'], 'family': 'Probe', 'text': 'Probe Probe',
              'prefix': ['Probe'], 'suffix': ['Probe'], 'use': 'official'}],
    'birthDate': '2000-01-01',
    'gender': 'unknown',
    'deceasedBoolean': False,
    'multipleBirthInteger': 1,
    'active': True,
    'telecom': [{'system': s, 'value': 'probe', 'use': 'home'}
                for s in ('phone', 'email', 'sms', 'fax', 'pager', 'url',
                          'other')],
    'address': [{'line': ['probe'], 'city': 'probe', 'state': 'probe',
                 'postalCode': 'probe', 'district': 'probe',
                 'country': 'probe', 'text': 'probe', 'use': 'home'}],
    'identifier': [{'system': 'urn:probe', 'value': 'probe', 'use': 'usual',
                    'type': {'text': 'probe'}}],
    'photo': [{'url': 'probe', 'contentType': 'image/png', 'title': 'probe',
               'data': 'cHJvYmU='}],
    'contact': [{'name': {'given': ['Probe'], 'family': 'Probe'},
                 'telecom': [{'system': 'phone', 'value': 'probe'}],
                 'address': {'line': ['probe'], 'city': 'probe'},
                 'relationship': [{'text': 'probe'}]}],
    'communication': [{'language': {'text': 'probe'}, 'preferred': True}],
    'maritalStatus': {'text': 'probe', 'coding': [{'code': 'probe'}]},
    'generalPractitioner': [{'reference': 'Practitioner/probe',
                             'display': 'probe'}],
    'managingOrganization': {'reference': 'Organization/probe',
                             'display': 'probe'},
    'link': [{'other': {'reference': 'Patient/probe'}, 'type': 'seealso'}],
    'extension': [{'url': 'urn:probe', 'valueString': 'probe'}],
    'modifierExtension': [{'url': 'urn:probe', 'valueString': 'probe'}],
    'meta': {'tag': [{'code': 'probe'}], 'security': [{'code': 'probe'}],
             'profile': ['urn:probe'], 'source': 'probe'},
    'text': {'status': 'generated', 'div': 'probe'},
    'implicitRules': 'urn:probe',
    'language': 'en',
}

#: One placeholder per resource type $populate auto-loads, so an expression
#: reaching for `%resources` is reported rather than silently empty. Constant,
#: for the same reason as _PROBE_PATIENT.
_PROBE_RESOURCES = [
    {'resourceType': 'Observation', 'id': 'probe', 'status': 'final',
     'subject': {'reference': 'Patient/probe', 'display': 'probe'},
     'code': {'text': 'probe', 'coding': [{'code': 'probe',
                                           'display': 'probe'}]},
     'valueString': 'probe',
     'valueCodeableConcept': {'text': 'probe'},
     'note': [{'text': 'probe'}],
     'performer': [{'reference': 'Practitioner/probe', 'display': 'probe'}]},
    {'resourceType': 'MedicationRequest', 'id': 'probe', 'status': 'active',
     'subject': {'reference': 'Patient/probe', 'display': 'probe'},
     'medicationCodeableConcept': {'text': 'probe'},
     'dosageInstruction': [{'text': 'probe'}]},
    {'resourceType': 'AllergyIntolerance', 'id': 'probe',
     'patient': {'reference': 'Patient/probe', 'display': 'probe'},
     'code': {'text': 'probe'},
     'reaction': [{'manifestation': [{'text': 'probe'}]}]},
    {'resourceType': 'Condition', 'id': 'probe',
     'subject': {'reference': 'Patient/probe', 'display': 'probe'},
     'code': {'text': 'probe'}},
]


@lru_cache(maxsize=512)
def resolves_outside_projection(expression):
    """Does `expression` reach for something the projection does not carry?

    THE ONE PROPERTY: **this is a pure function of the expression text.** It
    never sees a patient. It evaluates `expression` twice against the
    CONSTANT `_PROBE_PATIENT` — once through the projection and once around
    it — and reports "outside" when the unbounded form resolves and the
    bounded form does not.

    It reads a constant because the obvious implementation does not, and the
    obvious implementation is a PHI exfiltration channel. Evaluating the
    caller's expression against the REAL record and returning whether it was
    non-empty makes the issue list a one-bit oracle over exactly the data the
    projection withholds: a caller sends

        %patient.identifier.value.where($this.startsWith('123'))

    on one item, reads whether an issue named that linkId, and walks the
    withheld SSN out one character at a time — measured at eleven HTTP
    requests, with the value never once appearing in an answer (QA review of
    PR #562, council ruling D10). A bool is a value. Any function of the
    record whose output reaches the caller is a read of the record, however
    narrow the output looks.

    Answering from a constant keeps what the ruling asked for — an item that
    was refused says so, naming its linkId, instead of looking like a patient
    with no phone number — while telling the caller nothing whatsoever about
    this patient. `%patient.identifier` is outside the projection for
    everyone; that is a property of the allowlist, not of a record.

    The bounded half of the comparison is what keeps an ALLOWLISTED path that
    the real patient simply lacks (`%patient.telecom.where(system='email')`
    on a patient with no email) from being reported as withheld: it resolves
    against the probe on both sides, so it is not "outside".

    Cached because the answer depends only on `expression`, which also caps
    the work a caller can buy per request.
    """
    if not expression:
        return False
    projected = patient_projection(_PROBE_PATIENT)
    bounded = {'patient': projected, 'subject': projected}
    unbounded = {
        'patient': _PROBE_PATIENT,
        'subject': _PROBE_PATIENT,
        # The name the projection withholds entirely.
        'resources': _PROBE_RESOURCES,
    }
    if evaluate(expression, _PROBE_PATIENT, unbounded) is None:
        return False
    return evaluate(expression, projected, bounded) is None


def evaluate(expression, resource, context=None):
    """Evaluate a FHIRPath expression, returning a scalar, list, or None.

    Returns the single value when the result has one element, the list when
    it has several, and None when empty or on any evaluation error.
    """
    if not expression:
        return None
    try:
        result = fhirpathpy.evaluate(resource or {}, expression, context or {})
    except Exception as exc:  # noqa: BLE001 — never let one expr kill the run
        logger.warning('FHIRPath evaluation failed for %r: %s',
                       expression, type(exc).__name__)
        return None
    if not result:
        return None
    return result[0] if len(result) == 1 else result
