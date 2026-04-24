#!/usr/bin/env python3
"""
Kristy's schedule watcher.

Runs on the Mac mini as a LaunchAgent (default: every morning at 6:00am).
Pulls iCal feeds for the family's sports/activities, detects scheduling
conflicts in the next N days, and emits each NEW conflict as a pending
AgentTask to the Railway command center so it shows up on the dashboard's
Pending Tasks panel and in Kristy's daily bot nudges.

Idempotent: before creating a task, GETs the existing task list and skips
any conflict whose stable resource_ref already exists. Safe to run many
times a day.

Env (loaded from ~/.kristy/env first, then process env):
  FAMILY_ICAL_URLS        "Label|url,Label|url,..." — required
  FAMILY_HORIZON_DAYS     days ahead to scan (default: 28)
  FAMILY_ALLOWED_DAYS     lowercase weekday names (default: sun,mon,tue,wed,thu,sat)
  COMMAND_CENTER_API      base URL (default: https://app.healthclaw.io/command-center/api)
  STEP_UP_SECRET          required — mints the write token
  DEFAULT_TENANT          tenant id (default: ev-personal)
  KRISTY_AGENT_ID         (default: kristy)
  DRY_RUN                 truthy → parse + detect but skip POSTs

Exits 0 on clean run, 1 on config/network failure. Conflicts detected is
not an error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

import requests  # macOS system python has this; user-install if needed

logger = logging.getLogger("kristy.watcher")

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def _load_local_env(path: Path = Path.home() / ".kristy" / "env") -> None:
    """Merge a local dotenv-style file into os.environ (existing values win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def _allowed_weekdays() -> set[int]:
    raw = os.environ.get("FAMILY_ALLOWED_DAYS", "sun,mon,tue,wed,thu,sat")
    wanted = {w.strip().lower()[:3] for w in raw.split(",") if w.strip()}
    return {i for i, name in enumerate(_WEEKDAY_NAMES) if name in wanted}


def _ical_urls() -> list[tuple[str, str]]:
    raw = os.environ.get("FAMILY_ICAL_URLS", "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "|" not in entry:
            continue
        label, _, url = entry.partition("|")
        out.append((label.strip(), url.strip()))
    return out


# ---------------------------------------------------------------------------
# iCal fetch + parse
# ---------------------------------------------------------------------------

@dataclass
class Event:
    label: str              # "Henry Football"
    person: str             # "henry"  (first word of label, lowercase)
    kind: str               # "Game" | "Practice" | "Event"
    summary: str
    location: str
    starts: datetime
    ends: datetime

    @property
    def start_date(self) -> date:
        return self.starts.date()

    @property
    def location_norm(self) -> str:
        """Street-address-level identity — drops suite/field letter trailers."""
        loc = re.sub(r"\s+", " ", self.location.strip().lower())
        # Trim a trailing "- X" / "field a" / "turf c" / "court 2" qualifier
        loc = re.sub(r"\s*(-|—)\s*[a-z0-9]{1,3}\s*$", "", loc)
        loc = re.sub(r"\s+(field|turf|court|lot)\s+[a-z0-9]+\s*$", "", loc)
        return loc

    def stable_id(self) -> str:
        """SHA-1 fingerprint — same inputs always hash to same id."""
        key = f"{self.starts.isoformat()}|{self.label}|{self.summary}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]


def _extract_person(label: str) -> str:
    """"Henry Football" → "henry". Falls back to the full label."""
    first = label.split(maxsplit=1)[0].strip().lower()
    return first or label.strip().lower()


def _classify_kind(summary: str) -> str:
    if re.search(r"\bgame\b|\bmatch\b|\bvs\b|tournament|playoff", summary, re.I):
        return "Game"
    if re.search(r"\bpractice\b|\btraining\b|clinic|workout", summary, re.I):
        return "Practice"
    return "Event"


def fetch_events(
    urls: list[tuple[str, str]],
    today: date,
    horizon_days: int,
    allowed_weekdays: set[int],
    timeout: int = 20,
) -> list[Event]:
    """Fetch + parse + filter. Returns events in the [today, today+horizon] window."""
    # Lazy import so unit tests that only exercise pure logic don't require
    # icalendar to be installed in the test venv.
    from icalendar import Calendar

    end = today + timedelta(days=horizon_days)
    events: list[Event] = []

    for label, url in urls:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("fetch %s failed: %s", label, exc)
            continue
        try:
            cal = Calendar.from_ical(resp.text)
        except Exception as exc:
            logger.warning("parse %s failed: %s", label, exc)
            continue

        person = _extract_person(label)
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            if not dtstart:
                continue
            s = dtstart.dt
            e = dtend.dt if dtend else s
            # Normalize naive date → midnight UTC, keep tz otherwise
            if isinstance(s, date) and not isinstance(s, datetime):
                s = datetime(s.year, s.month, s.day, tzinfo=timezone.utc)
            if isinstance(e, date) and not isinstance(e, datetime):
                e = datetime(e.year, e.month, e.day, tzinfo=timezone.utc)
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)

            sd = s.date()
            if sd < today or sd > end:
                continue
            if sd.weekday() not in allowed_weekdays:
                continue

            summary = str(component.get("summary") or "")
            location = str(component.get("location") or "").replace("\n", " ").strip()
            events.append(Event(
                label=label,
                person=person,
                kind=_classify_kind(summary),
                summary=summary,
                location=location,
                starts=s,
                ends=e,
            ))

    events.sort(key=lambda ev: (ev.starts, ev.label))
    return events


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

@dataclass
class Conflict:
    kind: str                   # "same-person-double-booking" | "multi-person-overlap" | "tight-handoff"
    severity: str               # "critical" | "high" | "medium" | "low"
    title: str
    description: str
    events: list[Event]
    key_day: date
    stable_id: str = field(init=False)

    def __post_init__(self):
        # Stable ID = type + minute + sorted event fingerprints
        ev_ids = sorted(ev.stable_id() for ev in self.events)
        key = f"{self.kind}|{self.key_day.isoformat()}|{'+'.join(ev_ids)}"
        self.stable_id = hashlib.sha1(key.encode()).hexdigest()[:16]


def _overlaps(a: Event, b: Event) -> bool:
    return a.starts < b.ends and b.starts < a.ends


def _gap_minutes(earlier: Event, later: Event) -> int:
    return int((later.starts - earlier.ends).total_seconds() // 60)


def detect_conflicts(events: list[Event]) -> list[Conflict]:
    """
    Produce one Conflict per pair of overlapping (or tight-back-to-back) events.
    Three kinds:
      - same-person-double-booking — same person, overlapping, different locations
      - multi-person-overlap — different people, overlapping, different locations
      - tight-handoff — same or different people, gap < 30min at different locations
    """
    conflicts: list[Conflict] = []

    for a, b in combinations(events, 2):
        same_person = a.person == b.person
        same_location = a.location_norm == b.location_norm and a.location_norm != ""

        if _overlaps(a, b):
            if same_person and not same_location:
                conflicts.append(Conflict(
                    kind="same-person-double-booking",
                    severity="critical",
                    title=f"{a.person.title()} is double-booked {a.starts:%a %b %-d %-I:%M%p}",
                    description=(
                        f"{a.person.title()} has overlapping events at different locations:\n"
                        f"• {a.label}: {a.summary} @ {a.location or '?'} "
                        f"({a.starts:%I:%M%p}–{a.ends:%I:%M%p})\n"
                        f"• {b.label}: {b.summary} @ {b.location or '?'} "
                        f"({b.starts:%I:%M%p}–{b.ends:%I:%M%p})\n"
                        "Must pick one; suggest notifying the other coach."
                    ),
                    events=[a, b],
                    key_day=a.start_date,
                ))
            elif not same_person and not same_location:
                conflicts.append(Conflict(
                    kind="multi-person-overlap",
                    severity="medium",
                    title=f"{a.person.title()} + {b.person.title()} overlap {a.starts:%a %b %-d %-I:%M%p}",
                    description=(
                        f"Two family members have events at different locations at the same time:\n"
                        f"• {a.label}: {a.summary} @ {a.location or '?'} "
                        f"({a.starts:%I:%M%p}–{a.ends:%I:%M%p})\n"
                        f"• {b.label}: {b.summary} @ {b.location or '?'} "
                        f"({b.starts:%I:%M%p}–{b.ends:%I:%M%p})\n"
                        "Plan: two drivers, or carpool one with a teammate."
                    ),
                    events=[a, b],
                    key_day=a.start_date,
                ))
            # If same location → no conflict (same complex, different fields is fine)
            continue

        # Tight handoff — events in order, small gap, different locations
        earlier, later = (a, b) if a.ends <= b.starts else (b, a)
        gap = _gap_minutes(earlier, later)
        if 0 <= gap <= 30 and earlier.location_norm != later.location_norm \
           and earlier.start_date == later.start_date:
            conflicts.append(Conflict(
                kind="tight-handoff",
                severity="low",
                title=f"Tight handoff {earlier.ends:%a %-I:%M%p}→{later.starts:%-I:%M%p} "
                      f"({gap}min)",
                description=(
                    f"Only {gap} minutes between:\n"
                    f"• {earlier.label}: ends {earlier.ends:%I:%M%p} @ {earlier.location or '?'}\n"
                    f"• {later.label}: starts {later.starts:%I:%M%p} @ {later.location or '?'}\n"
                    "Plan the drive in advance."
                ),
                events=[earlier, later],
                key_day=earlier.start_date,
            ))

    return conflicts


# ---------------------------------------------------------------------------
# Step-up token minting — matches r6/stepup.py's format
# ---------------------------------------------------------------------------

def _mint_step_up_token(tenant_id: str, agent_id: str) -> str:
    """
    HMAC-SHA256 step-up token in r6.stepup's format:
        base64url(json({exp, tid, sub, nonce})) + "." + hmac_sha256_hex
    5-minute TTL. Server validates with the same STEP_UP_SECRET.
    """
    import base64
    secret = os.environ.get("STEP_UP_SECRET") or ""
    if not secret:
        raise RuntimeError("STEP_UP_SECRET not set — cannot write to command center")
    payload = {
        "exp": int(time.time()) + 300,
        "tid": tenant_id,
        "sub": agent_id or "system",
        "nonce": secrets.token_hex(16),
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


# ---------------------------------------------------------------------------
# Command-center task dedup + emit
# ---------------------------------------------------------------------------

def _task_signature(c: Conflict) -> str:
    """
    Embedded in AgentTask.resource_ref so we can dedupe idempotently.
    Format: "family-conflict:<stable_id>"
    """
    return f"family-conflict:{c.stable_id}"


def list_existing_conflict_refs(api_base: str, tenant_id: str, token: str) -> set[str]:
    """GET /api/tasks and return the set of resource_refs that look like our conflicts."""
    try:
        resp = requests.get(
            f"{api_base}/tasks",
            params={"tenant": tenant_id, "limit": 100},
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant_id},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("tasks GET failed: %s", exc)
        return set()
    return {
        (t.get("resource_ref") or "")
        for t in resp.json()
        if (t.get("resource_ref") or "").startswith("family-conflict:")
    }


def post_conflict_task(
    api_base: str, tenant_id: str, agent_id: str, c: Conflict, token: str
) -> bool:
    body = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "title": c.title,
        "description": c.description,
        "priority": c.severity if c.severity in ("low", "medium", "high", "critical") else "medium",
        "resource_ref": _task_signature(c),
        "source": "kristy-watcher",
    }
    try:
        resp = requests.post(
            f"{api_base}/tasks",
            json=body,
            headers={"X-Step-Up-Token": token, "X-Tenant-Id": tenant_id},
            timeout=15,
        )
    except Exception as exc:
        logger.error("POST failed for %s: %s", c.stable_id, exc)
        return False
    if resp.status_code == 201:
        return True
    logger.error("POST HTTP %s for %s: %s", resp.status_code, c.stable_id, resp.text[:200])
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _load_local_env()

    urls = _ical_urls()
    if not urls:
        logger.error("FAMILY_ICAL_URLS is unset or empty — nothing to do")
        return 1

    horizon = int(os.environ.get("FAMILY_HORIZON_DAYS", "28"))
    api_base = os.environ.get(
        "COMMAND_CENTER_API",
        "https://app.healthclaw.io/command-center/api",
    ).rstrip("/")
    tenant = os.environ.get("DEFAULT_TENANT", "ev-personal")
    agent_id = os.environ.get("KRISTY_AGENT_ID", "kristy")
    dry_run = bool(os.environ.get("DRY_RUN"))

    today = date.today()
    events = fetch_events(urls, today, horizon, _allowed_weekdays())
    logger.info("fetched %d events across %d feeds", len(events), len(urls))

    conflicts = detect_conflicts(events)
    logger.info("detected %d conflicts", len(conflicts))

    if dry_run:
        for c in conflicts:
            logger.info("DRY-RUN conflict [%s] %s (%s)",
                        c.severity, c.title, c.stable_id)
        return 0

    # Mint a single step-up token for the whole run (5-min TTL)
    try:
        token = _mint_step_up_token(tenant, agent_id)
    except Exception as exc:
        logger.error("could not mint step-up token: %s", exc)
        return 1

    existing = list_existing_conflict_refs(api_base, tenant, token)
    logger.info("%d existing family-conflict tasks in command center", len(existing))

    created = skipped = failed = 0
    for c in conflicts:
        ref = _task_signature(c)
        if ref in existing:
            skipped += 1
            continue
        if post_conflict_task(api_base, tenant, agent_id, c, token):
            created += 1
        else:
            failed += 1

    logger.info(
        "run complete: events=%d conflicts=%d created=%d skipped=%d failed=%d",
        len(events), len(conflicts), created, skipped, failed,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
