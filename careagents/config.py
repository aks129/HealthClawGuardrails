"""CareAgents runtime configuration — fail-closed in production.

Mirrors HealthClaw's posture: a production deployment refuses to boot
half-configured rather than running with weakened guarantees.
"""

from __future__ import annotations

import logging
import os

from careagents import _build

logger = logging.getLogger(__name__)


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
        self.resend_from = e.get("CARE_EMAIL_FROM",
                                 "CareAgents <hello@careagents.cloud>")
        # Fasten (verified-provider real records) — the connect flow runs on
        # HealthClaw's own /connect/<tenant> page (Stitch widget + verified
        # key). careagents only needs the public key present to offer the
        # button; it never builds a Fasten-hosted URL itself.
        self.fasten_public_key = e.get("FASTEN_PUBLIC_KEY", "")
        # Telegram deep-link target for surface binding.
        self.telegram_bot = e.get("CARE_TELEGRAM_BOT", "")
        # iMessage handle (phone/email) the Mac-mini relay sends/receives on —
        # shown to users as "text your agent here". Empty = surface hidden.
        self.imessage_handle = e.get("CARE_IMESSAGE_HANDLE", "")
        # Wearables (Open Wearables sidecar): only advertise a LIVE connect flow
        # where the sidecar + its OAuth developer auth are actually wired.
        # Otherwise Apple Health / wearables show as a "coming soon" tile.
        self.wearables_enabled = e.get(
            "CARE_WEARABLES_ENABLED", "").lower() in ("1", "true", "yes")
        # Real-record sources (Fasten, wearables, direct FHIR) for the beta
        # (council ruling 2026-09-02, D3). Gates NEW connections only: an
        # existing connection keeps refreshing, polling and deleting whatever
        # this says. `off` renders those tiles "coming soon" and refuses the
        # connect POST; `allowlist` opens them to the account emails in
        # CARE_REAL_RECORDS_ALLOWLIST (comma-separated, case-insensitive);
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

    def real_records_open_for(self, email) -> bool:
        """May this account START a real-record connection? See
        CARE_REAL_RECORDS above. The allowlist is consulted only in
        `allowlist` mode — never as a back door around `off`."""
        if self.real_records == "on":
            return True
        if self.real_records == "allowlist":
            return (email or "").strip().lower() in self.real_records_allowlist
        return False
