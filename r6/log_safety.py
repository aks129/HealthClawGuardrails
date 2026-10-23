"""Keep caller-supplied values out of log lines (FHI-60).

Headers such as `X-Tenant-Id` and `X-Agent-Id` are caller-chosen strings. A
token pasted into the wrong header was written to the request log verbatim,
even though the request was refused. Recognising tokens by shape cannot be
made reliable: the engine's own OAuth tokens are `secrets.token_urlsafe`
values, which have no prefix and are valid tenant ids. So a log never writes
a caller-supplied id at all. It writes a short keyed hash of it, which the
owner can recompute to search the logs (`flask --app main log-id-hash <id>`).

Paths are still scrubbed of credential-shaped runs, and query strings are
never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

logger = logging.getLogger(__name__)

#: Domain separation: the hash key is derived from the step-up secret, never
#: the secret itself. Otherwise a caller could put a token payload in a
#: header and read part of that payload's real signature out of the log.
_KEY_LABEL = b'healthclaw-log-id-v1'

_HASH_PREFIX = 't:'
_HASH_HEX_CHARS = 10

#: Request ids the engine itself generates (`str(uuid4())[:8]`) or a full
#: UUID. Neither can carry a credential, and both are useful verbatim for
#: correlation; anything else a caller sends is hashed like the other ids.
_OWN_REQUEST_ID = re.compile(
    r'[0-9a-f]{8}'
    r'|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{12}')

#: Runs that look like a credential, cut out of a logged path: a step-up
#: token or JWT (both start with base64 '{"'), provider and platform key
#: prefixes, a bearer value, and long hex runs.
_CREDENTIAL_SHAPED = re.compile(
    r'eyJ[A-Za-z0-9_=-]{8,}(?:\.[A-Za-z0-9_=.-]*)?'
    r'|(?<![A-Za-z0-9])(?:sk|rk|pk)[-_][A-Za-z0-9_-]{16,}'
    r'|(?<![A-Za-z0-9])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}'
    r'|(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}'
    r'|(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}'
    r'|(?i:bearer)\s+\S+'
    r'|(?<![A-Za-z0-9])[0-9a-fA-F]{40,}'
)

#: A query string inside a logged request line, up to the next space or
#: quote. It is where `?access_token=` and signed-URL signatures travel.
_QUERY_STRING = re.compile(r'\?[^\s"]*')


def _key() -> bytes | None:
    secret = os.environ.get('STEP_UP_SECRET', '')
    if not secret:
        return None
    return hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()


def warn_if_unkeyed() -> None:
    """Say at boot when log hashes are unkeyed, so anyone can recompute them."""
    if _key() is None:
        logger.warning('STEP_UP_SECRET is not set: request-log ids are hashed '
                       'without a key, so a guessable id can be recovered '
                       'from the log')


def id_hash(raw: str) -> str:
    """The hash a log writes in place of `raw`: ``t:`` and 10 hex chars."""
    key = _key()
    # str() first: a few callers hold a non-string id from a JSON body, and
    # a log line must never be the thing that raises.
    data = str(raw).encode('utf-8', 'surrogateescape')
    if key is None:
        digest = hashlib.sha256(data).hexdigest()
    else:
        digest = hmac.new(key, data, hashlib.sha256).hexdigest()
    return _HASH_PREFIX + digest[:_HASH_HEX_CHARS]


def loggable_id(raw: str | None) -> str:
    """A caller-supplied tenant or agent id as a log may write it.

    ``-`` when absent, otherwise its hash. Never the raw value.
    """
    if raw is None:
        return '-'
    return id_hash(raw)


def loggable_request_id(raw: str | None) -> str:
    """The request id verbatim only when it is one of ours or a UUID."""
    if raw is None:
        return '-'
    if _OWN_REQUEST_ID.fullmatch(raw):
        return raw
    return id_hash(raw)


#: Fasten's request id is written as sent only when it is exactly a canonical
#: dashed UUID (security ruling on #989): nothing else, undashed 32-hex
#: included, is distinguishable from a token.
_CANONICAL_UUID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{12}')


def vendor_request_id(raw) -> str:
    """A vendor request id as a log may write it: as sent when it is a
    canonical UUID, ``-`` when absent, otherwise its hash."""
    if raw is None or raw == '':
        return '-'
    if isinstance(raw, str) and _CANONICAL_UUID.fullmatch(raw):
        return raw
    return id_hash(raw)


def payload_shape(payload: dict, raw: str, named: frozenset) -> str:
    """A caller's JSON object described without writing any of it.

    Its size, the keys from `named` that it carries, and a count of the rest.
    A key is caller-chosen text like any value, so an unexpected one is
    counted, never written.
    """
    known = sorted(key for key in payload if key in named)
    return (f"bytes={len(raw.encode('utf-8'))} "
            f"keys={','.join(known) or '-'} other_keys={len(payload) - len(known)}")


def scrub(text: str) -> str:
    """`text` with every credential-shaped run replaced by ``[redacted]``."""
    return _CREDENTIAL_SHAPED.sub('[redacted]', text)


def scrub_request_line(text: str) -> str:
    """A logged request line with its query string dropped, then scrubbed."""
    return scrub(_QUERY_STRING.sub('', text))
