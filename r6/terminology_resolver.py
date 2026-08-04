"""Runtime resolution for codes the static label table does not carry.

Why this exists
---------------
``r6/terminology.py`` was deliberately a plain dict — "not a terminology
service, not a network call, not a cache" — because a dict cannot time out and
cannot fail in production. That reasoning still holds for the read path, and
nothing here changes it: the dict is still consulted first and still answers
without touching the network.

What changed is evidence. Measured against a live MEDENT import on 2026-08-04
(tenant with 52 Conditions, 26 distinct codes): **1 of 15 distinct ICD-10-CM
codes and 0 of 11 SNOMED codes had a label**. The one hit was E78.5, and it
produced the agent's entire answer — it reported "High Cholesterol" as the
patient's key health focus because that was the only row the table could
translate. A hand-curated table of 121 labels cannot cover a real EHR export,
and the honest "a record is here I could not read" fallback, applied to 25 of
26 conditions, is not a product.

So the miss path — and only the miss path — now consults the public
terminology services ``r6/curatr.py`` already talks to.

Three properties this must never lose
-------------------------------------
1. **It cannot break a read.** Every failure mode — disabled, over budget,
   unknown system, timeout, malformed response, unexpected exception —
   returns ``None``, which is exactly what the static table returned before.
   The caller's existing "unreadable record" path handles it.

2. **It cannot make a read slow.** A cold cache with 26 unknown codes and a
   5s timeout each would add minutes to a chat message. Work is capped per
   request (``PER_REQUEST_MAX_LOOKUPS``) and by wall clock
   (``PER_REQUEST_BUDGET_SECONDS``); past either, misses stay misses and are
   resolved on a later request. A partially-labelled answer now beats a
   complete one after a timeout.

3. **It cannot leak PHI.** Only ``(system, code)`` is sent. No tenant, no
   patient, no agent id, no free text. A code's meaning is a property of the
   code, not of the patient — the same argument that justifies the static
   table.

The privacy cost, stated plainly
--------------------------------
This is a disclosure, just a narrow one. Querying NLM or tx.fhir.org reveals
which clinical codes exist somewhere in this deployment — not whose they are,
but the set is clinical information that previously never left the box. It is
therefore **opt-in**: with ``TERMINOLOGY_LOOKUP_ENABLED`` unset, this module
does nothing at all and behaviour is byte-identical to before. Deployments
that cannot accept that disclosure should point ``r6/curatr.py`` at a
self-hosted terminology server instead of enabling this against public ones.

The cache is per-process and in-memory
--------------------------------------
Deliberately not a database table. A cache write during a read would share the
caller's SQLAlchemy session, and a failed write that rolled it back would
discard the caller's work — the exact defect shape of #202. A dict cannot do
that. The cost is that each process re-resolves after a restart, which the
budget already makes survivable.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from flask import has_request_context, request

from r6 import curatr as _curatr_mod

logger = logging.getLogger(__name__)

ENABLED_ENV = "TERMINOLOGY_LOOKUP_ENABLED"
_TRUE_VALUES = frozenset({"1", "true", "yes"})

# Per-request ceilings. Both are deliberately small: this runs inside a
# patient-facing read, and a label is a nicety while a timely answer is not.
PER_REQUEST_MAX_LOOKUPS = 8
PER_REQUEST_BUDGET_SECONDS = 0.4

# Systems r6/curatr.py knows how to route. Anything else is an immediate miss
# rather than a wasted round trip.
RESOLVABLE_SYSTEMS = frozenset({
    "http://hl7.org/fhir/sid/icd-10-cm",
    "http://hl7.org/fhir/sid/icd-10",
    "http://www.nlm.nih.gov/research/umls/rxnorm",
    "http://snomed.info/sct",
    "http://loinc.org",
})

# (system, code) -> label, or None for a resolved miss. Negative entries are
# cached too: an unknown code must not be re-queried on every message.
_CACHE: dict[tuple[str, str], str | None] = {}
_CACHE_LOCK = threading.Lock()

_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def enabled() -> bool:
    """Whether runtime lookup is switched on for this deployment."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() in _TRUE_VALUES


def _engine():
    """One CuratrEngine per process; it owns a pooled requests session."""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _curatr_mod.CuratrEngine()
    return _ENGINE


_BUDGET_KEY = "r6.terminology_budget"


def _budget_state():
    """Per-request (lookups_used, deadline), or None outside a request.

    Outside a request context there is no budget to enforce — that path is
    scripts and tests, not a patient waiting on a message.

    Stored on ``request.environ`` rather than ``flask.g`` on purpose. ``g`` is
    scoped to the APP context, not the request: whenever an app context
    outlives several requests — a worker, a CLI command, a test that pushes
    one itself — a ``g``-based budget is spent once and never refills, and
    every later request silently stops resolving. ``environ`` is created per
    request by the server, so it cannot leak between them.
    """
    if not has_request_context():
        return None
    environ = request.environ
    state = environ.get(_BUDGET_KEY)
    if state is None:
        state = {"used": 0, "deadline": time.monotonic() + PER_REQUEST_BUDGET_SECONDS}
        environ[_BUDGET_KEY] = state
    return state


def _budget_allows() -> bool:
    state = _budget_state()
    if state is None:
        return True
    if state["used"] >= PER_REQUEST_MAX_LOOKUPS:
        return False
    return time.monotonic() < state["deadline"]


def _budget_spend() -> None:
    state = _budget_state()
    if state is not None:
        state["used"] += 1


def resolve(system: str, code: str) -> str | None:
    """A label for this code from a terminology service, or None.

    None is always a valid answer and is what every failure returns. Callers
    treat it exactly as they treated a static-table miss.
    """
    if not enabled():
        return None
    if not system or not code:
        return None
    if system not in RESOLVABLE_SYSTEMS:
        return None

    key = (system, code)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

    if not _budget_allows():
        # Not cached as a miss: the code is unresolved because we ran out of
        # room, not because the terminology server said no. Caching it here
        # would make one slow request poison the label forever.
        return None

    _budget_spend()
    label = None
    try:
        result = _engine()._lookup_code(system, code)
        if isinstance(result, dict) and result.get("valid"):
            display = result.get("display")
            if isinstance(display, str) and display.strip():
                label = display.strip()[:512]
    except Exception:  # noqa: BLE001 — a label is never worth failing a read
        logger.debug("terminology lookup failed for %s (system %s)",
                     code, system, exc_info=True)
        return None

    with _CACHE_LOCK:
        _CACHE[key] = label
    return label


def cache_size() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)


def reset_cache() -> None:
    """Drop the process cache. For tests and for a deliberate refresh."""
    with _CACHE_LOCK:
        _CACHE.clear()
