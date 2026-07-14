"""CareAgents runtime configuration — fail-closed in production.

Mirrors HealthClaw's posture: a production deployment refuses to boot
half-configured rather than running with weakened guarantees.
"""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    pass


def _require(name: str, value: str | None, why: str) -> str:
    if not value:
        raise ConfigError(f"{name} is required in production — {why}")
    return value


class Config:
    """Resolved once at create_app(); everything the app needs from env."""

    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.app_env = (e.get("CARE_ENV") or e.get("APP_ENV") or "development").lower()
        prod = self.app_env == "production"

        self.healthclaw_base = (e.get("HEALTHCLAW_BASE")
                                or "https://app.healthclaw.io").rstrip("/")
        self.session_secret = e.get("CARE_SESSION_SECRET", "")

        # Accounts layer: own DB, WebAuthn relying-party, transactional email.
        self.database_url = e.get("CARE_DATABASE_URL",
                                  "sqlite:///careagents.db")
        self.rp_id = e.get("CARE_RP_ID", "careagents.cloud")
        self.rp_name = e.get("CARE_RP_NAME", "CareAgents")
        # Absolute site origin for WebAuthn + magic links.
        self.origin = (e.get("CARE_ORIGIN")
                       or f"https://{self.rp_id}").rstrip("/")
        self.resend_api_key = e.get("RESEND_API_KEY", "")
        self.resend_from = e.get("CARE_EMAIL_FROM",
                                 "CareAgents <hello@careagents.cloud>")
        # Fasten (verified-provider real records) — brokered via HealthClaw,
        # but careagents builds the connect widget URL for the browser.
        self.fasten_public_key = e.get("FASTEN_PUBLIC_KEY", "")
        self.fasten_connect_base = e.get(
            "FASTEN_CONNECT_URL", "https://connect.fastenhealth.com")
        # Telegram deep-link target for surface binding.
        self.telegram_bot = e.get("CARE_TELEGRAM_BOT", "")
        # Secret for minting step-up tokens for careagents' non-public tenants
        # on the HealthClaw layer (X-Internal-Secret). Server-side only.
        self.mint_secret = e.get("HEALTHCLAW_MINT_SECRET", "")

        # LLM provider: Anthropic preferred; OpenAI-compatible fallback so the
        # product works before an Anthropic key is provisioned.
        self.anthropic_api_key = e.get("ANTHROPIC_API_KEY", "")
        self.openai_api_key = e.get("OPENAI_API_KEY", "")
        self.openai_base = (e.get("OPENAI_BASE_URL")
                            or "https://api.openai.com/v1").rstrip("/")
        self.anthropic_model = e.get("CARE_MODEL", "claude-sonnet-5")
        self.openai_model = e.get("CARE_OPENAI_MODEL", "gpt-4o-mini")

        # Chat rate limit: turns per window per session (LLM spend bound on a
        # public, unauthenticated site).
        self.chat_turns_per_window = int(e.get("CARE_CHAT_TURNS", "20"))
        self.chat_window_seconds = int(e.get("CARE_CHAT_WINDOW", "600"))

        if prod:
            _require("CARE_SESSION_SECRET", self.session_secret,
                     "sessions must not be forgeable")
            if len(self.session_secret) < 32:
                raise ConfigError(
                    "CARE_SESSION_SECRET must be at least 32 characters")
            _require("HEALTHCLAW_MINT_SECRET", self.mint_secret,
                     "careagents mints tenant-bound tokens server-side")
            if not (self.anthropic_api_key or self.openai_api_key):
                raise ConfigError(
                    "an LLM key is required (ANTHROPIC_API_KEY preferred, "
                    "OPENAI_API_KEY fallback)")
            _require("RESEND_API_KEY", self.resend_api_key,
                     "email verification codes require a transactional sender")
        else:
            self.session_secret = self.session_secret or "dev-careagents-secret"

    @property
    def provider(self) -> str:
        return "anthropic" if self.anthropic_api_key else "openai"
