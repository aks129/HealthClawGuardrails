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


from models import db
from r6.models import R6Resource, AuditEventRecord
from r6.command_center import agents, projector, gateway
from r6.command_center.models import Conversation, ConversationMessage

TENANT = "test-tenant"


def _login_client(client, tenant: str = TENANT):
    """Log the test client in by exchanging a signed token for a session."""
    from r6.command_center import access
    token = access.generate_access_token(tenant)
    client.get("/command-center", query_string={"t": token}, follow_redirects=False)
    return client


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

class TestAgentRegistry:

    def test_loads_seven_agents(self):
        agents.load_agents.cache_clear()
        items = agents.load_agents()
        ids = {a["id"] for a in items}
        assert {"sally", "mary", "dom", "kristy", "shervin", "ronny", "joe"} <= ids

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
            db.session.add(Conversation(
                id="careagents:sally",
                tenant_id=TENANT,
                agent_id="sally",
                created_by_surface="telegram",
            ))
            db.session.add(ConversationMessage(
                tenant_id=TENANT,
                conversation_id="careagents:sally",
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
        assert len(body) == 7
        assert {a["id"] for a in body} == {
            "sally", "mary", "dom", "kristy", "shervin", "ronny", "joe"
        }

    def test_api_agent_templates(self, client):
        resp = client.get("/command-center/api/agent-templates")
        assert resp.status_code == 200
        body = resp.get_json()
        template_ids = {t["id"] for t in body["templates"]}
        assert "pcp-advisor" in template_ids
        assert len(body["bundles"]) >= 3

    def test_api_openclaw_sessions_requires_auth(self, client, monkeypatch):
        monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
        resp = client.get("/command-center/api/openclaw/sessions")
        assert resp.status_code == 401

    def test_api_openclaw_sessions_with_session(self, client, monkeypatch):
        monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
        _login_client(client)
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

    def test_api_system_requires_auth(self, client):
        resp = client.get("/command-center/api/system")
        assert resp.status_code == 401

    def test_api_system_with_session(self, client):
        _login_client(client)
        resp = client.get("/command-center/api/system")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["flask"]["up"] is True

    # The token branch of _require_session_or_stepup, pinned before kernel
    # slice 18 moves it. The pair above covers header-absent and session; a
    # predicate that stopped validating and merely checked for the header's
    # presence stayed green against them. Four facts: a token for the named
    # tenant is enough, a garbage token is not, another tenant's well-formed
    # token is not, and with no tenant named the token binds to the
    # blueprint's default tenant, not to whatever the token says.
    #
    # MUTATION (pre-kernel shape): `if valid: return None` -> `return None`
    # (presence check) -> the malformed, other-tenant and default-binding
    # tests go red while the valid-token test stays green. Executed
    # 2026-09-06. Kernel shape: `has_grant(...) is not None` -> `True`
    # -> the same three red. Executed 2026-09-06.

    def test_api_system_with_a_step_up_token_for_the_named_tenant(self, client):
        from r6.stepup import generate_step_up_token
        resp = client.get("/command-center/api/system", headers={
            "X-Step-Up-Token": generate_step_up_token(TENANT),
            "X-Tenant-Id": TENANT,
        })
        assert resp.status_code == 200
        assert resp.get_json()["flask"]["up"] is True

    def test_api_system_refuses_a_malformed_step_up_token(self, client):
        resp = client.get("/command-center/api/system", headers={
            "X-Step-Up-Token": "not-a-real-token",
            "X-Tenant-Id": TENANT,
        })
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "authentication required"}

    def test_api_system_refuses_another_tenants_step_up_token(self, client):
        from r6.stepup import generate_step_up_token
        resp = client.get("/command-center/api/system", headers={
            "X-Step-Up-Token": generate_step_up_token("someone-else"),
            "X-Tenant-Id": TENANT,
        })
        assert resp.status_code == 401

    def test_api_system_step_up_binds_to_the_default_tenant_when_none_is_named(
            self, client):
        from r6.command_center.routes import DEFAULT_TENANT
        from r6.stepup import generate_step_up_token
        # No ?tenant= and no X-Tenant-Id: the token must be the default
        # tenant's. One for TENANT is refused, one for the default is enough.
        refused = client.get("/command-center/api/system", headers={
            "X-Step-Up-Token": generate_step_up_token(TENANT)})
        assert refused.status_code == 401
        accepted = client.get("/command-center/api/system", headers={
            "X-Step-Up-Token": generate_step_up_token(DEFAULT_TENANT)})
        assert accepted.status_code == 200

    def test_api_conversations_post_and_get(self, client, step_up_token):
        payload = {
            "tenant_id": TENANT,
            "conversation_id": "careagents:sally",
            "request_id": "telegram-update-123",
            "agent_id": "sally",
            "channel": "telegram",
            "session_id": "chat-123",
            "user_id": "tg-456",
            "role": "user",
            "text": "/health",
        }
        resp = client.post(
            "/command-center/api/conversations",
            json=payload,
            headers={"X-Step-Up-Token": step_up_token},
        )
        assert resp.status_code == 201
        created = resp.get_json()
        assert created["tenant_id"] == TENANT
        assert created["agent_id"] == "sally"
        assert created["conversation_id"] == "careagents:sally"
        assert created["request_id"] == "telegram-update-123"

        resp = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": TENANT},
        )
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "/health"
        assert msgs[0]["agent_emoji"] == "🩺"

    def test_api_conversations_replays_duplicate_request_once(
            self, client, step_up_token):
        payload = {
            "tenant_id": TENANT,
            "conversation_id": "careagents:juniper",
            "agent_id": "juniper",
            "surface": "web",
            "request_id": "browser-request-1",
            "role": "user",
            "text": "show my medications",
        }
        headers = {"X-Step-Up-Token": step_up_token}

        first = client.post(
            "/command-center/api/conversations", json=payload, headers=headers)
        replay = client.post(
            "/command-center/api/conversations", json=payload, headers=headers)

        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.get_json()["id"] == first.get_json()["id"]
        assert replay.get_json()["idempotent_replay"] is True
        conflict = client.post(
            "/command-center/api/conversations",
            json={**payload, "text": "different payload"},
            headers=headers,
        )
        assert conflict.status_code == 409
        rows = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": TENANT,
                          "conversation_id": "careagents:juniper"},
        ).get_json()
        assert [row["text"] for row in rows] == ["show my medications"]

    def test_api_conversations_are_isolated_by_thread_and_agent(
            self, client, step_up_token):
        headers = {"X-Step-Up-Token": step_up_token}
        for agent_id, text in (("agent-a", "thread A"),
                               ("agent-b", "thread B")):
            response = client.post(
                "/command-center/api/conversations",
                json={
                    "tenant_id": TENANT,
                    "conversation_id": f"careagents:{agent_id}",
                    "agent_id": agent_id,
                    "role": "user",
                    "text": text,
                },
                headers=headers,
            )
            assert response.status_code == 201

        rows = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": TENANT,
                          "conversation_id": "careagents:agent-a",
                          "agent_id": "agent-a"},
        ).get_json()
        assert [row["text"] for row in rows] == ["thread A"]

        mismatch = client.post(
            "/command-center/api/conversations",
            json={
                "tenant_id": TENANT,
                "conversation_id": "careagents:agent-a",
                "agent_id": "agent-b",
                "role": "user",
                "text": "wrong owner",
            },
            headers=headers,
        )
        assert mismatch.status_code == 409

    def test_conversation_replay_can_stop_at_an_exact_claimed_message(
            self, app, client, step_up_token):
        """A later queued inbound must not enter an earlier run's prompt."""
        from datetime import datetime, timezone
        from models import db
        from r6.command_center.models import ConversationMessage

        headers = {"X-Step-Up-Token": step_up_token}
        base = {"tenant_id": TENANT,
                "conversation_id": "careagents:bounded",
                "agent_id": "bounded", "surface": "web", "role": "user"}
        first = client.post(
            "/command-center/api/conversations",
            json={**base, "request_id": "bounded-1", "text": "first"},
            headers=headers).get_json()
        later = client.post(
            "/command-center/api/conversations",
            json={**base, "request_id": "bounded-2", "text": "later"},
            headers=headers).get_json()
        # Even a timestamp collision is fail-closed: only the exact anchor and
        # strictly earlier timestamps are eligible.
        with app.app_context():
            same = datetime.now(timezone.utc)
            db.session.get(ConversationMessage, first["id"]).created_at = same
            db.session.get(ConversationMessage, later["id"]).created_at = same
            db.session.commit()

        rows = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": TENANT,
                          "conversation_id": "careagents:bounded",
                          "agent_id": "bounded",
                          "through_message_id": first["id"],
                          "full": "1"},
            headers=headers).get_json()

        assert [row["text"] for row in rows] == ["first"]

    def test_same_external_conversation_id_is_tenant_scoped(self, client):
        from r6.stepup import generate_step_up_token

        for tenant, text in (("tenant-a", "A only"),
                             ("tenant-b", "B only")):
            token = generate_step_up_token(tenant)
            response = client.post(
                "/command-center/api/conversations",
                json={
                    "tenant_id": tenant,
                    "conversation_id": "external-thread-1",
                    "agent_id": "external-agent",
                    "role": "user",
                    "text": text,
                },
                headers={"X-Step-Up-Token": token},
            )
            assert response.status_code == 201

        token = generate_step_up_token("tenant-a")
        rows = client.get(
            "/command-center/api/conversations",
            query_string={"tenant": "tenant-a",
                          "conversation_id": "external-thread-1"},
            headers={"X-Step-Up-Token": token},
        ).get_json()
        assert [row["text"] for row in rows] == ["A only"]

    def test_api_conversations_post_rejects_missing_fields(self, client):
        resp = client.post(
            "/command-center/api/conversations",
            json={"tenant_id": TENANT, "role": "user"},  # missing text
        )
        assert resp.status_code == 400

    def test_api_conversations_accepts_opaque_agent_identity(
            self, client, step_up_token):
        resp = client.post(
            "/command-center/api/conversations",
            json={
                "tenant_id": TENANT,
                "role": "user",
                "text": "hi",
                "agent_id": "bogus",
            },
            headers={"X-Step-Up-Token": step_up_token},
        )
        assert resp.status_code == 201
        assert resp.get_json()["agent_id"] == "bogus"

    def test_api_conversations_post_rejects_without_auth(self, client):
        resp = client.post(
            "/command-center/api/conversations",
            json={
                "tenant_id": TENANT,
                "role": "user",
                "text": "hi",
            },
        )
        assert resp.status_code == 401

    def test_api_tasks_create_list_and_update(self, client, step_up_token):
        headers = {"X-Step-Up-Token": step_up_token}
        # Create
        create = client.post("/command-center/api/tasks", json={
            "tenant_id": TENANT,
            "agent_id": "joe",
            "title": "Approve ICD-9 fix",
            "priority": "high",
            "source": "curatr",
        }, headers=headers)
        assert create.status_code == 201
        task = create.get_json()
        assert task["status"] == "pending"
        task_id = task["id"]

        # List
        listed = client.get("/command-center/api/tasks", query_string={"tenant": TENANT})
        assert listed.status_code == 200
        assert any(t["id"] == task_id for t in listed.get_json())

        # Update to completed (also requires step-up)
        updated = client.patch(
            f"/command-center/api/tasks/{task_id}",
            json={"status": "completed"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.get_json()["status"] == "completed"

        # Listing pending should no longer include it
        listed2 = client.get("/command-center/api/tasks", query_string={"tenant": TENANT})
        assert not any(t["id"] == task_id for t in listed2.get_json())

    def test_api_tasks_update_rejects_bad_status(self, client, step_up_token):
        headers = {"X-Step-Up-Token": step_up_token}
        create = client.post("/command-center/api/tasks", json={
            "tenant_id": TENANT,
            "agent_id": "sally",
            "title": "X",
        }, headers=headers)
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
        token = access.generate_access_token("test-tenant", agent_id="sally")
        payload = access.verify_access_token(token)
        assert payload is not None
        assert payload["tenant_id"] == "test-tenant"
        assert payload["agent_id"] == "sally"

    def test_bad_token_returns_none(self):
        from r6.command_center import access
        assert access.verify_access_token("not-a-real-token") is None
        assert access.verify_access_token("") is None

    def test_build_dashboard_url_format(self):
        from r6.command_center import access
        url = access.build_dashboard_url(
            "https://healthclaw.io", "test-tenant", agent_id="h"
        )
        assert url.startswith("https://healthclaw.io/command-center?tenant=test-tenant&t=")


class TestSignedLinkFlow:

    def test_private_tenant_requires_session(self, client):
        resp = client.get("/command-center", query_string={"tenant": "private-tenant"})
        assert resp.status_code == 401
        assert b"Your personal health command center" in resp.data

    def test_public_tenant_no_auth_required(self, client):
        resp = client.get("/command-center", query_string={"tenant": "desktop-demo"})
        assert resp.status_code == 200

    def test_valid_signed_link_logs_in_and_redirects(self, client):
        from r6.command_center import access
        token = access.generate_access_token("test-tenant")
        resp = client.get("/command-center", query_string={"t": token})
        assert resp.status_code == 302
        assert "test-tenant" in resp.headers["Location"]

        # Session is now sticky — follow-up request works
        resp2 = client.get("/command-center", query_string={"tenant": "test-tenant"})
        assert resp2.status_code == 200

    def test_expired_token_shows_error(self, client):
        resp = client.get("/command-center", query_string={"t": "garbage.token.here"})
        assert resp.status_code == 401
        assert b"expired or is invalid" in resp.data

    def test_login_page_renders(self, client):
        resp = client.get("/command-center/login")
        assert resp.status_code == 200
        # The page must name a way to actually get a link. This asserted
        # b"/dashboard" — the bot slash command that was the only documented
        # route in, and could not be followed (#564).
        assert b"support@healthclaw.io" in resp.data

    def test_login_page_sends_nobody_to_an_unserved_surface(self, client):
        """Both places this page states how to get in, in one test.

        It told a private-tenant user to open their Telegram bot and send
        /dashboard for a signed link, and the invalid-link 401 told them to
        ask that agent for a fresh one. The surface is not served (council
        ruling D6), so the only documented way in could not be followed and
        the dashboard was unreachable while it is down (#564).

        The 401 render is the second location of the one claim, and the
        second location is where copy like this survives a fix.

        MUTATION: restore "Ask your Telegram agent for a fresh one." in
        r6/command_center/routes.py, or the "Open your HealthClaw Telegram
        bot" step in templates/command_center_login.html -> reddens.
        Verified 2026-09-04.
        """
        page = client.get("/command-center/login")
        expired = client.get("/command-center", query_string={"t": "garbage"})
        assert expired.status_code == 401
        for resp in (page, expired):
            assert b"Telegram" not in resp.data, (
                "the command center login page routes a user through a "
                "surface that is not served")

    def test_logout_clears_session(self, client):
        from r6.command_center import access
        token = access.generate_access_token("private-tenant")
        client.get("/command-center", query_string={"t": token})  # login

        client.get("/command-center/logout")
        # Private tenant should now require auth again
        resp = client.get("/command-center", query_string={"tenant": "private-tenant"})
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
            json={"tenant_id": "private-tenant"},
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
            json={"tenant_id": "private-tenant"},
            headers={"X-Step-Up-Token": "not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_missing_tenant_id_rejected(self, client):
        resp = client.post("/command-center/api/generate-link", json={})
        assert resp.status_code == 400
