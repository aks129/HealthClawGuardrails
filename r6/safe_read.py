"""Type-safe readers for stored FHIR resources, and strict JSON out.

The write API stores what it is given, and so does an upstream feed, so any
field may hold a string, a list, a number or null where FHIR says object.
Read as `res.get("subject", {}).get("reference")`, one such row raised and
turned a whole operation into a 500 for every patient in the tenant (#869,
#879). Every engine reader goes through these instead: a field of the wrong
shape reads as absent, and the row is skipped or counted, never fatal.

The second half is the response. Python's json writes a non-finite float as
a bare `NaN` or `Infinity` token, which strict parsers, the browser's
`JSON.parse` among them, refuse. `StrictJSONProvider` drops each one (a
null property is invalid FHIR JSON) and then serializes with
`allow_nan=False`, so no Flask JSON response can carry one. `strict_dumps` does the same for JSON nested in a string,
such as a Parameters `valueString`.
"""

import json
import math

from flask.json.provider import DefaultJSONProvider


def as_dict(value):
    """`value` when it is a JSON object, else {}."""
    return value if isinstance(value, dict) else {}


def as_list(value):
    """`value` when it is a JSON array, else []."""
    return value if isinstance(value, list) else []


def is_number(value):
    """An int or float that float arithmetic can hold. bool is an int
    subclass and is excluded, and so are NaN and the infinities: none of them
    is a measurement. So is an int too large for a float (`10**400` parses
    from JSON as one): `math.isfinite` and any mean over it raise
    OverflowError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def subject_reference(res, field="subject"):
    """`res[field].reference` when it is a string, else None."""
    ref = as_dict(as_dict(res).get(field)).get("reference")
    return ref if isinstance(ref, str) else None


def codings(concept):
    """The Coding objects of a CodeableConcept, skipping anything else."""
    return [c for c in as_list(as_dict(concept).get("coding"))
            if isinstance(c, dict)]


def coding_code(coding):
    """`coding.code` when it is a non-empty string, else None."""
    code = as_dict(coding).get("code")
    return code if isinstance(code, str) and code else None


def coding_system(coding):
    """`coding.system` when it is a string, else ""."""
    system = as_dict(coding).get("system")
    return system if isinstance(system, str) else ""


def codes(concept):
    """The string codes of a CodeableConcept, in order."""
    return [c for c in map(coding_code, codings(concept)) if c]


#: Keys that make a dict a CodeableConcept rather than a Coding's own value.
_CONCEPT_KEYS = ("coding", "text", "extension")


def is_coding_shaped(node):
    """A dict that is, or poses as, a Coding: it has `system` or `code`, no
    `coding` list of its own, and is not a resource. Where it sits does not
    matter: valueCoding, an extension's valueCoding at any depth, `class`,
    meta.tag and meta.security all hold Codings with no `coding` around
    them (R886-1)."""
    return (isinstance(node, dict) and "resourceType" not in node
            and "coding" not in node and ("system" in node or "code" in node))


def code_shape(value, *, in_coding_list):
    """What a `code` beside a `system` holds, for redaction and the validator
    to treat alike: "string", "int", "codings", "concept" or "bad".

    Inside a `coding` list the dict is known to be a Coding, so its code is a
    string or an int and nothing else. Elsewhere a `code` key can belong to
    a parent element instead: a list of Codings (Questionnaire.item.code) or
    a CodeableConcept (component.code, even one carrying only an extension).
    Anything else, an object with none of a CodeableConcept's keys among
    them, is no code and can carry any text.
    """
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if in_coding_list:
        return "bad"
    if isinstance(value, list):
        return "codings"
    if isinstance(value, dict) and any(k in value for k in _CONCEPT_KEYS):
        return "concept"
    return "bad"


def string_field(res, field):
    """`res[field]` when it is a string, else ""."""
    value = as_dict(res).get(field)
    return value if isinstance(value, str) else ""


def _non_finite(value):
    return isinstance(value, float) and not math.isfinite(value)


def finite(value):
    """`value` with every non-finite float, at any depth, dropped: the key
    from an object, the element from an array. Not written as null, because
    a null property is invalid FHIR JSON. Only a bare top-level non-finite,
    which has nowhere to be dropped from, becomes None.

    Only dicts, lists and tuples are walked; dataclasses, UUIDs and Decimals
    pass through untouched for the JSON provider's `default` to handle.
    """
    if _non_finite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items() if not _non_finite(v)}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value if not _non_finite(v)]
    return value


def strict_dumps(value, **kwargs):
    """json.dumps that drops a non-finite float and never emits a bare NaN
    or Infinity token."""
    kwargs.setdefault("allow_nan", False)
    return json.dumps(finite(value), **kwargs)


class StrictJSONProvider(DefaultJSONProvider):
    """Flask's JSON provider with non-finite floats dropped.

    Sanitising first means `allow_nan=False` never raises for a stored NaN:
    the response is strict JSON, not a new 500.
    """

    def dumps(self, obj, **kwargs):
        kwargs.setdefault("allow_nan", False)
        return super().dumps(finite(obj), **kwargs)
