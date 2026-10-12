"""Which Claude model answers a chat turn (CARE_ROUTING=on).

Two tiers, chosen per turn without an extra model call:

  Haiku   the easiest turns only: greetings, thanks, what the assistant can
          do, and how to use the app (connecting records, passkeys, texting,
          deleting the account, feedback). Low effort, no advisor.
  Sonnet  everything else, at CARE_EFFORT, with an Opus advisor it can
          consult on the hard ones (CARE_ADVISOR, on by default).

The classifier is an allowlist on purpose. Answering a clinical question on
the small model is the failure that matters, so a message reaches Haiku only
when it looks like small talk or an app question AND carries no number and no
clinical word. Anything else, including anything ambiguous ("yes", "tell me
more", another language), goes to Sonnet. Haiku is still offered the tools,
and a Haiku reply that reaches for a clinical tool is thrown away and the
call re-run on Sonnet (careagents/llm.py), so a misroute costs a little
latency, never a clinical answer from the small model.

Also here: the per-UTC-day integer counts that say how routing behaves in
production. Metric names are fixed in this module; nothing a person wrote
can become a row.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import threading

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

TIER_HAIKU = 1
TIER_SONNET = 2

HAIKU_MODEL = "claude-haiku-5-5"
SONNET_MODEL = "claude-sonnet-5-5"
ADVISOR_MODEL = "claude-opus-5-5"

#: Server-side advisor (beta). Opus 5.5 returns its advice encrypted
#: (advisor_redacted_result): the executor reads it, we never can. Two
#: consultations per call at most; 3000 tokens covers the advisor's thinking
#: and its advice (about 600-850 in the 2026-10-12 probe).
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": ADVISOR_MODEL,
    "max_uses": 2,
    "max_tokens": 3000,
    "caching": {"type": "ephemeral", "ttl": "5m"},
}
ADVISOR_BETA = "advisor-tool-2026-03-01"
#: Refusal fallback on Sonnet 5.5 (Claude API only). It retries only the
#: cyber and frontier_llm categories; a health-adjacent decline still comes
#: back as a refusal and is answered with llm.REFUSAL_TEXT.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Appended to the system prompt on Sonnet when the advisor is offered.
#: Forced tool_choice is a 400 on Sonnet 5.5, so the nudge is in words.
ADVISOR_NOTE = (
    "\n\nYou can consult a more careful reviewer with the advisor tool. "
    "Consult it before you answer when the question needs several "
    "conditions read together, a trend across several labs, anything about "
    "medicines, or when you are unsure whether something is safe or urgent. "
    "The advisor is internal: never mention it, and never say you are "
    "checking with anyone. Answer the person directly.")

#: Every tool that reads or acts on the person's health record. Haiku calling
#: one of these means the turn was clinical after all.
CLINICAL_TOOLS = frozenset({
    "get_health_summary", "get_labs", "show_lab_timeline", "get_care_gaps",
    "search_records", "appointment_brief", "start_intake_form"})

#: Longer than this is not small talk.
_MAX_HAIKU_CHARS = 160

#: Words that make a message clinical wherever they appear. Matched on word
#: starts, so "med" covers meds/medication/medicine and "allerg" allergy.
_CLINICAL = re.compile(r"\b(?:" + "|".join([
    r"labs?\b", r"results?\b", r"tests?\b", r"tested", r"a1c", r"hba1c",
    r"cholesterol", r"ldl", r"hdl", r"triglycer", r"glucose", r"sugar",
    r"potassium", r"sodium", r"kidney", r"liver", r"thyroid", r"tsh",
    r"egfr", r"gfr", r"creatinine", r"blood", r"pressure", r"bp\b",
    r"pulse", r"heart", r"chest", r"pain", r"hurt", r"ache", r"sore",
    r"fever", r"cough", r"dizz", r"nause", r"vomit", r"bleed", r"rash",
    r"headache", r"migraine", r"breath", r"swell", r"tired", r"fatigue",
    r"sick", r"ill\b", r"symptom", r"med", r"pill", r"dose", r"dosage",
    r"mg\b", r"mcg", r"units?\b", r"prescri", r"refill", r"drug",
    r"insulin", r"metformin", r"statin", r"side.?effect", r"condition",
    r"diagnos", r"disease", r"diabet", r"hypertens", r"asthma", r"cancer",
    r"infect", r"allerg", r"vaccin", r"immuni", r"shots?\b", r"screen",
    r"records?\b", r"chart", r"history", r"visit", r"appointment",
    r"doctor", r"dr\b", r"nurse", r"clinic", r"hospital", r"er\b",
    r"emergenc", r"urgent", r"health", r"weight", r"bmi", r"pregnan",
    r"surgery", r"procedure", r"form\b", r"forms\b", r"intake", r"care\b",
    r"gaps?\b", r"due\b", r"worr", r"normal", r"range", r"level",
    r"value", r"trend", r"mean\b", r"means\b", r"brief",
]) + r")", re.IGNORECASE)

#: "connect my records" is an app question, so that one phrase is taken out
#: before the clinical scan. The rest of the message is still scanned.
_HOW_TO_RECORDS = re.compile(
    r"\b(?:connect|link|add|import|upload|sync|disconnect)(?:ing|ed|s)?\s+"
    r"(?:my\s+|the\s+|your\s+|our\s+)?"
    r"(?:(?:real|own|other|health|medical)\s+){0,2}records?\b",
    re.IGNORECASE)

#: Messages made only of these words are small talk.
_SMALL_TALK = frozenset("""
    hi hello hey hiya howdy yo there good morning afternoon evening night
    thanks thank thankyou thx ty you so much very really a lot
    ok okay k cool great awesome perfect nice lovely got it
    bye goodbye see ya later cheers appreciate that's thats sounds
    all right alright have day
""".split())

#: An app or capability question. Only consulted once the message has no
#: number and no clinical word.
_APP_TOPIC = re.compile(r"|".join([
    r"\bwhat (?:can|do|else can) you do\b", r"\bwho are you\b",
    r"\bwhat are you\b", r"\bhow (?:does|do) (?:this|it|you) work",
    r"\bhow can you help\b", r"\bwhat is (?:this|careagents)\b",
    r"\bconnect", r"\bpasskey", r"\bsign(?:ing)? (?:in|up|out)\b",
    r"\blog(?:ging)? ?(?:in|out)\b", r"\bpassword", r"\btext(?:ing)?\b",
    r"\bimessage", r"\bsms\b", r"\bphone\b", r"\baccount\b",
    r"\bnotification", r"\bsettings?\b", r"\bfeedback\b", r"\bbug\b",
    r"\bemail\b", r"\binvite", r"\bprivacy\b",
]), re.IGNORECASE)


def classify(text: str) -> int:
    """TIER_HAIKU for small talk and app questions, else TIER_SONNET."""
    t = (text or "").strip()
    if not t or len(t) > _MAX_HAIKU_CHARS or re.search(r"\d", t):
        return TIER_SONNET
    if _CLINICAL.search(_HOW_TO_RECORDS.sub(" ", t)):
        return TIER_SONNET
    words = re.findall(r"[a-z']+", t.lower())
    if words and all(w in _SMALL_TALK for w in words):
        return TIER_HAIKU
    if _APP_TOPIC.search(t):
        return TIER_HAIKU
    return TIER_SONNET


def tier_for(messages: list[dict]) -> int:
    """The tier for the next model call of the current turn.

    The current turn starts at the last plain user message. Once that turn
    has called a clinical tool it stays on Sonnet; otherwise the user's
    message decides. A turn with no user message is not small talk."""
    start = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            start = i
            break
    if start is None:
        return TIER_SONNET
    for m in messages[start + 1:]:
        for call in m.get("tool_calls") or []:
            if call.get("name") in CLINICAL_TOOLS:
                return TIER_SONNET
    return classify(messages[start]["content"])


# --- daily counts ------------------------------------------------------------

#: The only metric names that may be stored.
METRICS = frozenset({
    "turns_haiku", "turns_sonnet", "turns_single", "escalations",
    "advisor_consults", "advisor_errors", "refusals", "cut_offs"})


def _utc_day(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%d")


def record(session_scope, deltas: dict, now: _dt.datetime | None = None
           ) -> None:
    """Add each non-zero delta to today's count for its metric.

    Increment in the database first, insert on a miss, and on a racing
    insert increment the row the other writer made (analytics.record_view's
    pattern: several workers answer turns)."""
    from careagents.models import ModelUsageDay
    unknown = set(deltas) - METRICS
    if unknown:
        raise ValueError(f"unknown metric {sorted(unknown)!r}")
    day = _utc_day(now)
    for metric, n in deltas.items():
        n = int(n)
        if n <= 0:
            continue
        stmt = (update(ModelUsageDay)
                .where(ModelUsageDay.day == day,
                       ModelUsageDay.metric == metric)
                .values(count=ModelUsageDay.count + n))
        with session_scope() as s:
            if s.execute(stmt).rowcount:
                continue
        try:
            with session_scope() as s:
                s.add(ModelUsageDay(day=day, metric=metric, count=n))
        except IntegrityError:
            with session_scope() as s:
                s.execute(stmt)


def counts(session_scope, days: int = 7, now: _dt.datetime | None = None
           ) -> list[tuple[str, str, int]]:
    """(day, metric, count) for the last `days` UTC days, newest first."""
    from careagents.models import ModelUsageDay
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    since = _utc_day(moment - _dt.timedelta(days=days - 1))
    with session_scope() as s:
        rows = (s.query(ModelUsageDay)
                .filter(ModelUsageDay.day >= since)
                .order_by(ModelUsageDay.day.desc(), ModelUsageDay.metric)
                .all())
        return [(r.day, r.metric, r.count or 0) for r in rows]


_scopes: dict[str, object] = {}
_scopes_lock = threading.Lock()


def _scope_for(database_url: str):
    """One session scope per database per process, made on first use."""
    with _scopes_lock:
        scope = _scopes.get(database_url)
        if scope is None:
            from contextlib import contextmanager

            from careagents.models import make_engine, make_session_factory
            factory = make_session_factory(make_engine(database_url))

            @contextmanager
            def scope():
                s = factory()
                try:
                    yield s
                    s.commit()
                except Exception:
                    s.rollback()
                    raise
                finally:
                    s.close()
            _scopes[database_url] = scope
        return scope


def count(cfg, deltas: dict) -> None:
    """Best effort: a count is never a reason a turn fails."""
    url = getattr(cfg, "database_url", "") or ""
    if not url or not any(deltas.values()):
        return
    try:
        record(_scope_for(url), deltas)
    except Exception:  # noqa: BLE001 - counting must not fail a turn
        logger.warning("model usage counting failed", exc_info=True)
