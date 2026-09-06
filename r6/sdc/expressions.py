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
A bool derived from a patient's data and handed to the caller is a read of
that data; repeat it once per questionnaire item and it is a fast one. That
is not a hypothetical: a `resolves_outside_projection` classifier lived here
and decided whether to report an unpopulated item by evaluating the caller's
own expression against the unbounded record, and eleven HTTP requests walked
a stored identifier out through the issue list one character at a time (QA
review of PR #562).

There is no classifier here now, in any shape. `r6/sdc/populate.py` reports
every leaf that resolved no value, which depends on nothing but whether an
answer was produced — a fact the caller already holds, because the response
carries `answer` only on the leaves that got one. Nothing to leak, by
construction, and nobody has to re-verify that the next time this file is
touched.
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


def evaluate(expression, resource, context=None, link_id=None, warned=None):
    """Evaluate a FHIRPath expression, returning a scalar, list, or None.

    Returns the single value when the result has one element, the list when
    it has several, and None when empty or on any evaluation error.

    `link_id` names the questionnaire item, for the failure log only — see
    below for why it is what gets logged and the expression is not.

    `warned` is an optional set of linkIds already logged, owned by the
    caller and scoped to ONE REQUEST — populate_questionnaire builds it per
    call. It exists because the same leaf is evaluated once per repeat
    inside a list group, and a malformed expression would otherwise log a
    line per row: the caller supplies the records through the inline
    `content` Bundle, so log volume would scale with attacker-controlled
    input. The evaluation itself is unchanged and still happens per row —
    this is a logging concern only, and every line after the first says the
    same thing about the same linkId.
    """
    if not expression:
        return None
    try:
        result = fhirpathpy.evaluate(resource or {}, expression, context or {})
    except Exception as exc:  # noqa: BLE001 — never let one expr kill the run
        # THE EXPRESSION IS NOT LOGGED. A Questionnaire is request body, so
        # its expression text is the caller's: newlines that forge a log
        # line, and literals the caller chose to park in a `where()`. Nor is
        # `str(exc)` — fhirpathpy puts the offending token in the message
        # ("Not implemented: notAFunction"), which is the same text arriving
        # by the back door. The linkId identifies the item well enough to
        # debug and is questionnaire structure rather than patient data; it
        # is caller-supplied too, and %r is what escapes control characters
        # in it. Pinned by tests/test_sdc_expressions.py::
        # test_a_failing_expression_is_not_echoed_into_the_log.
        #
        # ONCE PER REQUEST PER LINKID. `warned` is None for callers with no
        # request scope (the direct unit tests), and those log every time.
        if warned is not None:
            if link_id in warned:
                return None
            warned.add(link_id)
        logger.warning('FHIRPath evaluation failed for item %r: %s',
                       link_id, type(exc).__name__)
        return None
    if not result:
        return None
    return result[0] if len(result) == 1 else result
