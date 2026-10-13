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
            "is_sample": c.get("kind") == "sample"}


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
    # Change records offers only the same person's records: an assistant
    # never moves to someone else's (one assistant per person spec, 9).
    # The hub's one list is the holder's; the per-person map is for the
    # card that renders people.
    by_person: dict = {}
    for r in active:
        by_person.setdefault(r.get("person_id"), []).append(
            {"id": r["id"], "label": r["label"]})
    me = next((p["id"] for p in home.get("people", [])
               if p["kind"] == "self"), None)
    return {
        "agents": [{**a, "reads": (by_id.get(a["connection_id"]) or {})
                    .get("label", "")} for a in home["agents"]],
        "records": records,
        "active_records": active,
        "move_choices": (by_person.get(me, []) if me is not None else
                         [{"id": r["id"], "label": r["label"]}
                          for r in active]),
        "move_choices_by_person": by_person,
        "past": [{**_record(c, now),
                  "readers": ", ".join(readers.get(c["id"], []))}
                 for c in conns if c["status"] == "revoked"],
        "connected_kinds": sorted({r["kind"] for r in active}),
        # Any real connection not revoked: one still connecting, or with no
        # records yet, is real all the same (#856 sign-off F3).
        "has_real": any(not r["is_sample"] for r in records),
    }


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
    an assistant reads the sample and a real connection is active."""
    by_id = {c["id"]: c for c in home["connections"]}
    real = [c for c in home["connections"]
            if c["kind"] != "sample" and c["status"] == "active"]
    if not real:
        return None
    # Only the holder's own assistant: someone they care for keeps their
    # own records (one assistant per person spec, section 9).
    others = {p["id"] for p in home.get("people", []) if p["kind"] != "self"}
    for a in home["agents"]:
        if a.get("person_id") in others:
            continue
        c = by_id.get(a["connection_id"])
        if c and c["kind"] == "sample":
            return {"agent_id": a["id"], "agent_name": a["name"],
                    "connection_id": real[0]["id"], "label": real[0]["label"]}
    return None


def assistants(home: dict) -> dict:
    """Each assistant with whose it is, its tone and its routines, for the
    hub card (one assistant per person spec, sections 4 and 8).

    `is_primary` is the person's oldest assistant on an active connection
    (their oldest one, if none is active). Any other assistant the same
    person has is `legacy_extra`: an account from before kept it, and the
    card shows its tone and routines with Remove, never Add (owner decision
    D5). `legacy_multi` says the account holds such an assistant at all.
    Labels only: a nickname, an enum, fixed names. No PHI.
    """
    from careagents.advisors import ROUTINES, focuses_on, routines_for
    from careagents.personas import DEFAULT_PERSONA, PERSONAS
    people = {p["id"]: p for p in home.get("people", [])}
    active = {c["id"] for c in home["connections"]
              if c["status"] == "active"}
    labels = {c["id"]: c.get("label") or "" for c in home["connections"]}
    by_person: dict = {}
    for a in sorted(home["agents"], key=lambda a: (
            a.get("connection_id") not in active, a.get("created_at") or 0)):
        by_person.setdefault(a.get("person_id"), []).append(a)
    out = []
    for person_id, agents in by_person.items():
        person = people.get(person_id) or {
            "id": person_id, "kind": "self", "relationship": None,
            "nickname": None}
        for i, a in enumerate(agents):
            ids = routines_for(a)
            tone = PERSONAS.get(a.get("persona"), PERSONAS[DEFAULT_PERSONA])
            out.append({**a, "person": person, "tone": tone["name"],
                        "reads": labels.get(a.get("connection_id"), ""),
                        "routines": [ROUTINES[r]["name"] for r in ids],
                        "focuses_on": focuses_on(ids),
                        "is_primary": i == 0, "legacy_extra": i > 0})
    return {"assistants": out, "people": list(people.values()),
            "legacy_multi": any(a["legacy_extra"] for a in out)}
