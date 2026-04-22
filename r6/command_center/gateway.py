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
