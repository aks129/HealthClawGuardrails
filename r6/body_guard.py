"""Request-body limits shared by every write path.

Moved out of `r6/routes.py` whole. It lived there because that is where the
first caller was, and three other modules then imported it back out — each
with a function-local import and a comment explaining that a module-level one
would deadlock. Those lazy imports were two of the back-edges holding
`r6.routes` inside an import cycle with the auth stack, which is what kept the
module from being split.

Nothing here touches the database, a blueprint, or another `r6` module: it
reads `flask.request` and returns a tuple. That is the whole reason it can
live at the bottom of the graph.
"""

from __future__ import annotations

from flask import request

#: FHIR resources do not nest anywhere near this deep in practice. Headroom,
#: not a tight bound.
INGEST_MAX_JSON_DEPTH = 32


def json_depth_within(obj, limit, _depth=0):
    """Bounded depth check, not a recursion-limit workaround.

    Refuses at `limit` (32) regardless of Python's actual recursion ceiling
    (typically ~1000), so a hostile payload is rejected long before it can
    exhaust the stack — this walker itself never recurses past limit+1
    levels.
    """
    if _depth > limit:
        return False
    if isinstance(obj, dict):
        return all(json_depth_within(v, limit, _depth + 1)
                   for v in obj.values())
    if isinstance(obj, list):
        return all(json_depth_within(v, limit, _depth + 1) for v in obj)
    return True


def json_body_within_depth(limit=INGEST_MAX_JSON_DEPTH):
    """Parse this request's JSON body, refusing one nested too deep to be safe.

    The write paths' shared entry point to the guard #267 put on
    /internal/ingest-bundle. `request.get_json(silent=True)` suppresses decode
    errors but NOT `RecursionError`, which json.loads raises on a ~1000-deep
    payload — so every handler that parsed before its auth gate turned a few
    kilobytes of `[[[[...` into a 500 with no credential presented (#312).

    Returns `(body, too_deep)`. `body` is whatever `get_json(silent=True)`
    returned (None on any decode failure) and `too_deep` is True when the
    payload must be refused outright. The caller formats its own error, so no
    handler's wire format changes: the FHIR routes answer an
    OperationOutcome, actions answer `{"error": ...}`, smbp answers its own
    OperationOutcome, all with the 400 they already use for a bad body.

    Two layers, as on ingest-bundle: catching RecursionError handles the
    payload that cannot be parsed, and json_depth_within handles the one that
    parses but is still absurd enough to hurt a downstream consumer.
    """
    try:
        body = request.get_json(silent=True)
    except RecursionError:
        return None, True
    if not json_depth_within(body, limit):
        return None, True
    return body, False
