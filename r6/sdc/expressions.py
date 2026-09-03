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
"""

import logging

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


def resolves_outside_projection(expression, subject, resources=None):
    """Would `expression` have resolved against the UNBOUNDED record?

    THE ONE PROPERTY: this returns a bool. The value it evaluates is never
    returned, logged, or stored — it exists only long enough to tell the two
    reasons an item came back empty apart:

      - the record simply has no such element  -> no answer, no issue
      - the projection withheld it             -> no answer, and an issue
        naming the linkId, so a caller learns the operation refused rather
        than guessing the patient has no phone number

    Call it ONLY after the bounded evaluation returned None; on its own it
    says nothing about what the caller is allowed to see.
    """
    if not expression:
        return False
    if not isinstance(subject, dict) and not resources:
        return False
    unbounded = {
        'patient': subject,
        'subject': subject,
        # The name the projection withholds entirely, present here so that an
        # expression reaching for it is reported rather than silently empty.
        'resources': list(resources or []),
    }
    return evaluate(expression, subject, unbounded) is not None


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
