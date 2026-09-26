"""The hub's view of an account: the words a person reads on /home.

Pure functions over `AccountService.list_home` output. No network and no
PHI: labels, statuses, counts and timestamps only (calm hub spec 3 and 7).
"""

from __future__ import annotations

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
    return {**c,
            "status_word": status_word(c.get("status")),
            "count_line": count_line(c.get("last_count")),
            "updated": updated_line(
                c.get("last_synced_at") or c.get("connected_at"), now),
            "is_sample": c.get("kind") == "sample"}


def build(home: dict, now: float) -> dict:
    conns = home["connections"]
    by_id = {c["id"]: c for c in conns}
    records = [_record(c, now) for c in conns if c["status"] != "revoked"]
    active = [r for r in records if r["status"] == "active"]
    return {
        "agents": [{**a, "reads": (by_id.get(a["connection_id"]) or {})
                    .get("label", "")} for a in home["agents"]],
        "records": records,
        "active_records": active,
        "move_choices": [{"id": r["id"], "label": r["label"]} for r in active],
        "past": [_record(c, now) for c in conns if c["status"] == "revoked"],
        "connected_kinds": sorted({r["kind"] for r in active}),
        "has_real": any(not r["is_sample"] for r in active),
    }


def switch_prompt(home: dict) -> dict | None:
    """The one "Switch Juniper to your records?" question, when it applies:
    an assistant reads the sample and a real connection is active."""
    by_id = {c["id"]: c for c in home["connections"]}
    real = [c for c in home["connections"]
            if c["kind"] != "sample" and c["status"] == "active"]
    if not real:
        return None
    for a in home["agents"]:
        c = by_id.get(a["connection_id"])
        if c and c["kind"] == "sample":
            return {"agent_id": a["id"], "agent_name": a["name"],
                    "connection_id": real[0]["id"], "label": real[0]["label"]}
    return None
