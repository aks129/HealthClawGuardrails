"""Keep caller-supplied credentials out of log lines (FHI-60).

Headers such as `X-Tenant-Id` and `X-Agent-Id` are caller-chosen strings. A
token pasted into the wrong header was written to the request log verbatim,
even though the request was refused. Anything that writes a caller-supplied
id or path to a log goes through one of these first.
"""

from __future__ import annotations

import re

#: The shape of an agent or request id a log may echo. The tenant id has its
#: own, stricter pattern in r6.access.
LOGGABLE_ID = re.compile(r'[A-Za-z0-9._:-]{1,128}')

#: Runs that look like a credential: a step-up token or JWT (both start with
#: base64 '{"'), provider and platform key prefixes, a bearer value, and long
#: hex runs (an HMAC or a raw secret; a uuid4 hex is 32, so ids stay
#: readable). Some of these are valid ids, so a pattern check alone would let
#: them through.
_CREDENTIAL_SHAPED = re.compile(
    r'eyJ[A-Za-z0-9_=-]{8,}(?:\.[A-Za-z0-9_=.-]*)?'
    r'|(?<![A-Za-z0-9])(?:sk|rk|pk)[-_][A-Za-z0-9_-]{16,}'
    r'|(?<![A-Za-z0-9])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}'
    r'|(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}'
    r'|(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}'
    r'|[Bb]earer\s+\S+'
    r'|(?<![A-Za-z0-9])[0-9a-fA-F]{40,}'
)

INVALID = 'invalid'


def loggable_id(raw: str | None, pattern: re.Pattern[str] = LOGGABLE_ID) -> str:
    """A caller-supplied id as a log may write it.

    The value itself only when it has an id's shape and no credential's;
    ``-`` when absent; otherwise the word ``invalid``. Never the raw value.
    """
    if raw is None:
        return '-'
    if not pattern.fullmatch(raw) or _CREDENTIAL_SHAPED.search(raw):
        return INVALID
    return raw


def scrub(text: str) -> str:
    """`text` with every credential-shaped run replaced by ``[redacted]``."""
    return _CREDENTIAL_SHAPED.sub('[redacted]', text)
