"""
Flask Blueprint for the command center.

Routes:
    GET  /command-center                                  — HTML dashboard page
    GET  /command-center?t=<signed-token>                 — signed-link login
    GET  /command-center/login                            — link-required page
    GET  /command-center/api/overview?tenant=<id>         — hero stats
    GET  /command-center/api/readiness?tenant=<id>        — 5-stage pipeline
    GET  /command-center/api/actions?tenant=<id>          — audit event stream
    GET  /command-center/api/sources?tenant=<id>          — data sources
    GET  /command-center/api/skills?tenant=<id>           — skills status
    GET  /command-center/api/agents?tenant=<id>           — agent personas + stats
    GET  /command-center/api/conversations?tenant=<id>    — recent chat turns
    GET  /command-center/api/tasks?tenant=<id>            — pending tasks
    GET  /command-center/api/insights?tenant=<id>         — derived insights
    GET  /command-center/api/system                       — Flask/MCP/gateway/redis probes
    POST /command-center/api/conversations                — log a chat turn
    POST /command-center/api/tasks                        — create a task
    PATCH /command-center/api/tasks/<id>                  — update status
    POST /command-center/api/generate-link                — mint a signed dashboard URL
    POST /command-center/logout                           — clear session tenant
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, jsonify, redirect, render_template, request, session, url_for
)

from models import db
from r6.command_center import projector, access
from r6.command_center.models import ConversationMessage, AgentTask
from r6.command_center.agents import load_agents, get_agent
from r6.stepup import validate_step_up_token

logger = logging.getLogger(__name__)

command_center_blueprint = Blueprint(
    "command_center",
    __name__,
    url_prefix="/command-center",
)

DEFAULT_TENANT = "desktop-demo"
SESSION_KEY = "cc_tenant"


def _tenant() -> str:
    """
    Resolve the active tenant for the current request. Priority:
    1. Session (set after a signed-link login)
    2. ?tenant= query param (allowed only for public tenants)
    3. X-Tenant-Id header
    4. DEFAULT_TENANT (desktop-demo)
    """
    return (
        session.get(SESSION_KEY)
        or request.args.get("tenant")
        or request.headers.get("X-Tenant-Id")
        or DEFAULT_TENANT
    )


def _authorized_for(tenant_id: str) -> bool:
    """Check if the current session/request is authorized for this tenant."""
    if access.is_public(tenant_id):
        return True
    if session.get(SESSION_KEY) == tenant_id:
        return True
    return False


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@command_center_blueprint.route("", methods=["GET"])
@command_center_blueprint.route("/", methods=["GET"])
def dashboard():
    # Signed-link login — exchange `?t=<token>` for a session and redirect
    # to a clean URL (so the token doesn't linger in browser history).
    token = request.args.get("t")
    if token:
        payload = access.verify_access_token(token)
        if payload:
            session[SESSION_KEY] = payload["tenant_id"]
            return redirect(
                url_for("command_center.dashboard", tenant=payload["tenant_id"])
            )
        return render_template(
            "command_center_login.html",
            error="This link has expired or is invalid. Ask your Telegram agent for a fresh one.",
        ), 401

    tenant = _tenant()
    # Non-public tenants require a valid session
    if not _authorized_for(tenant):
        return render_template(
            "command_center_login.html",
            error=None,
            tenant=tenant,
        ), 401

    return render_template(
        "command_center.html",
        tenant_id=tenant,
        agents=load_agents(),
    )


@command_center_blueprint.route("/login", methods=["GET"])
def login_page():
    """Standalone landing for users who don't have a link yet."""
    return render_template("command_center_login.html", error=None)


@command_center_blueprint.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop(SESSION_KEY, None)
    return redirect(url_for("command_center.dashboard"))


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------

@command_center_blueprint.route("/api/overview", methods=["GET"])
def api_overview():
    return jsonify(projector.overview(_tenant()))


@command_center_blueprint.route("/api/readiness", methods=["GET"])
def api_readiness():
    return jsonify(projector.readiness(_tenant()))


@command_center_blueprint.route("/api/actions", methods=["GET"])
def api_actions():
    limit = min(int(request.args.get("limit", "20")), 100)
    return jsonify(projector.latest_actions(_tenant(), limit=limit))


@command_center_blueprint.route("/api/sources", methods=["GET"])
def api_sources():
    return jsonify(projector.data_sources(_tenant()))


@command_center_blueprint.route("/api/skills", methods=["GET"])
def api_skills():
    return jsonify(projector.skills_status(_tenant()))


@command_center_blueprint.route("/api/agents", methods=["GET"])
def api_agents():
    return jsonify(projector.agents_status(_tenant()))


@command_center_blueprint.route("/api/conversations", methods=["GET"])
def api_conversations_list():
    limit = min(int(request.args.get("limit", "15")), 100)
    return jsonify(projector.recent_conversations(_tenant(), limit=limit))


@command_center_blueprint.route("/api/tasks", methods=["GET"])
def api_tasks_list():
    limit = min(int(request.args.get("limit", "20")), 100)
    return jsonify(projector.pending_tasks(_tenant(), limit=limit))


@command_center_blueprint.route("/api/insights", methods=["GET"])
def api_insights():
    limit = min(int(request.args.get("limit", "10")), 50)
    return jsonify(projector.insights(_tenant(), limit=limit))


@command_center_blueprint.route("/api/system", methods=["GET"])
def api_system():
    return jsonify(projector.system_status())


# ---------------------------------------------------------------------------
# Write APIs — used by Telegram bot + any future channel to persist activity
# ---------------------------------------------------------------------------

@command_center_blueprint.route("/api/conversations", methods=["POST"])
def api_conversations_create():
    """
    Log a single conversation turn.

    Body:
        tenant_id: str (required)
        agent_id: str (optional)
        channel: str (telegram|mcp|api|web, default 'unknown')
        session_id: str (e.g. telegram chat_id)
        user_id: str
        role: str (user|assistant|system, required)
        text: str (required)
        metadata: dict (optional)
    """
    body = request.get_json(silent=True) or {}

    tenant_id = body.get("tenant_id") or request.headers.get("X-Tenant-Id")
    role = body.get("role")
    text = body.get("text")
    if not tenant_id or not role or text is None:
        return jsonify({"error": "tenant_id, role, and text are required"}), 400

    agent_id = body.get("agent_id")
    if agent_id and not get_agent(agent_id):
        return jsonify({"error": f"unknown agent_id: {agent_id}"}), 400

    import json as _json
    md = body.get("metadata")
    metadata_json = _json.dumps(md) if isinstance(md, (dict, list)) else None

    msg = ConversationMessage(
        tenant_id=tenant_id,
        agent_id=agent_id,
        channel=body.get("channel", "unknown"),
        session_id=body.get("session_id"),
        user_id=body.get("user_id"),
        role=role,
        text=text,
        metadata_json=metadata_json,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify(msg.to_dict()), 201


@command_center_blueprint.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    """
    Create a new pending task.

    Body:
        tenant_id: str (required)
        agent_id: str (required)
        title: str (required)
        description: str
        priority: low|medium|high|critical
        resource_ref: str (FHIR ref like "Condition/abc")
        source: str (free-form, e.g. "curatr", "care-gap", "telegram")
    """
    body = request.get_json(silent=True) or {}

    tenant_id = body.get("tenant_id") or request.headers.get("X-Tenant-Id")
    agent_id = body.get("agent_id")
    title = body.get("title")
    if not tenant_id or not agent_id or not title:
        return jsonify({"error": "tenant_id, agent_id, and title are required"}), 400
    if not get_agent(agent_id):
        return jsonify({"error": f"unknown agent_id: {agent_id}"}), 400

    task = AgentTask(
        tenant_id=tenant_id,
        agent_id=agent_id,
        title=title[:256],
        description=body.get("description"),
        priority=body.get("priority", "medium"),
        resource_ref=body.get("resource_ref"),
        source=body.get("source"),
    )
    db.session.add(task)
    db.session.commit()
    return jsonify(task.to_dict()), 201


@command_center_blueprint.route("/api/tasks/<task_id>", methods=["PATCH"])
def api_tasks_update(task_id: str):
    """
    Update a task's status. Body: {status: pending|in_progress|completed|dismissed}.
    """
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in ("pending", "in_progress", "completed", "dismissed"):
        return jsonify({"error": "status must be one of pending|in_progress|completed|dismissed"}), 400

    task = AgentTask.query.filter_by(id=task_id).first()
    if not task:
        return jsonify({"error": "task not found"}), 404

    task.status = new_status
    task.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(task.to_dict())


# ---------------------------------------------------------------------------
# Signed-link minting — used by the Telegram bot to send shareable URLs
# ---------------------------------------------------------------------------

@command_center_blueprint.route("/api/generate-link", methods=["POST"])
def api_generate_link():
    """
    Mint a signed dashboard URL. Requires a valid step-up token (so only
    the bot owner / authorized agents can create links).

    Body:
        tenant_id: str (required)
        agent_id:  str (optional — informational, not enforced)
        base_url:  str (optional — override; defaults to request.host_url)

    Headers:
        X-Step-Up-Token: valid HMAC step-up token for tenant_id

    Returns:
        {url, token, expires_in_hours, tenant_id}
    """
    body = request.get_json(silent=True) or {}
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        return jsonify({"error": "tenant_id required"}), 400

    # Public tenants don't need a token — anyone can link to the demo
    if not access.is_public(tenant_id):
        step_up = request.headers.get("X-Step-Up-Token")
        if not step_up:
            return jsonify({
                "error": "X-Step-Up-Token required for non-public tenants"
            }), 401
        valid, err = validate_step_up_token(step_up, tenant_id)
        if not valid:
            return jsonify({"error": f"step-up token rejected: {err}"}), 401

    import os
    base_url = (
        body.get("base_url")
        or os.environ.get("DASHBOARD_BASE_URL", "").strip()
        or request.host_url.rstrip("/")
    )
    agent_id = body.get("agent_id")
    url = access.build_dashboard_url(base_url, tenant_id, agent_id=agent_id)
    token = access.generate_access_token(tenant_id, agent_id=agent_id)

    return jsonify({
        "url": url,
        "token": token,
        "expires_in_hours": access.DASHBOARD_TTL_HOURS,
        "tenant_id": tenant_id,
    })
