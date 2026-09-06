"""Constant-time comparison of a caller-supplied string against a secret.

`hmac.compare_digest` takes two bytes-likes, or two `str`s that are BOTH
ASCII-only. Handed a `str` carrying anything else it raises TypeError. That
is a CPython type constraint, not an authorization fact — and every gate in
this repository compares a value the caller controls (a header, a query
argument, half of a signed token) against one the server computed. r6/ held
13 calls to it, 11 of which passed that caller-supplied value as a raw `str`:
one non-ASCII byte in any of them raises out of the gate (#557).

The refusal is made TOTAL rather than the input pre-screened, and the
difference matters. A pre-check would add a second refusal path whose only
cause is an encoding: the caller would learn their credential was rejected
for how it was spelled rather than for being wrong, which is a distinction
with no authorization meaning behind it. Comparing bytes sends every wrong
credential — ASCII garbage, a UTF-8 name, a lone surrogate — out through the
one door the gate already had.

Callers pass strings and this module owns the encode, so `hmac.compare_digest`
is called in exactly one production module. That is pinned by
tests/test_constant_time.py, not left to review.
"""

import hmac

__all__ = ['as_bytes', 'equal']


def as_bytes(value: str | bytes | bytearray) -> bytes:
    """Encode `value` to bytes for a byte operation, without ever raising.

    'surrogatepass' is load-bearing rather than defensive: `json.loads` of
    `'"\\ud800"'` yields a lone surrogate, so a credential read out of a JSON
    body arrives as a `str` that strict UTF-8 refuses to encode. Bytes pass
    through unchanged, so a caller already holding raw bytes need not decode
    them first only to have them re-encoded here.

    Exported alongside `equal` because a comparison is not the only byte
    operation an untrusted string reaches: r6/stepup.py feeds one to
    `hmac.new` on the line above its comparison, and that encode was just as
    partial as the compare.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value.encode('utf-8', 'surrogatepass')


def equal(provided: str | bytes | bytearray,
          expected: str | bytes | bytearray) -> bool:
    """True iff `provided` equals `expected`, compared in constant time.

    `provided` is named first because it is the untrusted half; the order
    does not change the answer, but at a call site it says which side the
    caller controls.

    Length is not hidden — `hmac.compare_digest` never hid it either, and the
    expected values here are fixed-width digests and configured secrets.
    """
    return hmac.compare_digest(as_bytes(provided), as_bytes(expected))
