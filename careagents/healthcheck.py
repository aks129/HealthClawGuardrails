"""OCI health probe for the CareAgents web and durable-worker roles."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request


def healthy(env=None) -> bool:
    env = env or os.environ
    role = env.get("CARE_ROLE", "web")
    timeout = 4
    if role == "worker":
        base = (env.get("HEALTHCLAW_BASE") or "").rstrip("/")
        secret = env.get("HEALTHCLAW_MINT_SECRET") or ""
        stale = env.get("CARE_RUN_WORKER_STALE_SECONDS", "30")
        if not base or not secret:
            return False
        query = urllib.parse.urlencode({"max_age_seconds": stale})
        request = urllib.request.Request(
            f"{base}/command-center/api/runs/workers/health?{query}",
            headers={"X-Internal-Secret": secret},
        )
    else:
        port = env.get("PORT", "8600")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/healthz")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - a health probe must return unhealthy
        return False


def main() -> None:
    raise SystemExit(0 if healthy() else 1)


if __name__ == "__main__":
    main()
