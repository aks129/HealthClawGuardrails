"""The conditions the agent is shown by default: current ones, once each.

A real problem list holds every condition ever recorded, resolved ones and
the same diagnosis entered at visit after visit. Handed over whole, it
filled the model's view with history and repeats, and the agent told a
person it could see only part of their conditions. The default view is
the active list, deduplicated by code, with a count of what was held back.

Works on the redacted searchset bundle before `agent._summarize_bundle`
names anything, so no label or text is read here: only codes and statuses.
"""

from __future__ import annotations

from typing import Callable

#: Engine page asked for: its ceiling (r6/routes.py clamps `_count` to 200).
#: At the default 50, a longer problem list was cut before the filter below
#: could take its resolved entries out.
CONDITION_PAGE = 200

CLINICAL_SYSTEM = ("http://terminology.hl7.org/CodeSystem/"
                   "condition-clinical")
#: Condition.clinicalStatus codes that put a record out of the default view
#: (FHIR R4-R6 condition-clinical). `active`, `recurrence`, `relapse` and
#: `unknown` stay, and so does a record with no status: an unstated status
#: is not a resolved condition.
NOT_CURRENT = frozenset({"inactive", "remission", "resolved"})
CURRENT = frozenset({"active", "recurrence", "relapse"})


def _clinical_codes(res: dict) -> set[str]:
    """The record's clinical-status codes, from the condition-clinical
    system or a coding with no system; STU3 wrote a bare code."""
    cs = res.get("clinicalStatus")
    if isinstance(cs, str):
        return {cs.strip()}
    codings = cs.get("coding") if isinstance(cs, dict) else None
    return {c["code"].strip() for c in (codings if isinstance(codings, list)
                                        else [])
            if isinstance(c, dict) and isinstance(c.get("code"), str)
            and c.get("system") in (None, CLINICAL_SYSTEM)}


def not_current(res: dict) -> bool:
    """Stated inactive, in remission or resolved, and nothing current
    beside it. Conflicting codes keep the record in view."""
    codes = _clinical_codes(res)
    return bool(codes & NOT_CURRENT) and not codes & CURRENT


def _keys(res: dict, coding_key: Callable) -> set:
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    return {k for k in map(coding_key, codings if isinstance(codings, list)
                           else []) if k}


def current_conditions(bundle: dict, coding_key: Callable,
                       withdrawn: Callable) -> tuple[dict, int]:
    """(the bundle with only current, distinct Conditions, how many
    distinct conditions were held back as not current).

    - Withdrawn (entered in error, refuted): out, and not counted; such a
      record says nothing about the person.
    - Not current: out, counted once per code, and not at all when the same
      code is current too (a resolved episode of a condition that is back).
    - A repeat of a code already kept: out. Two records are one condition
      when they share any (system, code) pair; a record with no code we
      can key on is never a repeat, since nothing says what it is.

    The engine sorts newest first, so the newest record of a condition is
    the one kept. `total` drops by the records taken out, so a page that
    held the whole list still reads as whole, and a longer list stays cut.
    """
    entries = bundle.get("entry") if isinstance(bundle, dict) else None
    entries = entries if isinstance(entries, list) else []
    kept, seen, removed = [], set(), 0
    held: list[set] = []
    for entry in entries:
        res = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(res, dict) or res.get("resourceType") != "Condition":
            kept.append(entry)
            continue
        keys = _keys(res, coding_key)
        if withdrawn(res):
            removed += 1
        elif not_current(res):
            removed += 1
            held.append(keys)
        elif keys & seen:
            removed += 1
        else:
            seen |= keys
            kept.append(entry)
    held_back, held_seen = 0, set()
    for keys in held:
        if keys and keys & (seen | held_seen):
            continue
        held_seen |= keys
        held_back += 1
    out = {**bundle, "entry": kept}
    stated = bundle.get("total")
    if isinstance(stated, int) and not isinstance(stated, bool):
        out["total"] = max(stated - removed, 0)
    return out, held_back


def held_back_note(count: int) -> dict:
    """The list item that says resolved conditions exist and were left out.

    Shaped like the truncation marker, no `type` and no `name`, so it is
    never read as a condition. A count of the record's own resolved
    entries, never "shown X of Y": that reads as being kept from one's own
    records (#939)."""
    return {
        "not_current": count,
        "note": (f"{count} other condition record(s) on file are marked "
                 "resolved, inactive or in remission and are not listed. "
                 "They are past history, not current problems: do not list "
                 "them as current. If the person asks about past or "
                 "resolved conditions, say those entries are in their "
                 "records but not shown in this chat, and that the full "
                 "history is in their patient portal."),
    }
