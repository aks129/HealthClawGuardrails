"""
tests/test_command_center.py

Coverage:
- Agent registry load + lookup
- Projector functions (readiness, actions, agents, sources, skills, insights, overview)
- Conversation + task REST endpoints (create/list/update)
- OpenClaw gateway probe (configured / unreachable / cached)
- Dashboard HTML page renders
"""

import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import httpx
import pytest

from models import db
from r6.models import R6Resource, AuditEventRecord
from r6.command_center import agents, projector, gateway
from r6.command_center.models import ConversationMessage, AgentTask

TENANT = "test-tenant"


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

class TestAgentRegistry:

    def test_loads_six_agents(self):
        agents.load_agents.cache_clear()
        items = agents.load_agents()
        ids = {a["id"] for a in items}
        assert {"sally", "mary", "dom", "shervin", "ronny", "joe"} <= ids

    def test_get_agent_returns_dict(self):
        a = agents.get_agent("sally")
        assert a is not None
        assert a["name"] == "Sally"
        assert a["role"] == "PCP Advisor"
        assert "emoji" in a
        assert isinstance(a.get("tool_patterns", []), list)

    def test_get_agent_unknown_returns_none(self):
        assert agents.get_agent("no-such-agent") is None

    def test_agent_for_tool_curatr_goes_to_joe(self):
        a = agents.agent_for_tool("curatr_evaluate")
        assert a is not None
        assert a["id"] == "joe"

    def test_agent_for_tool_wearable_goes_to_dom(self):
        a = agents.agent_for_tool("wearable_sync_status")
        assert a is not None
        assert a["id"] == "dom"

    def test_agent_templates_load(self):
        agents.load_agent_templates.cache_clear()
        data = agents.load_agent_templates()
        assert "templates" in data
        assert "bundles" in data
        template_ids = {t["id"] for t in data["templates"]}
        # Spot-check a few well-known templates
        assert {"pcp-advisor", "pharmacy-helper", "fitness-coach"} <= template_ids
        # Bundles exist
        bundle_ids = {b["id"] for b in data["bundles"]}
        assert {"solo-essentials", "athlete-plus"} <= bundle_ids


# ---------------------------------------------------------------------------
# Projector — readiness
# ---------------------------------------------------------------------------

class TestReadiness:

    def test_empty_tenant_shows_pending_stages(self, app):
        with app.app_context():
            out = projector.readiness(TENANT)
            assert out["tenant_id"] == TENANT
            assert out["total"] == 5
            # Stack is always live; the rest should be pending
            states = {s["id"]: s["state"] for s in out["stages"]}
            assert states["stack-live"] == "ok"
            assert states["records-ingested"] == "pending"

    def test_readiness_advances_when_resources_seeded(self, app):
        with app.app_context():
            db.session.add(R6Resource(
                resource_type="Patient",
                resource_json='{"resourceType":"Patient"}',
                tenant_id=TENANT,
            ))
            db.session.commit()
            out = projector.readiness(TENANT)
            states = {s["id"]: s["state"] for s in out["stages"]}
            assert states["records-ingested"] == "ok"


# ---------------------------------------------------------------------------
# Projector — actions + agents
# ---------------------------------------------------------------------------

class TestActionsAndAgents:

    def test_latest_actions_returns_events_for_tenant(self, app):
        with app.app_context():
            db.session.add(AuditEventRecord(
                event_type="read",
                resource_type="Patient",
                tenant_id=TENANT,
                agent_id="sally",
                detail="fhir_search",
            ))
            db.session.commit()
            out = projector.latest_actions(TENANT, limit=5)
            assert len(out) == 1
            assert out[0]["event_type"] == "read"
            assert out[0]["agent_name"] == "Sally"
            assert out[0]["agent_emoji"]

    def test_agents_status_includes_conversation_count(self, app):
        with app.app_context():
            db.session.add(ConversationMessage(
                tenant_id=TENANT,
                agent_id="sally",
                channel="telegram",
                role="user",
                text="/health",
            ))
            db.session.commit()
            out = projector.agents_status(TENANT)
            advisor = next(a for a in out if a["id"] == "sally")
            assert advisor["conversation_count"] == 1
            assert advisor["state"] == "active"
            assert advisor["last_conversation"] is not None

    def test_agents_status_idle_when_no_activity(self, app):
        with app.app_context():
            out = projector.agents_status(TENANT)
            for a in out:
                assert a["state"] == "idle"
                assert a["recent_activity_count"] == 0
                assert a["conversation_count"] == 0


# ---------------------------------------------------------------------------
# Projector — sources / skills / insights
# ---------------------------------------------------------------------------

class TestSourcesSkillsInsights:

    def test_data_sources_lists_all_even_when_none_connected(self, app):
        with app.app_context():
            out = projector.data_sources(TENANT)
            names = {s["name"] for s in out}
            assert {"HealthEx", "Fasten Connect", "Wearables"} <= names

    def test_skills_enumerates_skills_dir(self, app):
        with app.app_context():
            out = projector.skills_status(TENANT)
            ids = {s["id"] for s in out}
            # Skill dir contains at least these
            assert {"curatr", "personal-health-records"} <= ids

    def test_insights_surfaces_flagged_resources(self, app):
        with app.app_context():
            body = {
                "resourceType": "Condition",
                "meta": {"tag": [{"system": "https://healthclaw.io/curatr", "code": "flag:icd9-deprecated"}]},
                "note": [{"text": "CURATR CRITICAL: ICD-9 code present"}],
            }
            r = R6Resource(
                resource_type="Condition",
                resource_json=json.dumps(body),
                tenant_id=TENANT,
            )
            r.review_needed = True
            db.session.add(r)
            db.session.commit()
            out = projector.insights(TENANT)
            assert len(out) == 1
            assert out[0]["severity"] == "critical"
            assert "icd9-deprecated" in out[0]["title"]


# ---------------------------------------------------------------------------
# Projector — system status + overview
# ---------------------------------------------------------------------------

class TestSystemStatus:

    def test_system_status_shape(self, app):
        with app.app_context():
            out = projector.system_status()
            assert out["flask"]["up"] is True
            assert "openclaw_gateway" in out
            assert "mcp_server" in out
            assert "redis" in out

    def test_overview_shape(self, app):
        with app.app_context():
            out = projector.overview(TENANT)
            assert out["tenant_id"] == TENANT
            for key in ("record_count", "flag_count", "pending_task_count", "activity_24h"):
                assert key in out


# ---------------------------------------------------------------------------
# OpenClaw gateway probe
# ---------------------------------------------------------------------------

class TestGatewayProbe:

    def setup_method(self):
        # Clear module-level cache
        gateway._cached = None
        gateway._cached_at = 0.0

    def test_probe_unreachable_returns_structured_error(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:1/healthz")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TIMEOUT", "0.5")
        gateway._cached = None
        status = gateway.probe(force=True)
        assert status.reachable is False
        assert status.configured is True
        assert status.error is not None

    def test_probe_caches_result(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:1/healthz")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TIMEOUT", "0.5")
        gateway._cached = None
        first = gateway.probe(force=True)
        second = gateway.probe()  # should return cached
        assert first.checked_at == second.checked_at


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

class TestRestEndpoints:

    def test_dashboard_html_renders(self, client):
        resp = client.get("/command-center")
        assert resp.status_code == 200
        assert b"My Health in Good Hands" in resp.data
        assert b"Sally" in resp.data

    def test_api_overview(self, client):
        resp = client.get("/command-center/api/overview", query_string={"tenant": TENANT})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["tenant_id"] == TENANT

    def test_api_readiness(self, client):
        resp = client.get("/command-center/api/readiness", query_string={"tenant": TENANT})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] == 5
        assert len(body["stages"]) == 5

    def test_api_agents_list(self, client):
        resp = client.get("/command-center/api/agents", query_string={"tenant": TENANT})
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body) == 6
        assert {a["id"] for a in body} == {
            "sally", "mary", "dom", "shervin", "ronny", "joe"
        }

    def test_api_agent_templates(self, client):
        resp = client.get("/command-center/api/agent-templates")
        assert resp.status_code == 200
        body = resp.get_json()
        template_ids = {t["id"] for t in body["templates"]}
        assert "pcp-advisor" in template_ids
        assert len(body["bundles"]) >= 3

    def test_api_openclaw_sessions_returns_structured_empty_when_unconfigured(self, client, monkeypatch):
        monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
        resp = client.get("/command-center/api/openclaw/sessions")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "gateway" in body
        assert "sessions" in body
        assert body["sessions"] == []

    def test_api_sources(self, client):
        resp = client.get("/command-center/api/sources", query_string={"tenant": TENANT})
        assert resp.status_code == 200
        assert len(resp.get_json()) >= 3

    def test_api_skills(self, client):
        resp = client.get("/command-center/api/skills", query_string={"tenant": TENANT})
        assert resp.status_code == 200

    def test_api_system(self, client):
        resp = client.get("/command-center/api/system")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["flask"]["up"] is True

    def test_api_conversations_post_and_get(self, client):
        payload = {
            "tenant_id": TENANT,
            "agent_id": "sally",
            "channel": "telegram",
            "session_id": "chat-123",
            "user_id": "tg-456",
            "role": "user",
            "text": "/health",
        }
        resp = client.post("/command-center/api/conversations", json=payload)
        assert resp.status_code == 201
        created = resp.get_json()
        assert created["tenant_id"] == TENANT
        assert created["agent_id"] == "sally"

        resp = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": TENANT},
        )
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "/health"
        assert msgs[0]["agent_emoji"] == "🩺"

    def test_api_conversations_post_rejects_missing_fields(self, client):
        resp = client.post(
            "/command-center/api/conversations",
            json={"tenant_id": TENANT, "role": "user"},  # missing text
        )
        assert resp.status_code == 400

    def test_api_conversations_post_rejects_unknown_agent(self, client):
        resp = client.post(
            "/command-center/api/conversations",
            json={
                "tenant_id": TENANT,
                "role": "user",
                "text": "hi",
                "agent_id": "bogus",
            },
        )
        assert resp.status_code == 400

    def test_api_tasks_create_list_and_update(self, client):
        # Create
        create = client.post("/command-center/api/tasks", json={
            "tenant_id": TENANT,
            "agent_id": "joe",
            "title": "Approve ICD-9 fix",
            "priority": "high",
            "source": "curatr",
        })
        assert create.status_code == 201
        task = create.get_json()
        assert task["status"] == "pending"
        task_id = task["id"]

        # List
        listed = client.get("/command-center/api/tasks", query_string={"tenant": TENANT})
        assert listed.status_code == 200
        assert any(t["id"] == task_id for t in listed.get_json())

        # Update to completed
        updated = client.patch(
            f"/command-center/api/tasks/{task_id}",
            json={"status": "completed"},
        )
        assert updated.status_code == 200
        assert updated.get_json()["status"] == "completed"

        # Listing pending should no longer include it
        listed2 = client.get("/command-center/api/tasks", query_string={"tenant": TENANT})
        assert not any(t["id"] == task_id for t in listed2.get_json())

    def test_api_tasks_update_rejects_bad_status(self, client):
        create = client.post("/command-center/api/tasks", json={
            "tenant_id": TENANT,
            "agent_id": "sally",
            "title": "X",
        })
        task_id = create.get_json()["id"]

        resp = client.patch(
            f"/command-center/api/tasks/{task_id}",
            json={"status": "garbage"},
        )
        assert resp.status_code == 400

    def test_api_tasks_update_404_for_missing(self, client):
        resp = client.patch(
            "/command-center/api/tasks/no-such-id",
            json={"status": "completed"},
        )
        assert resp.status_code == 404

    def test_api_insights_empty_by_default(self, client):
        resp = client.get("/command-center/api/insights", query_string={"tenant": TENANT})
        assert resp.status_code == 200
        assert resp.get_json() == []


# ---------------------------------------------------------------------------
# Signed-link access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    def test_desktop_demo_is_public(self):
        from r6.command_center import access
        assert access.is_public("desktop-demo") is True
        assert access.is_public("my-private-tenant") is False

    def test_generate_and_verify_roundtrip(self):
        from r6.command_center import access
        token = access.generate_access_token("ev-personal", agent_id="sally")
        payload = access.verify_access_token(token)
        assert payload is not None
        assert payload["tenant_id"] == "ev-personal"
        assert payload["agent_id"] == "sally"

    def test_bad_token_returns_none(self):
        from r6.command_center import access
        assert access.verify_access_token("not-a-real-token") is None
        assert access.verify_access_token("") is None

    def test_build_dashboard_url_format(self):
        from r6.command_center import access
        url = access.build_dashboard_url(
            "https://healthclaw.io", "ev-personal", agent_id="h"
        )
        assert url.startswith("https://healthclaw.io/command-center?tenant=ev-personal&t=")


class TestSignedLinkFlow:

    def test_private_tenant_requires_session(self, client):
        resp = client.get("/command-center", query_string={"tenant": "ev-personal"})
        assert resp.status_code == 401
        assert b"Your personal health command center" in resp.data

    def test_public_tenant_no_auth_required(self, client):
        resp = client.get("/command-center", query_string={"tenant": "desktop-demo"})
        assert resp.status_code == 200

    def test_valid_signed_link_logs_in_and_redirects(self, client):
        from r6.command_center import access
        token = access.generate_access_token("ev-personal")
        resp = client.get("/command-center", query_string={"t": token})
        assert resp.status_code == 302
        assert "ev-personal" in resp.headers["Location"]

        # Session is now sticky — follow-up request works
        resp2 = client.get("/command-center", query_string={"tenant": "ev-personal"})
        assert resp2.status_code == 200

    def test_expired_token_shows_error(self, client):
        resp = client.get("/command-center", query_string={"t": "garbage.token.here"})
        assert resp.status_code == 401
        assert b"expired or is invalid" in resp.data

    def test_login_page_renders(self, client):
        resp = client.get("/command-center/login")
        assert resp.status_code == 200
        assert b"/dashboard" in resp.data

    def test_logout_clears_session(self, client):
        from r6.command_center import access
        token = access.generate_access_token("ev-personal")
        client.get("/command-center", query_string={"t": token})  # login

        client.get("/command-center/logout")
        # Private tenant should now require auth again
        resp = client.get("/command-center", query_string={"tenant": "ev-personal"})
        assert resp.status_code == 401


class TestGenerateLinkEndpoint:

    def test_public_tenant_no_stepup_required(self, client):
        resp = client.post(
            "/command-center/api/generate-link",
            json={"tenant_id": "desktop-demo"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["tenant_id"] == "desktop-demo"
        assert body["url"].startswith("http")
        assert "t=" in body["url"]

    def test_private_tenant_requires_stepup(self, client):
        resp = client.post(
            "/command-center/api/generate-link",
            json={"tenant_id": "ev-personal"},
        )
        assert resp.status_code == 401

    def test_private_tenant_with_valid_stepup(self, client, tenant_id, step_up_token):
        resp = client.post(
            "/command-center/api/generate-link",
            json={"tenant_id": tenant_id, "agent_id": "sally"},
            headers={"X-Step-Up-Token": step_up_token},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["tenant_id"] == tenant_id
        assert body["expires_in_hours"] == 24

        # The minted token should verify + let us in
        from r6.command_center import access
        payload = access.verify_access_token(body["token"])
        assert payload is not None
        assert payload["tenant_id"] == tenant_id
        assert payload["agent_id"] == "sally"

    def test_private_tenant_with_bad_stepup_rejected(self, client):
        resp = client.post(
            "/command-center/api/generate-link",
            json={"tenant_id": "ev-personal"},
            headers={"X-Step-Up-Token": "not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_missing_tenant_id_rejected(self, client):
        resp = client.post("/command-center/api/generate-link", json={})
        assert resp.status_code == 400
