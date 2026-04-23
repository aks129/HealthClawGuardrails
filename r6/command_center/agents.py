"""
Agent registry loader.

Loads agents.yaml at process start and provides lookup helpers. The registry
is read-only in code — edit agents.yaml to add or change agents.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_AGENTS_PATH = Path(__file__).parent / "agents.yaml"
_TEMPLATES_PATH = Path(__file__).parent / "agent_templates.yaml"


@lru_cache(maxsize=1)
def load_agents() -> list[dict]:
    """Return the parsed agent list. Cached for the process lifetime."""
    if not _AGENTS_PATH.exists():
        logger.warning("agents.yaml not found at %s", _AGENTS_PATH)
        return []

    try:
        data = yaml.safe_load(_AGENTS_PATH.read_text())
    except yaml.YAMLError as e:
        logger.error("Failed to parse agents.yaml: %s", e)
        return []

    return data.get("agents", []) if data else []


@lru_cache(maxsize=1)
def load_agent_templates() -> dict:
    """Return the parsed agent templates catalog (templates + bundles)."""
    if not _TEMPLATES_PATH.exists():
        logger.warning("agent_templates.yaml not found at %s", _TEMPLATES_PATH)
        return {"templates": [], "bundles": []}

    try:
        data = yaml.safe_load(_TEMPLATES_PATH.read_text()) or {}
    except yaml.YAMLError as e:
        logger.error("Failed to parse agent_templates.yaml: %s", e)
        return {"templates": [], "bundles": []}

    return {
        "templates": data.get("templates") or [],
        "bundles": data.get("bundles") or [],
    }


def get_agent(agent_id: str) -> dict | None:
    """Look up a single agent by id."""
    for agent in load_agents():
        if agent.get("id") == agent_id:
            return agent
    return None


def agent_for_tool(tool_name: str) -> dict | None:
    """
    Return the first agent whose tool_patterns include `tool_name`.
    Used to attribute AuditEvents to an agent when no explicit agent_id
    was recorded. Order in agents.yaml matters — more specific agents
    should come before generic ones.
    """
    for agent in load_agents():
        if tool_name in (agent.get("tool_patterns") or []):
            return agent
    return None


def agent_for_event(event) -> dict | None:
    """
    Given an AuditEventRecord, infer the most likely agent.

    Priority:
    1. Explicit agent_id on the event matches an agent id in the registry
    2. Explicit agent_id matches a skill used by an agent
    3. Fall back to tool-name inference (detail field often names the tool)
    """
    agent_id = getattr(event, "agent_id", None) or ""
    if agent_id:
        direct = get_agent(agent_id)
        if direct:
            return direct
        # Check if agent_id names a skill
        for agent in load_agents():
            if agent_id in (agent.get("skills") or []):
                return agent

    detail = getattr(event, "detail", None) or ""
    for agent in load_agents():
        for tool in agent.get("tool_patterns") or []:
            if tool in detail:
                return agent

    return None
