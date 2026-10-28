"""
tests/test_sources_summary.py

Coverage for the Dev Days "check every connected source" endpoint:

    GET /command-center/api/sources-summary

Mirrors the auth/tenant model of the existing /api/sources tests in
test_command_center.py: `test-tenant` is public in the test env (set in
conftest via PUBLIC_TENANTS), so a bare ?tenant=test-tenant is accepted.
"""

from datetime import datetime, timezone


from models import db
from r6.models import R6Resource, AuditEventRecord
from r6.command_center import projector
import pytest

pytestmark = pytest.mark.extension

TENANT = "test-tenant"
OTHER_TENANT = "other-tenant"

# The 7 canonical source ids the summary must always report.
EXPECTED_SOURCE_IDS = {
    "fasten", "healthex", "hbo", "medent",
    "flexpa", "healthskillz", "wearables",
}


def _seed_resource(resource_type: str, tenant: str = TENANT):
    db.session.add(R6Resource(
        resource_type=resource_type,
        resource_json="{}",
        tenant_id=tenant,
    ))


# ---------------------------------------------------------------------------
# Projector — sources_summary()
# ---------------------------------------------------------------------------

class TestSourcesSummaryProjector:

    def test_reports_all_seven_sources(self, app):
        with app.app_context():
            out = projector.sources_summary(TENANT)
            ids = {s["id"] for s in out["sources"]}
            assert ids == EXPECTED_SOURCE_IDS
            assert out["source_count"] == 7
            assert out["tenant"] == TENANT
            assert "generated_at" in out

    def test_records_by_type_and_total(self, app):
        with app.app_context():
            for _ in range(3):
                _seed_resource("Condition")
            for _ in range(2):
                _seed_resource("Observation")
            _seed_resource("Immunization")
            db.session.commit()

            out = projector.sources_summary(TENANT)
            assert out["total_records"] == 6
            by_type = {r["type"]: r["count"] for r in out["records_by_type"]}
            assert by_type == {"Condition": 3, "Observation": 2, "Immunization": 1}

    def test_deleted_resources_excluded(self, app):
        with app.app_context():
            r = R6Resource(resource_type="Condition", resource_json="{}",
                           tenant_id=TENANT)
            r.is_deleted = True
            db.session.add(r)
            _seed_resource("Observation")
            db.session.commit()

            out = projector.sources_summary(TENANT)
            assert out["total_records"] == 1
            by_type = {r["type"]: r["count"] for r in out["records_by_type"]}
            assert by_type == {"Observation": 1}

    def test_tenant_isolation(self, app):
        with app.app_context():
            _seed_resource("Condition", tenant=OTHER_TENANT)
            _seed_resource("Condition", tenant=OTHER_TENANT)
            _seed_resource("Observation", tenant=TENANT)
            db.session.commit()

            out = projector.sources_summary(TENANT)
            assert out["total_records"] == 1
            assert {r["type"] for r in out["records_by_type"]} == {"Observation"}

    def test_audit_signal_marks_hbo_connected(self, app):
        with app.app_context():
            db.session.add(AuditEventRecord(
                event_type="read",
                resource_type="Condition",
                tenant_id=TENANT,
                detail="pulled from hbo upstream",
                recorded=datetime.now(timezone.utc),
            ))
            db.session.commit()

            out = projector.sources_summary(TENANT)
            sources = {s["id"]: s for s in out["sources"]}
            assert sources["hbo"]["connected"] is True
            assert sources["hbo"]["last_activity"] is not None
            # A source with no signal stays disconnected.
            assert sources["flexpa"]["connected"] is False
            assert out["connected_count"] >= 1

    def test_audit_signal_matches_agent_id(self, app):
        with app.app_context():
            db.session.add(AuditEventRecord(
                event_type="read",
                tenant_id=TENANT,
                agent_id="medent-export-agent",
                detail="generic export",
                recorded=datetime.now(timezone.utc),
            ))
            db.session.commit()

            out = projector.sources_summary(TENANT)
            sources = {s["id"]: s for s in out["sources"]}
            assert sources["medent"]["connected"] is True

    def test_audit_signal_is_tenant_isolated(self, app):
        with app.app_context():
            db.session.add(AuditEventRecord(
                event_type="read",
                tenant_id=OTHER_TENANT,
                detail="flexpa pull",
                recorded=datetime.now(timezone.utc),
            ))
            db.session.commit()

            out = projector.sources_summary(TENANT)
            sources = {s["id"]: s for s in out["sources"]}
            assert sources["flexpa"]["connected"] is False


# ---------------------------------------------------------------------------
# Route — GET /command-center/api/sources-summary
# ---------------------------------------------------------------------------

class TestSourcesSummaryRoute:

    def test_api_returns_all_seven(self, client):
        resp = client.get(
            "/command-center/api/sources-summary",
            query_string={"tenant": TENANT},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        ids = {s["id"] for s in body["sources"]}
        assert ids == EXPECTED_SOURCE_IDS
        assert body["source_count"] == 7
        assert body["tenant"] == TENANT

    def test_api_reflects_seeded_records(self, app, client):
        with app.app_context():
            for _ in range(5):
                _seed_resource("Condition")
            db.session.commit()

        resp = client.get(
            "/command-center/api/sources-summary",
            query_string={"tenant": TENANT},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total_records"] == 5
        by_type = {r["type"]: r["count"] for r in body["records_by_type"]}
        assert by_type == {"Condition": 5}

    def test_api_requires_auth_for_nonpublic_tenant(self, client):
        resp = client.get(
            "/command-center/api/sources-summary",
            query_string={"tenant": "private-no-auth-tenant"},
        )
        assert resp.status_code == 401

    def test_api_allows_nonpublic_tenant_with_step_up(self, app, client):
        from r6.stepup import generate_step_up_token
        tenant = "private-no-auth-tenant"
        with app.app_context():
            _seed_resource("Observation", tenant=tenant)
            db.session.commit()
        token = generate_step_up_token(tenant)
        resp = client.get(
            "/command-center/api/sources-summary",
            query_string={"tenant": tenant},
            headers={"X-Tenant-Id": tenant, "X-Step-Up-Token": token},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["tenant"] == tenant
        assert body["total_records"] == 1
