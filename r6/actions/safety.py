"""Mandatory, non-bypassable emergency screen for PROPOSE on every free-text
reason/body: a hit refuses the action and returns 911/urgent-care escalation,
audited like a Schedule-II refusal. Wired into the propose routes in Task 10
(Approve-is-the-commit flow); until then this module is the shared lexicon +
contract. Reuses the SMBP triage red-flag doctrine (symptoms trump
everything) — see r6/smbp/triage.py.

triage.SYMPTOMS is a structured question-set vocabulary (snake_case codes
like "chest_pain", answered yes/no in the BP flow), not free-text phrases —
so it can't be substring-matched against SMS/reason text as-is. Rather than
fork a second hardcoded symptom list, we derive the free-text lexicon from
it: underscore -> space turns "chest_pain" into "chest pain", "trouble_speaking"
into "trouble speaking", etc. That keeps ONE shared source of red-flag
symptoms — extend triage.SYMPTOMS and this screen picks it up automatically.

NOTE (spec v3): lexicon matching is the floor every kind shares; it is
sufficient for SMS bodies. The booking-reason field must ADDITIONALLY use a
structured question set or a classifier held to a zero-false-negative eval
gate — that lives in the booking rail (post-webinar), not here.
"""
from r6.smbp.triage import SYMPTOMS

# Expansion beyond cardiac/stroke: mental-health crisis, anaphylaxis, OB.
# triage.SYMPTOMS covers the BP-specific 7-item screen (chest pain, one-sided
# weakness, etc); these cover emergencies outside that domain that a booking
# or SMS body can still surface.
_EXTRA = [
    'kill myself', 'suicid', 'end my life', 'want to die',
    'anaphylax', 'throat closing', "can't breathe", 'cannot breathe',
    'trouble breathing', 'not breathing', "isn't breathing",
    'face drooping', 'slurred speech',
    'vaginal bleeding', 'pregnant and bleeding', 'overdose',
    # Lay terms for the events themselves, not just symptoms — a caregiver
    # types "he's having a heart attack", not "chest pain". Bare 'stroke'
    # substring-matches 'keystroke'/'stroke of luck' and '911' matches
    # addresses like '911 Main St'; accepted false positives — this screen
    # must err toward refusing an action, never toward missing an emergency.
    'heart attack', 'stroke', '911',
]

_LEXICON = [s.replace('_', ' ').lower() for s in SYMPTOMS] + [s.lower() for s in _EXTRA]

EMERGENCY_MESSAGE = (
    'This looks like it may be an emergency. HealthClaw cannot act on '
    'emergencies. If this is a medical emergency, call 911 or go to the '
    'nearest emergency department now.')


def screen_text(text):
    """Return {'emergency': True, 'matched': <phrase>} on a red-flag hit, else
    None. Case-insensitive substring match on the shared lexicon."""
    if not text:
        return None
    low = text.lower()
    for phrase in _LEXICON:
        if phrase and phrase in low:
            return {'emergency': True, 'matched': phrase}
    return None
