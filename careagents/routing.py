"""Which Claude model answers a chat turn (CARE_ROUTING=on).

Two tiers, chosen per turn without an extra model call:

  Haiku   the easiest turns only: greetings, thanks, what the assistant can
          do, and how to use the app (connecting records, passkeys, texting,
          deleting the account, feedback). Low effort, no advisor, and it
          sees only the current message, never earlier answers. It answers
          from HAIKU_CARD, and hands anything off the card to Sonnet.
  Sonnet  everything else, at CARE_EFFORT, with an Opus advisor it may
          consult once per turn (CARE_ADVISOR, on by default).

The classifier is a tight allowlist. A message reaches Haiku only when the
WHOLE message, once normalised, is small talk or one of a short list of app
questions written out below. Any other word, any number, any character
outside plain ASCII (emoji included), or anything that reads as a crisis or an emergency sends it to
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

#: On Sonnet calls after the turn's one consultation, when the advisor is no
#: longer offered. Without it the model, asked about its advisor, told the
#: person it had none (live, 2026-10-12).
#: Worded for both cases, consulted and failed, and without saying the
#: advisor is unavailable: told that, the model told the person so (live,
#: round two).
ADVISOR_SPENT_NOTE = (
    "\n\nNever mention an advisor, a reviewer or any tool you do not have, "
    "and never say whether you consulted anyone, even if the person asks "
    "you to. Answer the person directly.")

#: What Haiku says when the card below does not cover the question. A reply
#: holding it is thrown away and the call re-run on Sonnet (llm.py), so the
#: person sees it only if Haiku rewords it.
HAIKU_PASS = ("I'm not sure about that one. Ask me about your records, "
              "or tap Tell us.")

#: Ahead of the system prompt on every Haiku call (#935). Haiku sees only the
#: current message, yet the prompt behind this mentions sample records, and
#: it thanked a brand-new person for finding them useful; asked how to delete
#: the account, it guessed. Every path here is the real one, pinned to its
#: template by tests/test_careagents_haiku_card.py. Keep it short.
HAIKU_CARD = f"""\
For this message, these rules come before everything below.
You have seen none of this person's records and no earlier message. Never \
refer to records, results, sample data or anything said earlier. Do not call \
tools.
Greetings and thanks: one short, neutral sentence, such as "You're welcome. \
Ask me anything about your records."
App questions: answer in a sentence or two, only from this list.
- What you can do: answer questions about the records connected here, \
explain lab results in plain words, show a lab over time, list screenings \
that may be due, help get ready for a doctor visit, and start an intake form \
the person approves item by item.
- Delete the account: on your hub (Home in the chat menu) tap Settings, then \
Delete my account under "Sign out or leave", and type the word delete to \
confirm. It deletes the records behind every connection, then the email, passkey and \
consents. It can't be undone.
- Add a passkey: Settings, then Add a passkey; you confirm it's you with a \
code we email. Removing a passkey isn't available in the app; deleting the \
account removes it.
- Connect your own records: tap Connect your records on your hub. It shows \
once you've tried the made-up records, and may need an invite. If you came \
in from an email link, tap Sign in again to connect your records and enter \
the code we email.
- Invite people: Settings, then Invite friends and family; Copy link or \
share it; up to 5 people can join with it.
- Texting: Settings, under "Where you can reach your assistant", tap iMessage \
and text the code it shows from your phone. Start a chat first. If it says \
coming soon, texting isn't open yet.
- Feedback: tap Tell us at the bottom of the chat or your hub.
- Sign out: Sign out in the chat menu (the three dots), or in Settings.
- Safety: How we keep you safe, in the chat menu.
Anything else, or if unsure, reply with exactly: {HAIKU_PASS}

"""


def haiku_passed(text: str) -> bool:
    """Did Haiku hand the question on (HAIKU_PASS)? Normalised both sides,
    so a curly apostrophe is still a pass."""
    # Exact English: safe only because Spanish never reaches Haiku (classify).
    return normalize(HAIKU_PASS) in normalize(text)


#: Every tool that reads or acts on the person's health record. Haiku calling
#: one of these means the turn was clinical after all.
CLINICAL_TOOLS = frozenset({
    "get_health_summary", "get_labs", "show_lab_timeline", "get_care_gaps",
    "search_records", "appointment_brief", "start_intake_form"})

#: Longer than this is not small talk.
_MAX_HAIKU_CHARS = 120

#: Self-harm and wanting to die: crisis wording that also earns the 988
#: line where a fixed sentence answers instead of the model
#: (beta.emergency_first). Part of _CRISIS below, never a separate list.
#: Spanish (and Spanglish) is written without accents: is_crisis matches the
#: text with its accents taken off, so "convulsión" and "convulsion" both
#: count. Broad stems (morir, urgen) are deliberate: a false positive costs
#: one Sonnet call, a miss puts the next "ok" after a crisis on Haiku.
#: Security review of #1017 checked the Spanish against ordinary Spanish.
#: Pills and drugs by name, for the dose and bottle patterns below: "how
#: much salt would kill me" is not a dose (security round 8).
_DRUG = (r"pills?|tablets?|meds|medications?|medicines?|insulin|tylenol|"
         r"acetaminophen|paracetamol|ibuprofen|advil|aspirin|metformin|"
         r"opioids?|oxy\w{0,8}|xanax|benzos?|fentanyl|morphine|"
         r"antidepressants?|sleeping pills|doses?")

#: Not self-harm: "me corto el pelo", "voy a cortarme las uñas".
_NOT_GROOMING = (r"(?! (?:el |la |las |los |mi |mis )?"
                 r"(?:pelo|cabello|unas|flequillo|barba))")

_SELF_HARM_PATTERNS = [
    r"suicid", r"kill(?:ing)? my ?self", r"end(?:ing)? (?:it|my life|it all)",
    r"(?:want to|wanna|going to|gonna) die", r"don'?t want to (?:live|be here)",
    r"self.?harm", r"hurt(?:ing)? my ?self", r"cut(?:ting)? my ?self",
    # 911 and 988 as numbers on their own, never inside a phone number or
    # a lab value.
    r"overdos", r"\bod\b", r"(?<![\d-])988(?![\d-])",
    r"sobredosis",
    r"muriendo", r"me muero", r"no quiero vivir",
    r"quitarme la vida", r"acabar con (?:mi vida|todo)", r"terminar con todo",
    r"hacerme dano", r"me (?:quiero |voy a )?hacer dano", r"lastimarme",
    r"cortarme" + _NOT_GROOMING, r"me corto" + _NOT_GROOMING,
    r"me corte" + _NOT_GROOMING, r"me estoy cortando" + _NOT_GROOMING,
    r"cortandome" + _NOT_GROOMING,
    r"matar(?:me|se)", r"(?:quiero|kiero|voy a) matar",
    r"(?<!no )(?:quiero|quisiera|prefiero|deseo) morir",
    r"ganas de morir", r"kiero morir", r"(?:want to|wanna) morir",
    r"took (?:a (?:whole )?bottle of|all (?:of )?(?:my|the)) "
    r"(?:pills|meds|medications?|tablets)",
    r"better off (?:dead|without me)", r"no reason to live",
    r"hang(?:ing)? my ?self", r"slit(?:ting)? my (?:wrists?|throat)",
    r"\bkms\b", r"unalive",
    r"no vale la pena vivir", r"quiero desaparecer",
    r"(?:quiero|voy a) colgarme", r"me (?:quiero |voy a )?colgar\b",
    r"cort\w{0,8} las venas", r"me (?:hice|he hecho) dano", r"me lastime",
    r"no aguanto mas\b", r"(?:irme|ir) de este mundo",
    # The live-model check of #1017: answered with 911 and 988 by the
    # model, missed here. "Kill me" only as a wish or a dose: "this traffic
    # is going to kill me" is a figure of speech (security round 6).
    r"(?:want|wish|need) (?:it |them |someone |something )?to kill me",
    r"kill me now", r"(?:pills?|doses?|amount|many) (?:would|will|to) kill me",
    r"(?:" + _DRUG + r")\b.{0,30}(?:would|will) it take to kill me",
    # Security round 7: ideation the narrowed patterns missed.
    r"wish (?:someone|somebody) would kill me",
    r"(?:just|please) (?:just )?kill me",
    r"(?:how much|how many) (?:of (?:my|the|these|those) )?(?:\w+ )?"
    r"(?:" + _DRUG + r")\b(?: \w+)? (?:would|will|to) kill me",
    r"(?:would|will|could) (?:\d+|\w+) (?:pills?|tablets?|doses?) kill me",
    r"(?:wish|want) (?:i was|i were|to be) dead"
    r"(?! (?:tired|certain|serious|sure|set|wrong|right|last))",
    r"hope i (?:don'?t|do not|never) wake up"
    r"(?! (?:late|early|too|on time|in time))",
    r"want (?:it all|everything) to end(?! (?:well|on|soon|early|by|in))",
    r"(?:took|swallowed) a (?:whole )?bottle of (?:my |the |\w+ )?"
    r"(?:" + _DRUG + r")\b",
    r"(?:too many|overdos\w{0,6})\b.{0,40}\bon purpose",
    r"(?:pastillas|sobredosis|demasiad\w{0,4}).{0,40}a proposito",
    r"alguien me mate", r"ojala (?:estuviera|estar) muert",
    r"ojala no despert(?!\w{0,4} (?:tan|temprano))",
    r"lethal dose", r"dosis letal", r"cuantas pastillas .{0,20}(?:matar|morir)",
]

#: Medical emergencies, abuse and being unsafe.
_URGENT_PATTERNS = [
    r"can'?t breathe", r"cannot breathe",
    r"can not breathe", r"chest pain", r"heart attack", r"stroke",
    r"seizure", r"unconscious", r"passed out", r"pass(?:es|ing)? out",
    r"faint", r"bleeding", r"anaphyla", r"throat (?:is )?(?:closing|swelling)",
    r"can'?t swallow", r"cannot swallow", r"can not swallow",
    r"(?<![\d-])911(?![\d-])", r"emergenc", r"\bem?r\b", r"ambulance",
    r"crisis", r"abuse", r"\bunsafe\b",
    r"dolor (?:en el|de|del) pecho", r"ambulancia", r"desmay", r"sangrado",
    r"convulsi", r"derrame", r"infarto",
    r"(?:no puedo|can.?t|cannot|cuesta|dificultad para|trabajo para) respirar",
    r"no puedo breath", r"falta (?:el |de )?aire", r"me ahog",
    r"perd\w{0,8} el conocimiento", r"inconscien",
    r"no (?:responde|despierta|reacciona) (?:y|ni|cuando|aunque|al)\b",
    r"(?:no|ni) (?:se )?despierta",
    r"sangrando", r"sangro", r"mucha sangre", r"perdiendo sangre",
    r"no (?:siento|puedo mover) (?:el|la|mi) (?:brazo|pierna|cara|mano)",
    r"mitad del cuerpo", r"cara caida", r"cayo la cara", r"boca torcida",
    r"no puede hablar", r"habla arrastrada", r"embolia", r"\bictus\b",
    # Bounded: an unbounded \w* here was quadratic in a long word.
    r"(?:dolor|duele|presion|opresion|aprieta|arde)\w{0,6}.{0,20}pecho",
    r"pecho.{0,10}(?:hurts|duele|aprieta)", r"chest duele",
    r"ataque (?:al corazon|cardiac)", r"paro cardiac", r"epilep",
    r"urgen", r"abus", r"me pega", r"violencia", r"maltrat",
    r"no (?:estoy|me siento) segur[oa] (?:en|aqui|con)",
    r"segur[oa] en (?:mi )?casa", r"anafila", r"cierra la garganta",
    # Overdose and dying wording that is ambiguous on its own: 911, not 988.
    r"tome demasiad[oa]s? (?:pastillas|pildoras|tabletas|medicina|"
    r"medicamento|dosis|ibuprofeno|paracetamol|acetaminofen|insulina|"
    r"aspirina|tylenol)",
    r"(?:me )?tome (?:todas|muchas) (?:las |mis )?pastillas",
    r"un frasco de pastillas", r"(?:voy|va|vamos) a morir",
    r"(?:short(?:ness)? of|trouble|difficulty|hard to|struggling to) breath",
    r"not breathing", r"turning blue", r"\bchok(?:e|es|ed|ing)\b",
    r"(?:face|mouth|smile) (?:is )?droop", r"droop\w{0,4} (?:face|mouth)",
    r"slurr", r"can'?t feel my (?:arm|leg|face|hand|side)",
    r"worst headache", r"(?:coughing|vomiting|throwing) (?:up )?blood",
    r"allergic reaction", r"(?:lips?|tongue|face|throat) (?:are |is )?swell",
    r"(?:husband|wife|partner|boyfriend|girlfriend|he|she) (?:hits|beats) me",
    r"not safe at home",
    r"no respira", r"no respiro", r"ahogando", r"falta (?:la |de )?respiracion",
    r"(?:vomito|vomitando|toso|tosiendo) sangre", r"peor dolor de cabeza",
    r"traba la lengua", r"cara dormida", r"me (?:golpea|amenaza)",
    r"not waking up", r"won'?t wake up", r"unresponsive", r"collaps",
    r"i'?m dying", r"i am dying", r"(?:can'?t|can not|cannot) breath\b",
    r"sangra mucho", r"no puedo hablar", r"desplom", r"envenen",
    # Possibly an accident, not self-harm: 911 only (security round 6).
    r"(?:took|taken|swallowed) (?:way )?too many",
    r"too many (?:pills|tablets)\b",
]

#: Crisis and emergency language. Never Haiku, whatever else the message
#: says. The allowlist already refuses these; this is the rule stated out
#: loud, so a later template cannot quietly admit one.
_CRISIS = re.compile(r"|".join(_SELF_HARM_PATTERNS + _URGENT_PATTERNS))
_SELF_HARM = re.compile(r"|".join(_SELF_HARM_PATTERNS))

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
    rf"{_Q} (?:add|set up|setup|create|use|make|get|remove|delete) "
    r"(?:a |my )?passkeys?",
    rf"{_Q} invite (?:a |my |some )?(?:friends?|family|people|someone|others)"
    r"(?: (?:and|or) family)?",
    # Data only: "keep me safe" can be a person asking about their own
    # safety, which gets Sonnet's check-in, never a menu pointer (#936 sec).
    r"how (?:do|does) (?:you|this|it|careagents) keep "
    r"my (?:data|records|information|privacy) (?:safe|private|secure)",
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


#: Apostrophes read as ': the curly one and the modifier letter (U+02BC),
#: so "canʼt breathe" is "can't breathe".
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u02bc": "'"})


def normalize(text: str) -> str:
    """NFKC, invisible format characters removed, casefolded, whitespace
    runs collapsed to one space.

    NFKC folds fullwidth and other compatibility letters to ASCII, and the
    format characters (zero-width space, joiners, bidi marks) are what split
    a word so a scan misses it. A tab or a line break between two words
    splits a phrase the same way. Anything still not ASCII afterwards (a
    Cyrillic er that looks like p, an emoji) is left for classify to
    refuse."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    t = re.sub(r"\s+", " ", t.translate(_APOSTROPHES))
    return t.casefold().strip()


#: Marks that sit invisibly inside a word: the combining grapheme joiner,
#: variation selectors and zero-width characters. Most are category Mn or
#: Cf already; listed so none can split a crisis word whatever its category.
_INVISIBLE = frozenset(
    ["\u034f", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"]
    + [chr(c) for c in range(0xFE00, 0xFE10)]
    + [chr(c) for c in range(0xE0100, 0xE01F0)])


def _unaccented(text: str) -> str:
    """`text` with accents and other marks taken off: "desmayé" reads
    "desmaye", and a combining mark or enclosing circle inside "suicide"
    no longer splits it. For crisis matching only; classify still refuses
    non-ASCII text."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if unicodedata.category(ch) not in ("Mn", "Me")
                   and not unicodedata.combining(ch)
                   and ch not in _INVISIBLE)


def is_crisis(text: str) -> bool:
    return bool(_CRISIS.search(_unaccented(normalize(text))))


def is_self_harm(text: str) -> bool:
    """Crisis wording about self-harm or wanting to die (988, not only 911)."""
    return bool(_SELF_HARM.search(_unaccented(normalize(text))))


def classify(text: str) -> int:
    """TIER_HAIKU only for whole-message small talk and listed app
    questions; TIER_SONNET for everything else."""
    t = normalize(text)
    if (not t or len(t) > _MAX_HAIKU_CHARS or re.search(r"\d", t)
            # Any character outside ASCII, not only letters: an emoji is a
            # symbol, and "bye" with a row of pills is not small talk
            # (#931 round two R1).
            or not t.isascii()
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
    if crisis_in_window(messages):
        return TIER_SONNET
    return classify(messages[start]["content"])


def crisis_in_window(messages: list[dict]) -> bool:
    """Crisis or emergency wording anywhere in the conversation loaded for
    this turn, from the person or in an answer (a reply that gave 911 or
    988 is itself the signal).

    After a crisis message the person's likeliest next words are "ok. bye"
    or "thanks". On Haiku, which sees only that reply (#931 F2), they got a
    cheerful goodbye; Sonnet, with the history, checked they were safe
    (QA #931 Q1, live 3 of 3). The conversation stays on Sonnet instead.
    Tool results (record content) are not scanned, and neither is the 911
    line every refusal ends with (beta.EMERGENCY_TRAILER): it is there
    whatever the person said, so it is no signal."""
    from careagents.beta import EMERGENCY_TRAILER  # beta imports this module
    return any(
        m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and is_crisis(m["content"].replace(EMERGENCY_TRAILER, ""))
        for m in messages)


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
