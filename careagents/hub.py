"""The hub's view of an account: the words a person reads on /home.

Pure functions over `AccountService.list_home` output. No network and no
PHI: labels, statuses, counts and timestamps only (calm hub spec 3 and 7).
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def _plain(word: str) -> str:
    # "Connecting...", "connecting…" and "Connecting…" are one word here.
    return word.strip().lower().replace("...", "…")


def subtitle(c: dict) -> str:
    """The line under a source's name: its provider, unless the name
    already says it ("Records from your doctor" over "your doctor", patient
    tester on #940). Never a status word: the chip is the one place a row
    says where it stands, so a "Connecting…" left in the provider field
    can't sit under a "Connected" chip (hub critique 2026-10)."""
    provider = (c.get("provider") or "").strip()
    if (not provider or provider.lower() in (c.get("label") or "").lower()
            or _plain(provider) in {_plain(w) for w in STATUS_WORDS.values()}):
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
    # Whose records each source is, once someone the holder cares for is on
    # the account: two "Sample records" rows are otherwise twins (patient
    # tester on #951). One person: nothing new on the row.
    several = any(p["kind"] == "other" for p in home.get("people", []))

    def _whose(c):
        if not several:
            return ""
        return (whose_records(person_of(home, c.get("person_id")))
                or "Your records")
    # Deleting the last source of someone the holder cares for removes them
    # from the hub too (accounts._drop_empty_other): the dialog says so
    # before the tap (FHI-45).
    sources_of: dict = {}
    for c in conns:
        sources_of[c.get("person_id")] = sources_of.get(
            c.get("person_id"), 0) + 1

    def _leaves(c):
        person = person_of(home, c.get("person_id"))
        if person.get("kind") != "other" or sources_of[c.get(
                "person_id")] > 1:
            return ""
        return person_name(person) + " will be removed from your hub too."
    records = [{**_record(c, now), "whose": _whose(c), "leaves": _leaves(c),
                "readers": ", ".join(readers.get(c["id"], []))}
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
        "past": [{**_record(c, now), "whose": _whose(c),
                  "leaves": _leaves(c), "readers": ", ".join(readers.get(c["id"], []))}
                 for c in conns if c["status"] == "revoked"],
        "connected_kinds": sorted({r["kind"] for r in active}),
        # Any real connection not revoked: one still connecting, or with no
        # records yet, is real all the same (#856 sign-off F3).
        "has_real": any(not r["is_sample"] for r in records),
        # The source list (hub critique 2026-10): made-up rows step aside
        # once real records are here, and an empty upload shows once.
        "sources": _tell_apart(_sources(records)),
        "made_up_sources": _tell_apart(
            [r for r in records if r["is_sample"]]
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
    records (they wait in a quiet drawer instead). Nothing else is hidden:
    a hidden empty upload made Delete look like it did nothing (patient
    tester on #944). A second Upload tap reuses the empty row instead
    (_start_connection), and old twins are told apart by _tell_apart."""
    hide_samples = _real_has_records(records)
    return [r for r in records if not (r["is_sample"] and hide_samples)]


def _day(ts: float | None) -> str:
    if ts is None:
        return ""
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d:%b} {d.day}"


def _tell_apart(rows: list[dict]) -> list[dict]:
    """Rows that share a name say when each was added, so three
    "Records from your doctor" can be told apart (QA #944 F4). Two made the
    same day are numbered as well. A connect date is account bookkeeping,
    not a health detail. A name nobody else has keeps its line as it was.
    "Added", never "Connected": under a "Connecting…" chip that word is the
    very contradiction the chip exists to prevent."""
    by_label: dict = {}
    # Rows whose person line already differs are told apart by it, so
    # only one person's same-named rows are dated and numbered (#951).
    for r in rows:
        by_label.setdefault((r["label"], r.get("whose", "")), []).append(r)
    notes = {}
    for same in by_label.values():
        if len(same) < 2:
            continue
        seen: dict = {}
        for r in sorted(same, key=lambda r: r.get("connected_at") or 0):
            day = _day(r.get("connected_at"))
            seen[day] = seen.get(day, 0) + 1
            note = " ".join(x for x in (
                "Added", day,
                f"({seen[day]})" if seen[day] > 1 else "") if x)
            notes[r["id"]] = f"{r['sub']} · {note}" if r["sub"] else note
    return [{**r, "sub": notes.get(r["id"], r["sub"])} for r in rows]


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
    # The newest arrival first, so the question names one source every
    # time, and the one What's new would otherwise say again (QA #944 F3).
    real = sorted((c for c in home["connections"]
                   if c["kind"] != "sample" and c["status"] == "active"),
                  key=lambda c: c.get("connected_at") or 0, reverse=True)
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
                    "connection_id": real[0]["id"], "label": real[0]["label"],
                    "count_line": count_line(real[0].get("last_count"))}
    return None


#: How a card names someone you care for who has no nickname. Keyed by the
#: relationship enum (models.RELATIONSHIPS); anything else reads as
#: _ANYONE, never as the raw value, and "other" is never "your other".
_FOR_RELATION = {"parent": "your parent", "child": "your child",
                 "partner": "your partner"}
_ANYONE = "someone you care for"


def for_line(person: dict, several: bool) -> str:
    """The line under a card's name: whose assistant it is.

    The account holder's has none, unless someone they care for is on the
    account too: then "For you", so the cards are told apart. Someone they
    care for is their nickname (the holder's own words, escaped by the
    template) or their relationship. Labels only (spec section 2.1)."""
    if person.get("kind") != "other":
        return "For you" if several else ""
    return "For " + (person.get("nickname")
                     or _FOR_RELATION.get(person.get("relationship"),
                                          _ANYONE))


def relation_of(person: dict) -> str:
    """Who someone the holder cares for is, from the relationship enum
    alone: "your parent", "someone you care for". Empty for the holder.

    The one person wording that may reach the model: the system prompt, a
    starter tap and the sample line every answer opens with (FHI-44). The
    nickname is the holder's own typed text and is never read here."""
    if person.get("kind") != "other":
        return ""
    return _FOR_RELATION.get(person.get("relationship"), _ANYONE)


def person_name(person: dict) -> str:
    """Someone the holder cares for as a page names them, starting a
    sentence: their nickname ("Mom") or relationship ("Your parent").
    Page text only; never for the model (use relation_of)."""
    name = person.get("nickname") or relation_of(person)
    return name[:1].upper() + name[1:]


def whose_records(person: dict, mid: bool = False) -> str:
    """"Mom's records", for the chat header, the approvals page, the
    waiting band and the source rows (spec section 5). Empty for the
    account holder: their own records need no name. `mid` is the
    mid-sentence form ("Requests for your child's records")."""
    if person.get("kind") != "other":
        return ""
    nick = person.get("nickname")
    if nick:
        return f"{nick}'s records"
    rel = _FOR_RELATION.get(person.get("relationship"))
    if rel:
        return f"{rel if mid else rel[0].upper() + rel[1:]}'s records"
    return ("the records of " if mid else "Records of ") + _ANYONE


#: The tone a card shows, in plain words, by persona key (patient tester
#: on #951): "Tone: calm", not the persona's label.
TONE_WORDS = {"calm": "calm", "direct": "direct", "sunny": "cheerful"}


def person_of(home: dict, person_id: str | None) -> dict:
    """The person with this id, or the account holder when none matches: a
    row from before people existed is the holder's (spec section 4)."""
    return next((p for p in home.get("people", []) if p["id"] == person_id),
                {"id": person_id, "kind": "self", "relationship": None,
                 "nickname": None})


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
    several = any(p["kind"] == "other" for p in people.values())
    # The holder's assistants first (the primary leads, its extras under
    # it), then each person they care for in the order they were added.
    # An unknown person id is the holder's, as person_of reads it.
    added = list(people)

    def _rank(person_id):
        p = people.get(person_id)
        if p is None or p["kind"] != "other":
            return (0, 0)
        return (1, added.index(person_id))
    out = []
    for person_id, agents in sorted(by_person.items(),
                                    key=lambda kv: _rank(kv[0])):
        person = person_of(home, person_id)
        for i, a in enumerate(agents):
            ids = routines_for(a)
            tone = PERSONAS.get(a.get("persona"), PERSONAS[DEFAULT_PERSONA])
            out.append({**a, "person": person, "tone": tone["name"],
                        "tone_word": TONE_WORDS.get(
                            a.get("persona"), TONE_WORDS[DEFAULT_PERSONA]),
                        "reads": labels.get(a.get("connection_id"), ""),
                        "routines": [ROUTINES[r]["name"] for r in ids],
                        "focuses_on": focuses_on(ids),
                        "for_line": for_line(person, several),
                        "is_primary": i == 0, "legacy_extra": i > 0})
    return {"assistants": out, "people": list(people.values()),
            "legacy_multi": any(a["legacy_extra"] for a in out),
            # The holder has an assistant of their own: "Add someone you
            # care for" waits for it (patient tester on #951).
            "has_own": any(a["person"]["kind"] != "other" for a in out)}
