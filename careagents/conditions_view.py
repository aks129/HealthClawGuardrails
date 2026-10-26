"""The conditions the agent is shown by default: current ones, once each.

A real problem list holds every condition ever recorded, resolved ones and
the same diagnosis entered at visit after visit. Handed over whole, it
filled the model's view with history and repeats, and the agent told a
person it could see only part of their conditions. The default view is
the active list, deduplicated by code, with a note that resolved ones
were held back.

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


#: The code systems whose codes name a condition, keyed as `coding_key`
#: canonicalises them. Only a shared code in one of these makes two records
#: one condition. A null flavor ("UNK"), a data-absent-reason ("unknown")
#: or a local code says nothing about which problem a record is, and
#: merging on one collapsed distinct problems into a single line (#1039 QA).
IDENTITY_SYSTEMS = frozenset({
    "http://snomed.info/sct",
    "http://hl7.org/fhir/sid/icd-10-cm",
    "http://hl7.org/fhir/sid/icd-10",
    "http://hl7.org/fhir/sid/icd-9-cm",
})


def _keys(res: dict, coding_key: Callable) -> set:
    """The record's codings that name a condition (IDENTITY_SYSTEMS)."""
    code = res.get("code")
    codings = code.get("coding") if isinstance(code, dict) else None
    return {k for k in map(coding_key, codings if isinstance(codings, list)
                           else []) if k and k[0] in IDENTITY_SYSTEMS}


def current_conditions(bundle: dict, coding_key: Callable,
                       withdrawn: Callable) -> tuple[dict, int]:
    """(the bundle with only current, distinct Conditions, how many
    distinct conditions were held back as not current).

    - Withdrawn (entered in error, refuted): out, and not counted; such a
      record says nothing about the person.
    - Not current: out, and counted only when its code is not also current
      (a resolved episode of a condition that is back is not history).
    - A repeat of a condition already kept: out. Two records are one
      condition only when they share a code from IDENTITY_SYSTEMS; a record
      with none is never a repeat, since nothing says which problem it is.

    "Newest" is the engine's order, `-_lastUpdated`: when the record last
    reached the engine (ingest time), not when the condition began. The
    first record of a condition in that order is the one kept. `total` drops
    by the records taken out, so a page that held the whole list still
    reads as whole, and a longer list stays cut.
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


def held_back_note() -> dict:
    """The list item that says resolved conditions exist and were left out.

    Shaped like the truncation marker, no `type` and no `name`, so it is
    never read as a condition. No number: what this page held back is not
    a count of the person's resolved conditions, and "shown X of Y" reads
    as being kept from one's own records (#939)."""
    return {
        "not_current": True,
        "note": ("Some resolved or inactive conditions are on file and are "
                 "not listed. They are past history, not current problems: "
                 "do not list them as current. If the person asks about "
                 "past or resolved conditions, say those entries are in "
                 "their records but not shown in this chat, and that the "
                 "full history is in their patient portal."),
    }


def collapse_unreadable(items: list[dict]) -> list[dict]:
    """One note for the unreadable conditions of each kind, not one each.

    Every unreadable item carries a sentence of about 330 characters, so
    200 of them came to about 67k characters. Each item stays, so it is
    still a real record and never absent; the first of a kind carries the
    note, with how many share it, and the rest carry none. A single item
    keeps its own note unchanged."""
    for kind, note in (("uncoded", _UNCODED_NOTE),
                       ("unlabeled", _UNLABELED_NOTE)):
        group = [i for i in items if i.get("unreadable")
                 and bool(i.get("uncoded")) == (kind == "uncoded")]
        if len(group) < 2:
            continue
        for i in group:
            i.pop("note", None)
        group[0]["note"] = note.format(n=len(group))
    return items


_UNLABELED_NOTE = (
    "This entry and the others marked unreadable ({n} in all) have no "
    "readable name here. Say plainly that their doctor can say what they "
    "are. Each is still a real record, never absent. Do not guess what they "
    "are or suggest they could be serious, and never say you can't rule "
    "anything out.")
_UNCODED_NOTE = (
    "This entry and the others marked uncoded ({n} in all) were sent by "
    "the source as free text with no standard code, and free text is "
    "removed for privacy. Each is still a real record, never absent; "
    "suggest confirming the details with the clinician.")
