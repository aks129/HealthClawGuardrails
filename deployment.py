"""Which deployment is this, and what is it allowed to do.

Two hosts serve this codebase and they are not interchangeable:

    app.healthclaw.io   Railway, Postgres, writes persist.  The stateful one.
    healthclaw.io       Vercel, ephemeral serverless storage, every mutating
                        request to a stateful path is refused with 405
                        (api/index.py) and /r6/fhir/health reports
                        `database: error`.

That distinction used to live in exactly one place — a private constant in
api/index.py — while the pages served by both hosts assumed they could write.
The public dashboard shipped fifteen panels of buttons that POST to /r6/…, so
on healthclaw.io every one of them returned 405 to anyone who clicked it.

Keeping the fact here, once, is the point. `tests/test_deployment_topology.py`
fails if api/index.py grows its own copy of the hostname again, because two
copies of a deployment fact drift and the drift is invisible until a visitor
finds it.
"""

import os

#: The host that can actually hold state. Anything that needs a write, or a
#: measurement that requires one, belongs here.
STATEFUL_HOST = "https://app.healthclaw.io"


def _is_true(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_read_only(config=None) -> bool:
    """True when this process must not be asked to persist anything.

    Reads Flask config first so tests can set it without touching the
    environment, then falls back to the environment for the serverless entry
    point, which runs before an app context exists.

    VERCEL is set by the platform; READ_ONLY_DEPLOYMENT is set by us in
    vercel.json. Either one is sufficient — a deployment that declares itself
    read-only is taken at its word even if it is not on Vercel, which is what
    lets a local run reproduce the read-only page.
    """
    if config is not None:
        if _is_true(config.get("VERCEL")) or _is_true(
                config.get("READ_ONLY_DEPLOYMENT")):
            return True
    return (_is_true(os.environ.get("VERCEL"))
            or _is_true(os.environ.get("READ_ONLY_DEPLOYMENT")))
