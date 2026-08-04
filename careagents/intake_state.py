"""Which of four things is true about a patient's records — and only one.

Reported live 2026-08-04 (#336). Moments after a Fasten/MEDENT connect, the
agent greeting said:

    Hi — I'm joe. I found 0 conditions, 0 medications, and 0 lab results in
    your records.

The import was still running; the connect page had promised records "over the
next 5-45 minutes". Zero was the expected state. The count was accurate and
the sentence was false, because "I found 0 conditions in your records" reads
as *you have no conditions* — a claim about the person's health made from a
fact about a background job.

Zero-from-empty and zero-from-not-yet-arrived are the same number. Only the
connection tells them apart, which is why this takes the connection and not
just the totals.

## Why counts live inside the state

`counts` is populated for :data:`READY` and is `None` everywhere else, and
:func:`classify` drops them rather than trusting the caller to check. The
alternative — return the counts always and have each template remember when it
may print them — is the defect shape `docs/2026-08-02-retro.md` names: a
control that looks like one thing and quietly does two. A caller cannot render
a number that does not exist.

Nothing here performs I/O, so the honest-vs-false decision is testable without
a HealthClaw, an account, or a browser.
"""

from __future__ import annotations

from dataclasses import dataclass

NO_CONNECTION = "no-connection"   # nothing to speak about yet
ARRIVING = "arriving"             # connected, nothing landed, still in window
OVERDUE = "overdue"               # connected, nothing landed, window passed
READY = "ready"                   # something landed; counts are speakable

# The window `/connect` itself promises ("over the next 5-45 minutes"). Past
# it, "your records are still arriving" becomes its own unfounded claim — this
# app cannot see the import job, so it knows only that nothing has landed. Same
# family as #310, where a fabricated progress animation stood in for a fact the
# app did not have.
ARRIVAL_WINDOW_SECONDS = 45 * 60


@dataclass(frozen=True)
class IntakeState:
    state: str
    counts: dict | None
    provider: str | None
    minutes_waiting: int | None


def classify(*, totals: dict | None, connection_status: str | None,
             connected_at: float | None, now: float,
             provider: str | None = None) -> IntakeState:
    """Decide what may be said about this patient's records.

    ``totals`` is ``None`` when the count was not taken or the lookup failed —
    which is not the same as zero, and never collapses into it. A failed count
    means we do not get to say what we would have seen.

    ``connection_status`` is the stored ``pending|active|revoked``. It is a
    weak signal on its own: it flips to ``active`` only while the connect page
    is open polling, so a patient who closed the tab keeps a stale ``pending``
    over a full chart. Records in hand therefore outrank it.
    """
    if connection_status is None:
        return IntakeState(NO_CONNECTION, None, provider, None)

    arrived = bool(totals) and any(int(v or 0) > 0 for v in totals.values())
    if arrived or connection_status != "pending":
        return IntakeState(READY, totals, provider, None)

    waiting = None if connected_at is None else max(0.0, now - connected_at)
    if waiting is not None and waiting > ARRIVAL_WINDOW_SECONDS:
        return IntakeState(OVERDUE, None, provider, int(waiting // 60))
    return IntakeState(
        ARRIVING, None, provider,
        None if waiting is None else int(waiting // 60))
