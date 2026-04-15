"""
Health context loader.

Reads `.health-context.yaml` at the repo root — a single declaration of
jurisdiction, audience, regulations, and defaults for this deployment.
Used by the guardrail stack to pick redaction profile, default audit
agent, and default tenant without re-asking every caller.

The file is treated as immutable at runtime. Loaded once, cached.
"""

import logging
import os
from functools import lru_cache
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT: dict[str, Any] = {
    "name": "HealthClaw Guardrails",
    "version": "1.3.0",
    "role": "engine",
    "jurisdiction": "us",
    "regulations": ["hipaa"],
    "audience": ["developer"],
    "data_sensitivity": "phi",
    "tenant_default": "desktop-demo",
    "audit_agent_default": "healthclaw-guardrails",
    "surfaces": [],
}


def _locate_context_file() -> str | None:
    """Find `.health-context.yaml` walking up from this file."""
    start = os.path.dirname(os.path.abspath(__file__))
    here = start
    for _ in range(4):  # r6/ -> repo root is 1 step; give ourselves slack
        candidate = os.path.join(here, ".health-context.yaml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


@lru_cache(maxsize=1)
def load_health_context() -> dict[str, Any]:
    """
    Load and cache the repo-level health context.

    Returns a dict merged with sensible defaults. Missing file is not
    an error — defaults are used and a one-line info log is emitted.
    """
    path = _locate_context_file()
    if not path:
        logger.info(
            "No .health-context.yaml found; using built-in defaults "
            "(jurisdiction=us, sensitivity=phi, tenant=desktop-demo)"
        )
        return dict(_DEFAULT_CONTEXT)

    try:
        with open(path, encoding="utf-8") as f:
            parsed = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse %s: %s — falling back to defaults", path, exc)
        return dict(_DEFAULT_CONTEXT)

    merged = dict(_DEFAULT_CONTEXT)
    merged.update({k: v for k, v in parsed.items() if v is not None})
    return merged


def get(key: str, default: Any = None) -> Any:
    """Shortcut: fetch a single key from the loaded context."""
    return load_health_context().get(key, default)
