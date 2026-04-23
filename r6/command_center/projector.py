"""
Command center projector.

Pure functions that read existing tables + runtime signals and produce
the JSON shapes the dashboard consumes. No new DB tables are invented —
everything is derived from AuditEventRecord, R6Resource, FastenConnection,
FastenJob, WearableConnection, ConversationMessage, AgentTask, plus live
probes (OpenClaw gateway, MCP server, FHIR_UPSTREAM_URL).
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import desc, func

from models import db
from r6.models import R6Resource, AuditEventRecord, ContextEnvelope
from r6.fasten.models import FastenConnection, FastenJob
from r6.wearables.models import WearableConnection
from r6.command_center.models import ConversationMessage, AgentTask
from r6.command_center.agents import load_agents, agent_for_event, get_agent
from r6.command_center import gateway


# ---------------------------------------------------------------------------
# Readiness pipeline
# ---------------------------------------------------------------------------

def readiness(tenant_id: str) -> dict:
    """
    5-stage pipeline: Stack Live → Data Connected → Records Ingested →
    Quality Curated → Insights Running.

    Each stage returns {state: ok|pending|blocked, detail: str}.
    """
    stages: list[dict] = []

    # --- 1. Stack Live -----------------------------------------------------
    # If this code is running, Flask is up. MCP status is probed separately.
    stages.append({
        "id": "stack-live",
        "label": "Stack Live",
        "state": "ok",
        "detail": "Flask + database ready",
    })

    # --- 2. Data Connected -------------------------------------------------
    fasten_count = FastenConnection.query.filter_by(
        tenant_id=tenant_id, connection_status="authorized"
    ).count()
    wearable_count = WearableConnection.query.filter_by(tenant_id=tenant_id).count()
    has_upstream = bool(os.environ.get("FHIR_UPSTREAM_URL", "").strip())
    has_healthex = _has_healthex_signal(tenant_id)

    source_count = fasten_count + wearable_count + (1 if has_upstream else 0) + (1 if has_healthex else 0)
    if source_count > 0:
        sources = []
        if fasten_count:
            sources.append(f"{fasten_count} Fasten")
        if wearable_count:
            sources.append(f"{wearable_count} wearable")
        if has_upstream:
            sources.append("upstream FHIR")
        if has_healthex:
            sources.append("HealthEx")
        stages.append({
            "id": "data-connected",
            "label": "Data Connected",
            "state": "ok",
            "detail": ", ".join(sources),
        })
    else:
        stages.append({
            "id": "data-connected",
            "label": "Data Connected",
            "state": "pending",
            "detail": "No sources connected yet",
        })

    # --- 3. Records Ingested ----------------------------------------------
    total_resources = R6Resource.query.filter_by(
        tenant_id=tenant_id, is_deleted=False
    ).count()
    if total_resources > 0:
        stages.append({
            "id": "records-ingested",
            "label": "Records Ingested",
            "state": "ok",
            "detail": f"{total_resources:,} records in store",
        })
    else:
        stages.append({
            "id": "records-ingested",
            "label": "Records Ingested",
            "state": "pending",
            "detail": "Store is empty — run fhir_seed or import a bundle",
        })

    # --- 4. Quality Curated -----------------------------------------------
    review_needed = R6Resource.query.filter_by(
        tenant_id=tenant_id, review_needed=True, is_deleted=False
    ).count()
    curated = R6Resource.query.filter_by(
        tenant_id=tenant_id, curation_state="curated", is_deleted=False
    ).count()
    curatr_runs = AuditEventRecord.query.filter(
        AuditEventRecord.tenant_id == tenant_id,
        AuditEventRecord.detail.like("%curatr%"),
    ).count()

    if total_resources == 0:
        stages.append({
            "id": "quality-curated",
            "label": "Quality Curated",
            "state": "pending",
            "detail": "Waiting on records",
        })
    elif curatr_runs == 0:
        stages.append({
            "id": "quality-curated",
            "label": "Quality Curated",
            "state": "pending",
            "detail": "curatr has not evaluated this tenant yet",
        })
    elif review_needed > 0:
        stages.append({
            "id": "quality-curated",
            "label": "Quality Curated",
            "state": "ok",
            "detail": f"{review_needed} flag{'s' if review_needed != 1 else ''} pending review, {curated} curated",
        })
    else:
        stages.append({
            "id": "quality-curated",
            "label": "Quality Curated",
            "state": "ok",
            "detail": f"All clear — {curated} curated",
        })

    # --- 5. Insights Running ----------------------------------------------
    # Proxy: has there been any read/search/curatr activity in the last 24h?
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_activity = AuditEventRecord.query.filter(
        AuditEventRecord.tenant_id == tenant_id,
        AuditEventRecord.recorded >= since,
    ).count()
    if recent_activity >= 5:
        stages.append({
            "id": "insights-running",
            "label": "Insights Running",
            "state": "ok",
            "detail": f"{recent_activity} agent actions in last 24h",
        })
    elif recent_activity > 0:
        stages.append({
            "id": "insights-running",
            "label": "Insights Running",
            "state": "pending",
            "detail": f"{recent_activity} action{'s' if recent_activity != 1 else ''} in last 24h (warming up)",
        })
    else:
        stages.append({
            "id": "insights-running",
            "label": "Insights Running",
            "state": "pending",
            "detail": "No recent agent activity",
        })

    # Summary line
    ok_count = sum(1 for s in stages if s["state"] == "ok")
    total = len(stages)
    if ok_count == total:
        summary = "Your personal health agent is ready."
    elif ok_count == 0:
        summary = "Let's get your health agent set up."
    else:
        summary = f"Setup {ok_count}/{total} complete."

    return {
        "tenant_id": tenant_id,
        "summary": summary,
        "completed": ok_count,
        "total": total,
        "stages": stages,
    }


def _has_healthex_signal(tenant_id: str) -> bool:
    """Heuristic: HealthEx imports leave audit events with 'healthex' in detail."""
    return db.session.query(AuditEventRecord.id).filter(
        AuditEventRecord.tenant_id == tenant_id,
        AuditEventRecord.detail.like("%healthex%"),
    ).first() is not None


# ---------------------------------------------------------------------------
# Latest actions (audit event stream)
# ---------------------------------------------------------------------------

def latest_actions(tenant_id: str, limit: int = 20) -> list[dict]:
    """Return the most recent N audit events for the tenant, with agent attribution."""
    events = (
        AuditEventRecord.query
        .filter_by(tenant_id=tenant_id)
        .order_by(desc(AuditEventRecord.recorded))
        .limit(limit)
        .all()
    )

    out = []
    for ev in events:
        agent = agent_for_event(ev)
        out.append({
            "id": ev.id,
            "event_type": ev.event_type,
            "resource_type": ev.resource_type,
            "resource_id": ev.resource_id,
            "outcome": ev.outcome,
            "agent_id": ev.agent_id,
            "agent_name": agent["name"] if agent else None,
            "agent_emoji": agent["emoji"] if agent else None,
            "detail": (ev.detail or "")[:200],
            "recorded": ev.recorded.isoformat() if ev.recorded else None,
        })
    return out


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def data_sources(tenant_id: str) -> list[dict]:
    """List all connected (and unconnected) data sources for the tenant."""
    sources: list[dict] = []

    # --- HealthEx (via Claude MCP) ----------------------------------------
    has_hex = _has_healthex_signal(tenant_id)
    latest_hex = None
    if has_hex:
        ev = (
            AuditEventRecord.query
            .filter(
                AuditEventRecord.tenant_id == tenant_id,
                AuditEventRecord.detail.like("%healthex%"),
            )
            .order_by(desc(AuditEventRecord.recorded))
            .first()
        )
        latest_hex = ev.recorded.isoformat() if ev and ev.recorded else None
    sources.append({
        "id": "healthex",
        "name": "HealthEx",
        "description": "Claude-integrated national network pull (CommonWell, Carequality)",
        "connected": has_hex,
        "detail": "Connected via Claude.ai Settings → Integrations" if has_hex
                  else "Connect in Claude.ai → HealthEx integration",
        "last_activity": latest_hex,
    })

    # --- Fasten Connect ---------------------------------------------------
    fastens = FastenConnection.query.filter_by(tenant_id=tenant_id).all()
    if fastens:
        latest_sync = max(
            (f.last_export_at for f in fastens if f.last_export_at),
            default=None,
        )
        sources.append({
            "id": "fasten",
            "name": "Fasten Connect",
            "description": "EHR portal connections (Epic, Cerner, AHN, etc.)",
            "connected": True,
            "detail": f"{len(fastens)} org connection{'s' if len(fastens) != 1 else ''}",
            "last_activity": latest_sync.isoformat() if latest_sync else None,
        })
    else:
        sources.append({
            "id": "fasten",
            "name": "Fasten Connect",
            "description": "EHR portal connections (Epic, Cerner, AHN, etc.)",
            "connected": False,
            "detail": "Set FASTEN_PUBLIC_KEY to enable widget",
            "last_activity": None,
        })

    # --- Wearables --------------------------------------------------------
    wearables = WearableConnection.query.filter_by(tenant_id=tenant_id).all()
    if wearables:
        latest_sync = max(
            (w.last_sync_at for w in wearables if w.last_sync_at),
            default=None,
        )
        providers = ", ".join(sorted({w.provider for w in wearables}))
        sources.append({
            "id": "wearables",
            "name": "Wearables",
            "description": "Open Wearables sidecar (Fitbit, Oura, Whoop, Garmin)",
            "connected": True,
            "detail": f"{len(wearables)} connection{'s' if len(wearables) != 1 else ''}: {providers}",
            "last_activity": latest_sync.isoformat() if latest_sync else None,
        })
    else:
        configured = bool(os.environ.get("OPEN_WEARABLES_URL", "").strip())
        sources.append({
            "id": "wearables",
            "name": "Wearables",
            "description": "Open Wearables sidecar (Fitbit, Oura, Whoop, Garmin)",
            "connected": False,
            "detail": "Sidecar configured, no devices linked" if configured
                      else "Set OPEN_WEARABLES_URL to enable",
            "last_activity": None,
        })

    # --- Upstream FHIR Proxy ----------------------------------------------
    upstream = os.environ.get("FHIR_UPSTREAM_URL", "").strip()
    if upstream:
        sources.append({
            "id": "upstream",
            "name": "Upstream FHIR",
            "description": "Live proxy with guardrails applied",
            "connected": True,
            "detail": upstream,
            "last_activity": None,
        })

    # --- SmartHealthConnect (sister plugin) -------------------------------
    shc_signal = db.session.query(AuditEventRecord.id).filter(
        AuditEventRecord.tenant_id == tenant_id,
        AuditEventRecord.detail.like("%smarthealthconnect%"),
    ).first() is not None
    sources.append({
        "id": "smarthealthconnect",
        "name": "SmartHealthConnect",
        "description": "Companion plugin for SMART-on-FHIR app launches",
        "connected": shc_signal,
        "detail": "Detected via audit events" if shc_signal
                  else "Not configured",
        "last_activity": None,
    })

    return sources


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def skills_status(tenant_id: str) -> list[dict]:
    """Enumerate skills/ folder and project recent-activity status per skill."""
    skills: list[dict] = []

    # Map skill name → list of tool names that identify its activity
    skill_tools: dict[str, list[str]] = {
        "curatr": ["curatr_evaluate", "curatr_apply_fix"],
        "personal-health-records": ["fhir_seed", "Bundle/$ingest-context"],
        "fhir-r6-guardrails": ["fhir_search", "fhir_read", "fhir_lastn", "fhir_stats"],
        "phi-redaction": ["$deidentify"],
        "fasten-connect": ["fasten"],
        "fhir-upstream-proxy": ["upstream"],
        "healthex-export": ["healthex"],
    }

    if not _SKILLS_DIR.exists():
        return skills

    since_week = datetime.now(timezone.utc) - timedelta(days=7)

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_id = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        # Extract the description from the SKILL.md frontmatter (heuristic: first non-empty
        # "description:" line). Keep it cheap — no full YAML parse.
        description = ""
        for raw in skill_md.read_text(errors="ignore").splitlines()[:40]:
            stripped = raw.strip()
            if stripped.startswith("description:"):
                description = stripped.split(":", 1)[1].strip().strip(">").strip()
                if description:
                    break

        # Count recent audit events matching any of the skill's tool patterns
        patterns = skill_tools.get(skill_id, [])
        recent_count = 0
        last_activity = None
        if patterns:
            from sqlalchemy import or_ as sql_or
            q = AuditEventRecord.query.filter(
                AuditEventRecord.tenant_id == tenant_id,
                AuditEventRecord.recorded >= since_week,
                sql_or(*[AuditEventRecord.detail.like(f"%{p}%") for p in patterns]),
            )
            recent_count = q.count()
            latest = q.order_by(desc(AuditEventRecord.recorded)).first()
            if latest:
                last_activity = latest.recorded.isoformat()

        skills.append({
            "id": skill_id,
            "name": skill_id.replace("-", " ").title(),
            "description": description[:180],
            "recent_activity_count": recent_count,
            "last_activity": last_activity,
            "state": "active" if recent_count > 0 else "idle",
        })

    return skills


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def agents_status(tenant_id: str) -> list[dict]:
    """List agents with recent activity, task counts, and last conversation."""
    since_week = datetime.now(timezone.utc) - timedelta(days=7)
    out: list[dict] = []

    for agent in load_agents():
        agent_id = agent["id"]

        # Recent audit events attributed to this agent (by agent_id or tool pattern)
        from sqlalchemy import or_ as sql_or
        patterns = agent.get("tool_patterns") or []
        tool_filters = [AuditEventRecord.detail.like(f"%{p}%") for p in patterns]
        agent_filter = AuditEventRecord.agent_id == agent_id
        if tool_filters:
            filter_clause = sql_or(agent_filter, *tool_filters)
        else:
            filter_clause = agent_filter

        recent_count = AuditEventRecord.query.filter(
            AuditEventRecord.tenant_id == tenant_id,
            AuditEventRecord.recorded >= since_week,
            filter_clause,
        ).count()

        last_event = (
            AuditEventRecord.query
            .filter(
                AuditEventRecord.tenant_id == tenant_id,
                filter_clause,
            )
            .order_by(desc(AuditEventRecord.recorded))
            .first()
        )

        # Conversation + task counts
        conversation_count = ConversationMessage.query.filter_by(
            tenant_id=tenant_id, agent_id=agent_id
        ).count()
        last_conversation = (
            ConversationMessage.query
            .filter_by(tenant_id=tenant_id, agent_id=agent_id)
            .order_by(desc(ConversationMessage.created_at))
            .first()
        )
        pending_tasks = AgentTask.query.filter_by(
            tenant_id=tenant_id, agent_id=agent_id, status="pending"
        ).count()

        out.append({
            "id": agent_id,
            "name": agent["name"],
            "role": agent.get("role"),
            "emoji": agent.get("emoji", "🤖"),
            "color": agent.get("color", "#64748b"),
            "telegram": agent.get("telegram"),
            "description": agent.get("description", "").strip(),
            "skills": agent.get("skills", []),
            "tool_patterns": agent.get("tool_patterns", []),
            "example_prompts": agent.get("example_prompts", []),
            "recent_activity_count": recent_count,
            "last_activity": last_event.recorded.isoformat() if last_event else None,
            "conversation_count": conversation_count,
            "last_conversation": last_conversation.to_dict() if last_conversation else None,
            "pending_tasks": pending_tasks,
            "state": "active" if recent_count > 0 or conversation_count > 0 else "idle",
        })

    return out


# ---------------------------------------------------------------------------
# Conversations & tasks
# ---------------------------------------------------------------------------

def recent_conversations(tenant_id: str, limit: int = 15) -> list[dict]:
    """Return the most recent chat turns across all agents + channels."""
    msgs = (
        ConversationMessage.query
        .filter_by(tenant_id=tenant_id)
        .order_by(desc(ConversationMessage.created_at))
        .limit(limit)
        .all()
    )
    out = []
    for m in msgs:
        agent = get_agent(m.agent_id) if m.agent_id else None
        d = m.to_dict()
        d["agent_name"] = agent["name"] if agent else None
        d["agent_emoji"] = agent["emoji"] if agent else None
        out.append(d)
    return out


def pending_tasks(tenant_id: str, limit: int = 20) -> list[dict]:
    """Return open (pending / in_progress) tasks, newest first."""
    tasks = (
        AgentTask.query
        .filter(
            AgentTask.tenant_id == tenant_id,
            AgentTask.status.in_(["pending", "in_progress"]),
        )
        .order_by(desc(AgentTask.created_at))
        .limit(limit)
        .all()
    )
    out = []
    for t in tasks:
        agent = get_agent(t.agent_id)
        d = t.to_dict()
        d["agent_name"] = agent["name"] if agent else None
        d["agent_emoji"] = agent["emoji"] if agent else None
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Insights (v1: surface existing curatr flags + simple resource counts)
# ---------------------------------------------------------------------------

def insights(tenant_id: str, limit: int = 10) -> list[dict]:
    """
    V1 insights: surface resources that have curatr meta tags AND the counts
    of each resource type. Simple rules engine can be added later.
    """
    out: list[dict] = []

    # Curatr flag buckets (by severity if present in the resource note)
    flagged = R6Resource.query.filter_by(
        tenant_id=tenant_id, review_needed=True, is_deleted=False
    ).limit(limit).all()

    for r in flagged:
        import json as _json
        try:
            body = _json.loads(r.resource_json)
        except (ValueError, TypeError):
            continue

        flag_codes = []
        for tag in (body.get("meta") or {}).get("tag", []):
            system = tag.get("system", "")
            code = tag.get("code", "")
            if "curatr" in system.lower() and code.startswith("flag:"):
                flag_codes.append(code.replace("flag:", ""))

        note_text = ""
        for note in (body.get("note") or []):
            if "CURATR" in (note.get("text") or ""):
                note_text = note["text"]
                break

        severity = "medium"
        if "CRITICAL" in note_text:
            severity = "critical"
        elif "HIGH" in note_text:
            severity = "high"
        elif "LOW" in note_text:
            severity = "low"

        out.append({
            "id": f"curatr:{r.id}",
            "kind": "data-quality",
            "severity": severity,
            "title": f"{r.resource_type} flagged: {', '.join(flag_codes) or 'review needed'}",
            "description": note_text[:280],
            "resource_ref": f"{r.resource_type}/{r.id}",
        })

    return out


# ---------------------------------------------------------------------------
# Overview (ticker-style summary for the hero banner)
# ---------------------------------------------------------------------------

def overview(tenant_id: str) -> dict:
    """Compact headline stats for the dashboard hero banner."""
    total = R6Resource.query.filter_by(
        tenant_id=tenant_id, is_deleted=False
    ).count()
    flags = R6Resource.query.filter_by(
        tenant_id=tenant_id, review_needed=True, is_deleted=False
    ).count()
    tasks = AgentTask.query.filter_by(
        tenant_id=tenant_id, status="pending"
    ).count()
    since_day = datetime.now(timezone.utc) - timedelta(hours=24)
    activity_24h = AuditEventRecord.query.filter(
        AuditEventRecord.tenant_id == tenant_id,
        AuditEventRecord.recorded >= since_day,
    ).count()

    latest = (
        AuditEventRecord.query
        .filter_by(tenant_id=tenant_id)
        .order_by(desc(AuditEventRecord.recorded))
        .first()
    )

    return {
        "tenant_id": tenant_id,
        "record_count": total,
        "flag_count": flags,
        "pending_task_count": tasks,
        "activity_24h": activity_24h,
        "last_activity": latest.recorded.isoformat() if latest and latest.recorded else None,
    }


# ---------------------------------------------------------------------------
# System status (Flask + MCP + gateway + Redis)
# ---------------------------------------------------------------------------

def system_status() -> dict:
    """Live probes for infrastructure pieces."""
    gw = gateway.probe()
    return {
        "flask": {"up": True, "mode": _local_or_proxy()},
        "mcp_server": _probe_mcp(),
        "openclaw_gateway": gw.to_dict(),
        "redis": _probe_redis(),
    }


def _local_or_proxy() -> str:
    return "upstream-proxy" if os.environ.get("FHIR_UPSTREAM_URL", "").strip() else "local"


def _probe_mcp() -> dict:
    url = os.environ.get("MCP_HEALTH_URL", "http://localhost:3001/health")
    try:
        import httpx as _httpx
        r = _httpx.get(url, timeout=1.5)
        return {"up": r.status_code < 500, "url": url, "status_code": r.status_code}
    except Exception as e:  # noqa: BLE001
        return {"up": False, "url": url, "error": str(e)[:120]}


def _probe_redis() -> dict:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return {"up": False, "configured": False, "detail": "REDIS_URL not set"}
    try:
        import redis  # type: ignore
        client = redis.from_url(url, socket_connect_timeout=1)
        client.ping()
        return {"up": True, "configured": True, "url": url.split("@")[-1]}
    except Exception as e:  # noqa: BLE001
        return {"up": False, "configured": True, "error": str(e)[:120]}
