"""Type-safe readers for stored FHIR resources, and strict JSON out.

The write API stores what it is given, and so does an upstream feed, so any
field may hold a string, a list, a number or null where FHIR says object.
Read as `res.get("subject", {}).get("reference")`, one such row raised and
turned a whole operation into a 500 for every patient in the tenant (#869,
#879). Every engine reader goes through these instead: a field of the wrong
shape reads as absent, and the row is skipped or counted, never fatal.

The second half is the response. Python's json writes a non-finite float as
a bare `NaN` or `Infinity` token, which strict parsers, the browser's
`JSON.parse` among them, refuse. `StrictJSONProvider` turns each one into
null and then serializes with `allow_nan=False`, so no Flask JSON response
can carry one. `strict_dumps` does the same for JSON nested in a string,
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


def string_field(res, field):
    """`res[field]` when it is a string, else ""."""
    value = as_dict(res).get(field)
    return value if isinstance(value, str) else ""


def finite(value):
    """`value` with every non-finite float, at any depth, replaced by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def strict_dumps(value, **kwargs):
    """json.dumps that writes null for a non-finite float and never emits
    a bare NaN or Infinity token."""
    kwargs.setdefault("allow_nan", False)
    return json.dumps(finite(value), **kwargs)


class StrictJSONProvider(DefaultJSONProvider):
    """Flask's JSON provider with non-finite floats written as null.

    Sanitising first means `allow_nan=False` never raises for a stored NaN:
    the response is strict JSON, not a new 500.
    """

    def dumps(self, obj, **kwargs):
        kwargs.setdefault("allow_nan", False)
        return super().dumps(finite(obj), **kwargs)
