"""Which Claude model answers a chat turn (CARE_ROUTING=on).

Two tiers, chosen per turn without an extra model call:

  Haiku   the easiest turns only: greetings, thanks, what the assistant can
          do, and how to use the app (connecting records, passkeys, texting,
          deleting the account, feedback). Low effort, no advisor, and it
          sees only the current message, never earlier answers.
  Sonnet  everything else, at CARE_EFFORT, with an Opus advisor it may
          consult once per turn (CARE_ADVISOR, on by default).

The classifier is a tight allowlist. A message reaches Haiku only when the
WHOLE message, once normalised, is small talk or one of a short list of app
questions written out below. Any other word, any number, any letter outside
plain ASCII, or anything that reads as a crisis or an emergency sends it to
Sonnet. A word list of clinical terms was tried first and lost (#931
security review F1): "can I text you at night when I feel like ending it
all" reached Haiku, because no list of bad words is ever complete, and the
escalation net below only catches questions that need the record.

Haiku is still offered the tools, and a Haiku reply that reaches for a
clinical tool is thrown away and the call re-run on Sonnet
(careagents/llm.py).

Also here: the per-UTC-day integer counts that say how routing behaves in
production. Metric names are fixed in this module; nothing a person wrote
can become a row.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import unicodedata

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

TIER_HAIKU = 1
TIER_SONNET = 2

HAIKU_MODEL = "claude-haiku-5-5"
SONNET_MODEL = "claude-sonnet-5-5"
ADVISOR_MODEL = "claude-opus-5-5"

#: Server-side advisor (beta). Opus 5.5 returns its advice encrypted
#: (advisor_redacted_result): the executor reads it, we never can. One
#: consultation per call, and the advisor is offered only until the turn
#: has used it (advisor_used), so a turn consults Opus at most once however
#: many rounds its tool loop runs (#931 F5). 3000 tokens covers the
#: advisor's thinking and its advice (about 600-850 in the 2026-10-12 probe).
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": ADVISOR_MODEL,
    "max_uses": 1,
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
_MAX_HAIKU_CHARS = 120

#: Crisis and emergency language. Never Haiku, whatever else the message
#: says. The allowlist already refuses these; this is the rule stated out
#: loud, so a later template cannot quietly admit one.
_CRISIS = re.compile(r"|".join([
    r"suicid", r"kill(?:ing)? my ?self", r"end(?:ing)? (?:it|my life|it all)",
    r"(?:want to|wanna|going to|gonna) die", r"don'?t want to (?:live|be here)",
    r"self.?harm", r"hurt(?:ing)? my ?self", r"cut(?:ting)? my ?self",
    r"overdos", r"\bod\b", r"can'?t breathe", r"cannot breathe",
    r"can not breathe", r"chest pain", r"heart attack", r"stroke",
    r"seizure", r"unconscious", r"passed out", r"faint", r"bleeding",
    r"911", r"988", r"emergenc", r"\bem?r\b", r"ambulance", r"crisis",
    r"abuse", r"\bunsafe\b",
]))

#: Whole messages made only of these words are small talk.
_SMALL_TALK = frozenset("""
    hi hello hey hiya howdy yo there good morning afternoon evening night
    thanks thank thankyou thx ty you so much very really a lot
    ok okay k cool great awesome perfect nice lovely got it
    bye goodbye see ya later cheers appreciate that's thats sounds
    all right alright have day
""".split())

#: Stripped from the ends of a message before the templates are tried.
_LEAD = frozenset("hi hello hey hiya yo ok okay so um thanks".split())
_TRAIL = frozenset("please thanks".split())

_Q = (r"(?:how do i|how can i|how would i|how to|can i|where do i|"
      r"where can i|is it possible to)")

#: The app questions Haiku may answer, as whole messages. Matched against
#: the normalised words of the message joined by single spaces, after the
#: lead and trail words above are taken off. Nothing else may be present.
_TEMPLATES = re.compile(r"|".join(f"(?:{t})" for t in [
    rf"{_Q} (?:connect|link|add|import) (?:my |the )?"
    r"(?:(?:real|own|other|health|medical) ){0,2}records?",
    rf"{_Q} (?:add|set up|setup|create|use|make|get) (?:a |my )?passkeys?",
    rf"{_Q} (?:sign|log) (?:in|out|up)"
    r"(?: on (?:my |a )?(?:phone|laptop|computer|ipad|tablet))?",
    rf"{_Q} (?:delete|close|remove|cancel) (?:my )?account",
    r"(?:can i|how do i|how can i) (?:text|message|imessage) you"
    r"(?: from my phone)?",
    r"can i use (?:imessage|texting|text|sms)(?: with (?:this|you))?",
    r"what (?:else )?can you do(?: for me)?", r"what do you do",
    r"who are you", r"what are you", r"how (?:does|do) (?:this|it|you) work",
    r"how can you help(?: me)?", r"what is (?:this|careagents)",
    r"i (?:have|got) (?:some )?feedback"
    r"(?: (?:about|on|for) (?:the |this )?app)?",
    rf"{_Q} (?:send|give|leave|share) (?:you )?feedback",
]))


def normalize(text: str) -> str:
    """NFKC, invisible format characters removed, casefolded.

    NFKC folds fullwidth and other compatibility letters to ASCII, and the
    format characters (zero-width space, joiners, bidi marks) are what split
    a word so a scan misses it. Letters that are still not ASCII afterwards
    (a Cyrillic er that looks like p) are left for classify to refuse."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    return t.replace("’", "'").casefold().strip()


def is_crisis(text: str) -> bool:
    return bool(_CRISIS.search(normalize(text)))


def classify(text: str) -> int:
    """TIER_HAIKU only for whole-message small talk and listed app
    questions; TIER_SONNET for everything else."""
    t = normalize(text)
    if (not t or len(t) > _MAX_HAIKU_CHARS or re.search(r"\d", t)
            or any(ch.isalpha() and not ch.isascii() for ch in t)
            or _CRISIS.search(t)):
        return TIER_SONNET
    words = re.findall(r"[a-z']+", t.replace("thank you", "thanks"))
    if not words:
        return TIER_SONNET
    if all(w in _SMALL_TALK for w in words):
        return TIER_HAIKU
    while words and words[0] in _LEAD:
        words.pop(0)
    while words and words[-1] in _TRAIL:
        words.pop()
    if words and _TEMPLATES.fullmatch(" ".join(words)):
        return TIER_HAIKU
    return TIER_SONNET


def turn_start(messages: list[dict]) -> int | None:
    """Index of the current turn's user message, or None. A note the loop
    adds itself (agent.run_turn's tool-budget nudge) is not one."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if (m.get("role") == "user" and isinstance(m.get("content"), str)
                and not m.get("loop_note")):
            return i
    return None


def tier_for(messages: list[dict]) -> int:
    """The tier for the next model call of the current turn.

    Once the turn has called a clinical tool it stays on Sonnet; otherwise
    the user's message decides. A turn with no user message is not small
    talk."""
    start = turn_start(messages)
    if start is None:
        return TIER_SONNET
    for m in messages[start + 1:]:
        for call in m.get("tool_calls") or []:
            if call.get("name") in CLINICAL_TOOLS:
                return TIER_SONNET
    return classify(messages[start]["content"])


def advisor_used(messages: list[dict]) -> int:
    """Advisor consultations already made in the current turn.

    The loop's history is provider-neutral and carries no advisor blocks,
    so the callers record each assistant round's count under
    `advisor_calls` (careagents/agent.py, careagents/worker.py)."""
    start = turn_start(messages)
    if start is None:
        return 0
    return sum(int(m.get("advisor_calls") or 0)
               for m in messages[start + 1:] if m.get("role") == "assistant")


# --- daily counts ------------------------------------------------------------

#: The only metric names that may be stored.
METRICS = frozenset({
    "turns_haiku", "turns_sonnet", "escalations", "advisor_consults",
    "advisor_errors", "advisor_timeouts", "refusals", "cut_offs"})


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


#: The process's own session scope, handed over by whoever owns the account
#: store (careagents/worker.py). The counts use the app's engine and pool;
#: this module never opens one of its own (#931 security review).
_session_scope = None


def use_session_scope(scope) -> None:
    global _session_scope
    _session_scope = scope


def count(deltas: dict) -> None:
    """Best effort: a count is never a reason a turn fails. Nothing is
    counted in a process that has not handed over its session scope."""
    scope = _session_scope
    if scope is None or not any(deltas.values()):
        return
    try:
        record(scope, deltas)
    except Exception:  # noqa: BLE001 - counting must not fail a turn
        logger.warning("model usage counting failed", exc_info=True)
