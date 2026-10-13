"""The hub's view of an account: the words a person reads on /home.

Pure functions over `AccountService.list_home` output. No network and no
PHI: labels, statuses, counts and timestamps only (calm hub spec 3 and 7).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

#: The one place a stored status becomes a word a person reads. `revoked`
#: has no word: a revoked connection appears only under Past connections,
#: whose heading already says what it is.
STATUS_WORDS = {"active": "Connected", "pending": "Connecting…",
                "empty": "No records yet"}

_DAY = 86400


def status_word(status: str | None) -> str:
    return STATUS_WORDS.get(status or "", "")


def updated_line(ts: float | None, now: float) -> str:
    if ts is None:
        return ""
    days = int(max(0.0, now - ts) // _DAY)
    if days == 0:
        return "Updated today"
    if days == 1:
        return "Updated yesterday"
    return f"Updated {days} days ago"


def count_line(n: int | None) -> str:
    # None is "not counted", which is never the same as zero (#403).
    if n is None:
        return ""
    return "1 record" if n == 1 else f"{n} records"


def _record(c: dict, now: float) -> dict:
    # Only a sync, or an active connection, has updated anything. A pending
    # or empty row has received nothing, so its connect time is not an
    # update and saying "Updated today" there is untrue.
    stamp = c.get("last_synced_at")
    if stamp is None and c.get("status") == "active":
        stamp = c.get("connected_at")
    return {**c,
            "status_word": status_word(c.get("status")),
            "count_line": count_line(c.get("last_count")),
            "updated": updated_line(stamp, now),
            "sub": subtitle(c),
            "is_sample": c.get("kind") == "sample"}


def subtitle(c: dict) -> str:
    """The line under a source's name: its provider, unless the name
    already says it ("Records from your doctor" over "your doctor", patient
    tester on #940). Never a status word: the chip is the one place a row
    says where it stands, so a "Connecting…" left in the provider field
    can't sit under a "Connected" chip (hub critique 2026-10)."""
    provider = (c.get("provider") or "").strip()
    if (not provider or provider.lower() in (c.get("label") or "").lower()
            or provider in STATUS_WORDS.values()):
        return ""
    return provider


def build(home: dict, now: float) -> dict:
    conns = home["connections"]
    by_id = {c["id"]: c for c in conns}
    # Which assistants read each connection, for the disconnect dialog: a
    # revoked connection takes its assistant's approvals with it (#847).
    readers: dict = {}
    for a in home["agents"]:
        readers.setdefault(a["connection_id"], []).append(a["name"])
    records = [{**_record(c, now), "readers": ", ".join(readers.get(c["id"], []))}
               for c in conns if c["status"] != "revoked"]
    active = [r for r in records if r["status"] == "active"]
    return {
        "agents": [{**a, "reads": (by_id.get(a["connection_id"]) or {})
                    .get("label", "")} for a in home["agents"]],
        "records": records,
        "active_records": active,
        "move_choices": [{"id": r["id"], "label": r["label"]} for r in active],
        "past": [{**_record(c, now),
                  "readers": ", ".join(readers.get(c["id"], []))}
                 for c in conns if c["status"] == "revoked"],
        "connected_kinds": sorted({r["kind"] for r in active}),
        # Any real connection not revoked: one still connecting, or with no
        # records yet, is real all the same (#856 sign-off F3).
        "has_real": any(not r["is_sample"] for r in records),
        # The source list (hub critique 2026-10): made-up rows step aside
        # once real records are here, and an empty upload shows once.
        "sources": _sources(records),
        "made_up_sources": ([r for r in records if r["is_sample"]]
                            if _real_has_records(records) else []),
        "news": _news(records, now),
    }


def _real_has_records(records: list[dict]) -> bool:
    """Has a real source brought records? Active, and not counted as zero.
    An uncounted one (None) has records all the same: unknown is never
    zero (#403). One still connecting has brought nothing yet."""
    return any(not r["is_sample"] and r["status"] == "active"
               and r.get("last_count") != 0 for r in records)


def _sources(records: list[dict]) -> list[dict]:
    """The rows "Your sources" lists. Samples leave once a real source has
    records (they wait in a quiet drawer instead). Of the uploads still
    waiting for a file, only the oldest shows: two rows reading "Uploaded
    records / No records yet" are one job. One an assistant reads stays."""
    hide_samples = _real_has_records(records)
    out, empty_upload = [], False
    for r in sorted(records, key=lambda r: r.get("connected_at") or 0):
        if r["is_sample"] and hide_samples:
            continue
        if (r["kind"] == "direct" and r["status"] == "empty"
                and r.get("last_count") is None and not r.get("readers")):
            if empty_upload:
                continue
            empty_upload = True
        out.append(r)
    order = {r["id"]: i for i, r in enumerate(records)}
    return sorted(out, key=lambda r: order[r["id"]])


#: How long a new source's arrival is news.
NEWS_DAYS = 7


def _news(records: list[dict], now: float) -> list[dict]:
    """What's new: a real source connected in the last week whose records
    arrived. Only an arrival is a change we know of; a stamp that moved
    ("Updated today") is not one, and made-up records are never news."""
    return [{"id": r["id"], "label": r["label"],
             "line": f"{r['label']}: {count_line(r['last_count'])} arrived."}
            for r in records
            if not r["is_sample"] and r["status"] == "active"
            and (r.get("last_count") or 0) > 0
            and now - (r.get("connected_at") or 0) < NEWS_DAYS * _DAY]


def status_chip(c: dict, page: dict | None, paused: bool) -> dict:
    """The one status a source row shows: its class and its word.

    A row has one place for status, and this is what fills it, so the chip
    and the note under it can never disagree. A failed import keeps the
    pending class, so the pending poller still sees a retry land, but it
    never reads "Connecting…" (patient tester on #940)."""
    if paused:
        return {"cls": "paused", "word": "Paused"}
    if page and page.get("state") == "failed":
        return {"cls": c["status"], "word": "Didn't finish"}
    return {"cls": c["status"], "word": status_word(c["status"])}


def menu_items(catalog: list[dict], real_open: bool) -> list[dict]:
    """The connector rows the Add records menu shows (spec section 4).

    The sample is never a row: it is the closed state's one action and the
    open state's small link. With real records closed the catalog still
    returns every other source as coming soon, and the closed state shows
    none of them, so they are dropped here rather than trusted to the
    template.
    """
    rows = [m for m in catalog if m["id"] != "sample"]
    if not real_open:
        rows = [m for m in rows if m["tier"] != "soon"]
    return rows


#: An engine status past the human gate, as the state the hub's line is
#: chosen by (#847). home.js owns the sentences; this owns the mapping, so
#: an engine status with no line here is dropped rather than shown raw.
RECENT_STATES = {"completed": "done", "failed": "failed",
                 "needs_review": "needs_review", "unknown": "unknown",
                 "executing": "in_progress"}


def recent_state(status: str | None) -> str | None:
    return RECENT_STATES.get(status or "")


def live_link(link, now: float) -> str | None:
    """The PDF link, if it can still open.

    The engine signs it for a day (`r6/sdc/delivery.py`), so a link past its
    `exp` is dropped: a dead link reads as a broken product. Only http(s)
    becomes a link, since the page puts it in an href.
    """
    if not isinstance(link, str):
        return None
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    exp = parse_qs(parts.query).get("exp", [""])[0]
    if exp:
        try:
            if int(exp) <= now:
                return None
        except ValueError:
            return None
    return link


def switch_prompt(home: dict) -> dict | None:
    """The one "Switch Juniper to your records?" question, when it applies:
    an assistant reads the sample and a real connection is active.

    Asked, never done for the person: what the assistant reads changes only
    on their tap. The connect consent covers bringing records in, not which
    records a conversation already under way reads (#937). The count says
    that something arrived, so the question is worth answering."""
    by_id = {c["id"]: c for c in home["connections"]}
    real = [c for c in home["connections"]
            if c["kind"] != "sample" and c["status"] == "active"]
    if not real:
        return None
    for a in home["agents"]:
        c = by_id.get(a["connection_id"])
        if c and c["kind"] == "sample":
            return {"agent_id": a["id"], "agent_name": a["name"],
                    "connection_id": real[0]["id"], "label": real[0]["label"],
                    "count_line": count_line(real[0].get("last_count"))}
    return None
