"""propose_action: a fixed text to the person's own phone.

The model picks only a purpose. The number, the recipient label and the
words are CareAgents', built here: the number is the account's own verified
iMessage number, the label never holds it (the engine copies `to` into every
audit detail and push), and the words carry no record content and no token.
Only the review page approves; a texted "yes" is an ordinary chat turn.

The same rule decides both ends: whether the tool is offered and run in a
turn, and whether the review page may approve a text (careagents/app.py).
"""

from __future__ import annotations

#: The only argument the model gives, and its only values.
PURPOSES = ("visit_prep", "reminder")

#: The engine's `to` for every text. Never the number: ProposedAction
#: .summary() copies `to` into audit details and notifications.
RECIPIENT_LABEL = "your phone"

_OPENERS = {
    "visit_prep": "CareAgents: your visit prep is ready.",
    "reminder": "CareAgents: you have a reminder from your assistant.",
}

#: Added to the prompt when texting is open to the account but it has no
#: single verified phone, so the tool is absent (design section 9).
LINK_PHONE_NOTE = (
    "Texting the person's phone is not set up: no phone number is linked "
    "to their account. If they ask to be texted or reminded by text, say "
    "you can't text them yet, and that they can link their phone from "
    "their CareAgents home page by connecting iMessage. Do not offer to "
    "text them until then.")

#: Said to the model and kept in the tool result.
APPROVAL_NOTE = "Only the review page approves; a text reply cannot."


def body_for(purpose: str, origin: str) -> str:
    """The whole text, from a fixed template. Detail stays behind sign-in."""
    base = (origin or "").rstrip("/")
    where = (f"Sign in to read it: {base}/chat." if base
             else "Sign in to CareAgents to read it.")
    return f"{_OPENERS[purpose]} {where} Reply STOP to opt out."


def sms_phone_for(cfg, accounts, account_id: str, email: str) -> str | None:
    """The number a text may go to for this account, or None: the flag is
    closed for it, its records are paused, or it has no single verified
    phone. Asked when a turn starts, again when the tool runs, and again
    before an approval is submitted."""
    if not account_id or not cfg.actions_sms_open_for(email):
        return None
    if accounts.is_paused(account_id):
        return None
    return accounts.verified_phone(
        account_id, cfg.imessage_reverify_days * 86400)
