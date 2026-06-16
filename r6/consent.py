"""
Chat-platform consent (Phase 3).

When a patient pulls their health data into a chat platform (Telegram, Slack,
Discord), PHI leaves the guardrailed system and enters a third party that is
NOT a HIPAA business associate. The legal posture that makes this lawful is
patient-directed access: the individual is exercising their HIPAA right of
access (45 CFR 164.524) and directing their own data to a destination of
their choosing. HealthClaw acts as the patient's agent, not as a covered
entity disclosing data.

That posture only holds if the patient is actually informed and consenting.
This module owns the notice text, its version, and the gate the bot enforces
before any identified data is sent to a chat. It pairs with the consent
fields + helpers on `TelegramBinding` (record_consent / has_consented /
set_phi_mode / consent_status).

Bumping CONSENT_VERSION re-prompts every chat on its next PHI command.
"""

# Bump when the notice text materially changes (re-prompts all chats).
CONSENT_VERSION = "2026-06-16"


def consent_notice(platform: str = "this chat") -> str:
    """The risk-acknowledgment notice shown before PHI flows to a chat.

    `platform` names the destination (e.g. 'Telegram') so the third-party
    warning is concrete. Returns Markdown-safe plain text.
    """
    return (
        "*Before we share your health records here — please read.*\n\n"
        "You are using your individual right of access (HIPAA, 45 CFR 164.524) "
        "to direct a copy of your own health data to "
        f"{platform}. HealthClaw acts as *your agent* at your request — it is "
        "not your doctor and not a covered entity disclosing your records.\n\n"
        "What that means for you:\n"
        f"• {platform} is a third party and is *not* a HIPAA business "
        "associate. Messages — including any health details — may be stored "
        "on its servers and on your device.\n"
        "• Anyone with access to this chat or device can read what's sent "
        "here. Don't use a shared or work account.\n"
        "• This is *not medical advice*. Talk to a licensed clinician about "
        "any decision.\n"
        "• You stay in control: choose *summary-only* mode (counts, no "
        "identified values) with /privacy, and disconnect anytime with "
        "/unbind. Write actions still require your explicit step-up approval.\n\n"
        "Reply /consent to acknowledge and continue, or /privacy to switch to "
        "summary-only first."
    )


# One-line reminder shown when a PHI command is blocked for lack of consent.
CONSENT_REQUIRED_HINT = (
    "🔒 Please review and acknowledge the data-sharing notice first: "
    "send /consent (or /start to re-read it)."
)
