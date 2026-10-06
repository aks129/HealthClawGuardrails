"""CareAgents runtime configuration — fail-closed in production.

Mirrors HealthClaw's posture: a production deployment refuses to boot
half-configured rather than running with weakened guarantees.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from careagents import _build

logger = logging.getLogger(__name__)

# Model hosts that may serve chat while real records are open (see
# CARE_REAL_RECORDS below). Operators extend it by name, never by pattern.
# The owner approved these four providers on 2026-09-26: Anthropic, OpenAI,
# Google Gemini (its OpenAI-compatible endpoint) and Groq.
_VETTED_MODEL_HOSTS = frozenset({
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "api.groq.com",
})


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
        # `healthclaw_base` is for server-to-server calls and may be an
        # internal host (in production it is the Railway-private hostname).
        # `healthclaw_public_base` is for anything rendered into a page a
        # person will click — Terms, Privacy — and must stay public (#534).
        self.healthclaw_public_base = (e.get("HEALTHCLAW_PUBLIC_BASE")
                                       or "https://app.healthclaw.io").rstrip("/")
        self.session_secret = e.get("CARE_SESSION_SECRET", "")

        # Build provenance (#258) — telemetry, never a gate. Deliberately not
        # _require()d even in production: a missing marker must degrade to
        # "unknown", not stop a boot. Nothing branches on these.
        self.build_sha = _build.BUILD_SHA
        self.build_time = _build.BUILD_TIME

        # Accounts layer: own DB, WebAuthn relying-party, transactional email.
        self.database_url = e.get("CARE_DATABASE_URL",
                                  "sqlite:///careagents.db")
        self.rp_id = e.get("CARE_RP_ID", "careagents.cloud")
        self.rp_name = e.get("CARE_RP_NAME", "CareAgents")
        # Absolute site origin for WebAuthn + magic links.
        self.origin = (e.get("CARE_ORIGIN")
                       or f"https://{self.rp_id}").rstrip("/")
        self.resend_api_key = e.get("RESEND_API_KEY", "")
        # Real mail leaves only production, unless this is "1"
        # (careagents/mail.py, real_mail_allowed).
        self.allow_real_mail = e.get("CARE_ALLOW_REAL_MAIL", "") == "1"
        self.resend_from = e.get("CARE_EMAIL_FROM",
                                 "CareAgents <hello@careagents.cloud>")
        # Fasten (verified-provider real records) — the connect flow runs on
        # HealthClaw's own /connect/<tenant> page (Stitch widget + verified
        # key). careagents only needs the public key present to offer the
        # button; it never builds a Fasten-hosted URL itself.
        self.fasten_public_key = e.get("FASTEN_PUBLIC_KEY", "")
        # Telegram deep-link target for surface binding.
        self.telegram_bot = e.get("CARE_TELEGRAM_BOT", "")
        # Sendblue, a hosted iMessage line (careagents/sendblue.py). On only
        # when every required setting is present; the webhook refuses
        # everything otherwise. Real records must not go over this line
        # until Sendblue's HIPAA instance and BAA are in place
        # (docs/runbooks/sendblue-imessage.md).
        self.sendblue_api_key_id = e.get("SENDBLUE_API_KEY_ID", "")
        self.sendblue_api_secret = e.get("SENDBLUE_API_SECRET", "")
        self.sendblue_webhook_secret = e.get("SENDBLUE_WEBHOOK_SECRET", "")
        # The line is sent as `from_number` and shown in settings, so it is
        # held as E.164, normalized the way inbound handles are. A value that
        # is not a phone number switches Sendblue off rather than sending
        # from a malformed line. Imported here: imessage pulls in the agent.
        from careagents.imessage import mask, normalize_handle
        raw_from = (e.get("SENDBLUE_FROM_NUMBER") or "").strip()
        from_number = normalize_handle(raw_from) if raw_from else None
        if from_number and "@" in from_number:
            from_number = None
        if raw_from and not from_number:
            logger.warning("SENDBLUE_FROM_NUMBER %s is not a phone number; "
                           "Sendblue is off", mask(raw_from))
        self.sendblue_from_number = from_number or ""
        self.sendblue_api_base = (e.get("SENDBLUE_API_BASE")
                                  or "https://api.sendblue.co").rstrip("/")
        # Real-record answers over Sendblue: off unless switched on, and it
        # must stay off until Sendblue's HIPAA instance and BAA exist. Off,
        # an agent on a non-sample connection gets no turn and no answer by
        # text, only a pointer to the app (careagents/sendblue_surface.py).
        self.sendblue_real_records = (e.get("SENDBLUE_REAL_RECORDS") or ""
                                      ).strip().lower() in (
                                          "1", "true", "yes", "on")
        self.sendblue_enabled = all((
            self.sendblue_api_key_id, self.sendblue_api_secret,
            self.sendblue_webhook_secret, self.sendblue_from_number))
        # iMessage handle (phone/email) shown to users as "text your agent
        # here": the Mac-mini relay's, or the Sendblue line when that is on.
        # Empty = surface hidden.
        self.imessage_handle = e.get("CARE_IMESSAGE_HANDLE") or (
            self.sendblue_from_number if self.sendblue_enabled else "")
        # Days a bound iMessage handle is answered before its owner
        # re-confirms it with a texted link (#871): numbers get reassigned.
        # At least 1, or a confirm would be stale the moment it is made.
        try:
            reverify_days = int(e.get("CARE_IMESSAGE_REVERIFY_DAYS", "60"))
        except ValueError:
            logger.warning("CARE_IMESSAGE_REVERIFY_DAYS is not a whole "
                           "number of days; using 60")
            reverify_days = 60
        self.imessage_reverify_days = max(1, reverify_days)
        # Where a new /beta request is announced (careagents/beta_signup.py).
        # Unset = no announcement; the request is still saved.
        self.beta_notify_email = e.get("CARE_BETA_NOTIFY_EMAIL", "")
        # Wearables (Open Wearables sidecar): only advertise a LIVE connect flow
        # where the sidecar + its OAuth developer auth are actually wired.
        # Otherwise Apple Health / wearables show as a "coming soon" tile.
        self.wearables_enabled = e.get(
            "CARE_WEARABLES_ENABLED", "").lower() in ("1", "true", "yes")
        # Page-view counting for the pages anyone can open (careagents/
        # analytics.py). Off unless asked for: it writes a row per day per
        # page and nothing about a visitor, but a counter nobody switched on
        # should not start counting because a deploy happened.
        self.analytics_enabled = e.get(
            "CARE_ANALYTICS", "").lower() in ("1", "true", "yes")
        # Real-record sources (Fasten, wearables, direct FHIR) for the beta
        # (council ruling 2026-09-02, D3). Gates NEW connections only: an
        # existing connection keeps refreshing, polling and deleting whatever
        # this says. `off` renders those tiles "coming soon" and refuses the
        # connect POST; `allowlist` opens them to the account emails in
        # CARE_REAL_RECORDS_ALLOWLIST (comma-separated, case-insensitive)
        # and to live invites in ca_real_record_invites;
        # `on` opens them to everyone. Unset is `off`: a deployment that
        # forgets the variable must not open real records to strangers.
        self.real_records = (
            e.get("CARE_REAL_RECORDS") or "off").strip().lower()
        if self.real_records not in ("off", "allowlist", "on"):
            raise ConfigError(
                "CARE_REAL_RECORDS must be one of off, allowlist, on "
                f"(got {self.real_records!r})")
        self.real_records_allowlist = frozenset(
            x.strip().lower()
            for x in (e.get("CARE_REAL_RECORDS_ALLOWLIST") or "").split(",")
            if x.strip())
        # Sole public hostname (#264, D7). When set, a request whose Host
        # header differs is answered 308 to the same path and query on this
        # host, so the platform's own *.up.railway.app name is not a second
        # front door with its own passkey origin. `/healthz` is exempt (the
        # platform's health check arrives on the internal hostname). Unset
        # means no redirect, which is what local and CI want.
        self.canonical_host = (
            e.get("CAREAGENTS_CANONICAL_HOST") or "").strip().lower()
        # A bare hostname and nothing else. `https://careagents.cloud` is what
        # an operator types by reflex, and it is not a harmless no-op: the
        # comparison below never matches, so EVERY request — including one
        # already on the real site — is answered 308 to a malformed
        # `https://https//careagents.cloud/...`, and a trailing slash loops
        # forever, one slash longer each hop. `/healthz` is exempt, so the
        # platform keeps reporting the deploy healthy while the site is dead.
        # Refuse to boot, exactly as CARE_REAL_RECORDS does above.
        if self.canonical_host and (
                "/" in self.canonical_host
                or ":" in self.canonical_host
                or any(c.isspace() for c in self.canonical_host)):
            raise ConfigError(
                "CAREAGENTS_CANONICAL_HOST must be a bare hostname — no "
                "scheme, port, path or whitespace (got "
                f"{self.canonical_host!r})")
        # Secret for minting step-up tokens for careagents' non-public tenants
        # on the HealthClaw layer (X-Internal-Secret). Server-side only.
        self.mint_secret = e.get("HEALTHCLAW_MINT_SECRET", "")

        # LLM provider: Anthropic preferred; OpenAI-compatible fallback so the
        # product works before an Anthropic key is provisioned.
        self.anthropic_api_key = e.get("ANTHROPIC_API_KEY", "")
        # Claude subscription / OpenClaw OAuth access token (Authorization:
        # Bearer + oauth beta header) — an alternative to an API key. Short-
        # lived, so refresh it out of band (e.g. from the Mac-mini OpenClaw
        # credential) when it expires.
        self.anthropic_oauth_token = e.get("ANTHROPIC_OAUTH_TOKEN", "")
        self.anthropic_oauth_beta = e.get(
            "ANTHROPIC_OAUTH_BETA", "oauth-2025-04-20")
        self.openai_api_key = e.get("OPENAI_API_KEY", "")
        self.openai_base = (e.get("OPENAI_BASE_URL")
                            or "https://api.openai.com/v1").rstrip("/")
        self.anthropic_model = e.get("CARE_MODEL", "claude-sonnet-5")
        self.openai_model = e.get("CARE_OPENAI_MODEL", "gpt-4o-mini")
        # With real records open, chat turns carry redacted-but-real health
        # data to the model host, so that host must be vetted: one of the
        # owner-approved hosts in _VETTED_MODEL_HOSTS, or one named in
        # CARE_REAL_RECORDS_MODEL_HOSTS (naming it is the deliberate act).
        # Exact hostname match. The Anthropic SDK honours ANTHROPIC_BASE_URL
        # from the environment, so that is the Anthropic host when set.
        if self.real_records != "off":
            serving_url = (
                (e.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
                if self.provider == "anthropic" else self.openai_base)
            model_host = urlparse(serving_url).hostname
            vetted = _VETTED_MODEL_HOSTS | {
                x.strip().lower()
                for x in (e.get("CARE_REAL_RECORDS_MODEL_HOSTS") or "")
                .split(",") if x.strip()}
            if model_host not in vetted:
                # Host only: a base URL can carry credentials.
                raise ConfigError(
                    f"CARE_REAL_RECORDS={self.real_records} but chat would "
                    f"go to {self.provider} at unvetted host {model_host!r}; "
                    "use a vetted provider or name the host in "
                    "CARE_REAL_RECORDS_MODEL_HOSTS")

        # Chat rate limit: turns per window per session (LLM spend bound on a
        # public, unauthenticated site).
        self.chat_turns_per_window = int(e.get("CARE_CHAT_TURNS", "20"))
        self.chat_window_seconds = int(e.get("CARE_CHAT_WINDOW", "600"))
        # Retained for other deployment integrations. Conversation execution
        # itself is serialized by HealthClaw's database-backed run claims, so
        # CareAgents no longer depends on Redis for correctness.
        self.redis_url = e.get("REDIS_URL", "")
        # Durable daily ceiling per account. The burst limiter above is
        # in-process, so it resets on restart and multiplies by gunicorn
        # worker count; this one is DB-backed and is what actually bounds
        # what a single account can cost the operator in a day.
        self.chat_turns_per_day = int(e.get("CARE_CHAT_TURNS_PER_DAY", "200"))

        # Durable run execution. Web requests enqueue and replay; this fixed
        # worker pool performs inference outside Gunicorn request threads.
        self.run_deadline_seconds = int(e.get("CARE_RUN_DEADLINE_SECONDS", "120"))
        self.run_lease_seconds = int(e.get("CARE_RUN_LEASE_SECONDS", "60"))
        self.run_worker_concurrency = int(e.get("CARE_RUN_WORKERS", "4"))
        self.run_poll_seconds = float(e.get("CARE_RUN_POLL_SECONDS", "0.5"))
        self.run_poll_max_seconds = float(e.get(
            "CARE_RUN_POLL_MAX_SECONDS", "6.0"))
        self.run_worker_stale_seconds = int(e.get(
            "CARE_RUN_WORKER_STALE_SECONDS", "30"))
        self.run_sse_poll_seconds = float(e.get(
            "CARE_RUN_SSE_POLL_SECONDS", "0.25"))
        # #575: the event stream doubles its wait each time a page comes
        # back empty, up to this, and snaps back to the base the moment an
        # event arrives. The base keeps a token prompt; the cap keeps an
        # idle run from polling HealthClaw four times a second for minutes.
        self.run_sse_poll_max_seconds = float(e.get(
            "CARE_RUN_SSE_POLL_MAX_SECONDS", "2.0"))
        self.run_sse_timeout_seconds = int(e.get(
            "CARE_RUN_SSE_TIMEOUT_SECONDS", "150"))
        if not 5 <= self.run_deadline_seconds <= 3600:
            raise ConfigError("CARE_RUN_DEADLINE_SECONDS must be 5-3600")
        if not 10 <= self.run_lease_seconds <= 600:
            raise ConfigError("CARE_RUN_LEASE_SECONDS must be 10-600")
        if not 1 <= self.run_worker_concurrency <= 32:
            raise ConfigError("CARE_RUN_WORKERS must be 1-32")
        if not 0.05 <= self.run_poll_seconds <= 30:
            raise ConfigError("CARE_RUN_POLL_SECONDS must be 0.05-30")
        if not 0.05 <= self.run_poll_max_seconds <= 30:
            raise ConfigError("CARE_RUN_POLL_MAX_SECONDS must be 0.05-30")
        # The cap can never sit below the floor, so setting it to the floor
        # pins the idle interval flat — the rollback path, by variable change
        # and no redeploy. Without the clamp a smaller cap would invert the
        # doubling instead of disabling it.
        self.run_poll_max_seconds = max(
            self.run_poll_seconds, self.run_poll_max_seconds)
        if not 5 <= self.run_worker_stale_seconds <= 300:
            raise ConfigError(
                "CARE_RUN_WORKER_STALE_SECONDS must be 5-300")
        if not 0.05 <= self.run_sse_poll_seconds <= 10:
            raise ConfigError("CARE_RUN_SSE_POLL_SECONDS must be 0.05-10")
        if not self.run_sse_poll_seconds <= self.run_sse_poll_max_seconds <= 30:
            raise ConfigError(
                "CARE_RUN_SSE_POLL_MAX_SECONDS must be between "
                "CARE_RUN_SSE_POLL_SECONDS and 30")
        if not 10 <= self.run_sse_timeout_seconds <= 3600:
            raise ConfigError("CARE_RUN_SSE_TIMEOUT_SECONDS must be 10-3600")

        if prod:
            _require("CARE_SESSION_SECRET", self.session_secret,
                     "sessions must not be forgeable")
            if len(self.session_secret) < 32:
                raise ConfigError(
                    "CARE_SESSION_SECRET must be at least 32 characters")
            _require("HEALTHCLAW_MINT_SECRET", self.mint_secret,
                     "careagents mints tenant-bound tokens server-side")
            if not (self.anthropic_api_key or self.anthropic_oauth_token
                    or self.openai_api_key):
                raise ConfigError(
                    "an LLM credential is required (ANTHROPIC_API_KEY or "
                    "ANTHROPIC_OAUTH_TOKEN preferred, OPENAI_API_KEY fallback)")
            _require("RESEND_API_KEY", self.resend_api_key,
                     "email verification codes require a transactional sender")
            # SQLite is single-writer and file-local: it does not survive a
            # host rebuild, cannot be backed up consistently while running,
            # and serialises concurrent users. Fine for one tester, wrong for
            # real accounts. Warn rather than refuse, because the live
            # deployment is still on SQLite and a hard failure here would take
            # it down instead of migrating it — flip this to _require once
            # CARE_DATABASE_URL points at Postgres (see docs/development.md).
            if self.database_url.startswith("sqlite"):
                logger.warning(
                    "CareAgents is running production on SQLite (%s). Migrate "
                    "CARE_DATABASE_URL to Postgres before onboarding real "
                    "users: SQLite serialises writes and is lost with the host.",
                    self.database_url)
        else:
            self.session_secret = self.session_secret or "dev-careagents-secret"

    @property
    def provider(self) -> str:
        if self.anthropic_api_key or self.anthropic_oauth_token:
            return "anthropic"
        return "openai"

    def real_records_open_for(self, email, invited=None) -> bool:
        """May this account START a real-record connection? See
        CARE_REAL_RECORDS above. The allowlist is consulted only in
        `allowlist` mode — never as a back door around `off`.

        `invited`, when given, is a callable(email) -> bool for the invite
        table (beta pathway spec section 4.2). Like the environment list, it
        is asked only in `allowlist` mode, and only when the environment list
        did not already admit the email."""
        if self.real_records == "on":
            return True
        if self.real_records == "allowlist":
            email = (email or "").strip().lower()
            if email in self.real_records_allowlist:
                return True
            return bool(email and invited is not None and invited(email))
        return False
