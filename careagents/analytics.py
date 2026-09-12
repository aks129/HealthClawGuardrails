"""Counting views of the pages anyone can open, without counting people.

Why this exists rather than a hosted analytics product: the hosted ones work
by putting a third-party script on the page and sending it the URL, and the
pages behind sign-in here are a person's own health surface. Vercel's Web
Analytics is not an option either — its endpoint is served by Vercel's edge,
and this app answers from somewhere else, so the script would have nothing to
report to.

What is recorded is one integer per UTC day per page, and the page is named by
its Flask endpoint, which is a name written in this repository rather than
anything a request supplies. No address, no agent string, no cookie, no
referrer, no query string. That is the whole record.

The consequence is worth stating plainly rather than discovering later: this
counts views, not visitors. One person reloading five times is five. There is
no way to tell them apart without storing something about them, and storing
something about them is what this deliberately does not do.

Only the pages on PUBLIC_PAGES are counted. Everything behind sign-in is not
counted at all, so no row here can be about one person's use of their own
records, whatever anyone later does with the table.
"""
from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import update

from careagents.models import PageViewDay

logger = logging.getLogger(__name__)

#: Flask endpoint names for pages that need no session. Adding to this set is
#: a decision, which is the point of it being a set rather than a rule about
#: which routes happen to be reachable: `home`, `chat` and `brief` are a
#: person's own health surface, and they are absent on purpose.
PUBLIC_PAGES = frozenset({"landing", "auth"})


def utc_day(now: _dt.datetime | None = None) -> str:
    """The UTC date as "YYYY-MM-DD" — the only time this module stores."""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    return moment.strftime("%Y-%m-%d")


def record_view(session_scope, endpoint: str, now: _dt.datetime | None = None
                ) -> bool:
    """Add one to today's count for `endpoint`. Returns whether it counted.

    A page that is not public is not counted and says so by returning False,
    so a caller that wires this to the wrong hook fails a test rather than
    quietly filling the table with signed-in traffic.

    The increment is done in the database (`views = views + 1`) rather than by
    reading and writing back, because several workers serve these pages and a
    read-modify-write between them loses counts.
    """
    if endpoint not in PUBLIC_PAGES:
        return False
    day = utc_day(now)
    with session_scope() as session:
        touched = session.execute(
            update(PageViewDay)
            .where(PageViewDay.day == day, PageViewDay.endpoint == endpoint)
            .values(views=PageViewDay.views + 1)
        ).rowcount
        if touched:
            return True
    # No row for today yet. Insert one, and treat a collision with another
    # worker doing the same thing as an ordinary outcome: retry the increment
    # once, in its own transaction, so the loser of the race still counts.
    try:
        with session_scope() as session:
            session.add(PageViewDay(day=day, endpoint=endpoint, views=1))
        return True
    except Exception:
        with session_scope() as session:
            touched = session.execute(
                update(PageViewDay)
                .where(PageViewDay.day == day,
                       PageViewDay.endpoint == endpoint)
                .values(views=PageViewDay.views + 1)
            ).rowcount
        return bool(touched)


def counts(session_scope, days: int = 7, now: _dt.datetime | None = None
           ) -> list[tuple[str, str, int]]:
    """(day, endpoint, views) for the last `days` UTC days, newest first."""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    since = utc_day(moment - _dt.timedelta(days=days - 1))
    with session_scope() as session:
        rows = session.query(PageViewDay).filter(
            PageViewDay.day >= since).order_by(
                PageViewDay.day.desc(), PageViewDay.endpoint).all()
        return [(r.day, r.endpoint, r.views or 0) for r in rows]
