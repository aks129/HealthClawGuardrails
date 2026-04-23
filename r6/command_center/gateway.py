"""
OpenClaw Gateway probe.

The external OpenClaw product (github.com/openclaw/openclaw) runs a local
Gateway daemon on the user's machine. When HealthClaw Guardrails is deployed
on the same host (e.g., a user's Mac mini), we can probe the gateway to show
its live status in the command center.

Probe is a best-effort HEAD/GET to a configurable URL. All network errors
are caught and surfaced as a structured "status: down / unknown" result so
the dashboard never breaks on gateway unavailability.

Environment variables:
    OPENCLAW_GATEWAY_URL       Override probe URL (default: http://host.docker.internal:4319/healthz)
    OPENCLAW_GATEWAY_TIMEOUT   Seconds (default: 2)
    OPENCLAW_GATEWAY_TOKEN     Bearer token for OpenClaw RPC endpoints (sessions)
    OPENCLAW_SESSIONS_URL      Override sessions list URL
                               (default: OPENCLAW_GATEWAY_URL base + /rpc/sessions_list)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://host.docker.internal:4319/healthz"
_CACHE_TTL_SECONDS = 10


@dataclass
class GatewayStatus:
    """Snapshot of the OpenClaw gateway liveness probe."""

    configured: bool
    reachable: bool
    url: str
    status_code: int | None
    latency_ms: int | None
    checked_at: float
    version: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "configured": self.configured,
            "reachable": self.reachable,
            "url": self.url,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "version": self.version,
            "error": self.error,
        }


# In-process cache so 5-second dashboard polls don't hammer the gateway.
_cached: GatewayStatus | None = None
_cached_at: float = 0.0


def _gateway_url() -> str:
    return os.environ.get("OPENCLAW_GATEWAY_URL", _DEFAULT_URL).strip()


def _is_configured() -> bool:
    # "Configured" = the user has explicitly set an URL, OR we're on a host
    # where host.docker.internal resolves (we still try the default but mark
    # explicit opt-in via env var).
    return bool(os.environ.get("OPENCLAW_GATEWAY_URL"))


def probe(force: bool = False) -> GatewayStatus:
    """
    Return a fresh or cached gateway status. Cached for _CACHE_TTL_SECONDS.
    """
    global _cached, _cached_at

    now = time.time()
    if not force and _cached and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached

    url = _gateway_url()
    timeout = float(os.environ.get("OPENCLAW_GATEWAY_TIMEOUT", "2"))
    start = time.time()

    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        reachable = resp.status_code < 500
        version = None
        try:
            data = resp.json()
            if isinstance(data, dict):
                version = data.get("version") or data.get("gateway_version")
        except ValueError:
            pass

        _cached = GatewayStatus(
            configured=_is_configured(),
            reachable=reachable,
            url=url,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            checked_at=now,
            version=version,
            error=None if reachable else f"HTTP {resp.status_code}",
        )
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, OSError) as e:
        latency_ms = int((time.time() - start) * 1000)
        _cached = GatewayStatus(
            configured=_is_configured(),
            reachable=False,
            url=url,
            status_code=None,
            latency_ms=latency_ms,
            checked_at=now,
            version=None,
            error=str(e)[:200],
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("Unexpected gateway probe error: %s", e)
        _cached = GatewayStatus(
            configured=_is_configured(),
            reachable=False,
            url=url,
            status_code=None,
            latency_ms=None,
            checked_at=now,
            version=None,
            error=f"{type(e).__name__}: {e}"[:200],
        )

    _cached_at = now
    return _cached


# ---------------------------------------------------------------------------
# OpenClaw sessions — list active chat sessions across channels
# ---------------------------------------------------------------------------

_sessions_cache: list[dict] | None = None
_sessions_cached_at: float = 0.0
_SESSIONS_CACHE_TTL = 15


def _sessions_url() -> str:
    explicit = os.environ.get("OPENCLAW_SESSIONS_URL", "").strip()
    if explicit:
        return explicit
    base = _gateway_url()
    # Derive: healthz -> /rpc/sessions_list under the same origin
    if "/healthz" in base:
        return base.replace("/healthz", "/rpc/sessions_list")
    return base.rstrip("/") + "/rpc/sessions_list"


def list_sessions(force: bool = False) -> list[dict]:
    """
    Query the OpenClaw Gateway for active sessions across channels
    (Telegram, WhatsApp, iMessage, etc.). Returns a list of sessions
    normalized to {id, channel, peer, agent, last_activity, started}.

    Cached for _SESSIONS_CACHE_TTL seconds. Returns [] on any error.
    """
    global _sessions_cache, _sessions_cached_at
    now = time.time()
    if not force and _sessions_cache is not None and (now - _sessions_cached_at) < _SESSIONS_CACHE_TTL:
        return _sessions_cache

    if not _is_configured():
        _sessions_cache = []
        _sessions_cached_at = now
        return _sessions_cache

    url = _sessions_url()
    timeout = float(os.environ.get("OPENCLAW_GATEWAY_TIMEOUT", "3"))
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # OpenClaw RPC uses JSON-RPC 2.0; empty params lists all sessions
        resp = httpx.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "sessions_list", "params": {}},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("result", []) if isinstance(data, dict) else []

        normalized = []
        for s in raw if isinstance(raw, list) else []:
            normalized.append({
                "id": s.get("id") or s.get("session_id"),
                "channel": s.get("channel"),
                "peer": s.get("peer") or s.get("account"),
                "agent": s.get("agent") or s.get("agent_name"),
                "last_activity": s.get("last_activity") or s.get("updated_at"),
                "started": s.get("started") or s.get("created_at"),
                "message_count": s.get("message_count"),
            })
        _sessions_cache = normalized
    except Exception as e:  # noqa: BLE001
        logger.debug("OpenClaw sessions_list failed: %s", e)
        _sessions_cache = []

    _sessions_cached_at = now
    return _sessions_cache
