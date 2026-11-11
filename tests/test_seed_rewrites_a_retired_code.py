"""Re-seeding a tenant seeded before #1119 corrects its metformin row.

The seed used RxNorm 860975 (the extended-release tablet) for plain
metformin 500 mg. A row that exists is skipped on re-seed, so a tenant
seeded earlier would keep 860975 and, once the label table told the truth
about that code, read "extended release" for a record that never was.

MUTATION: make `_carries_a_retired_seed_code` return False and
test_reseeding_rewrites_the_old_metformin_code fails (the row keeps 860975).
"""
from __future__ import annotations

import json

import pytest

from models import db
from r6.models import AuditEventRecord, R6Resource
from r6.seed import _built_in_resources, seed_demo_data

_TENANT = "seed-retired-code-tenant"
_OLD, _NEW = "860975", "861007"


def _seed_as_before() -> list[dict]:
    """The built-in set as it was before the correction."""
    return json.loads(json.dumps(_built_in_resources()).replace(_NEW, _OLD))


def _metformin(tenant: str) -> R6Resource:
    return R6Resource.query.filter_by(
        tenant_id=tenant, resource_type="MedicationRequest",
        id="demo-medreq-metformin").one()


@pytest.fixture
def clean(app):
    # Audit rows are append-only, so only the records are cleared.
    with app.app_context():
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()
        yield
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()


def test_reseeding_rewrites_the_old_metformin_code(app, clean):
    with app.app_context():
        seed_demo_data(_TENANT, _seed_as_before())
        assert _OLD in _metformin(_TENANT).resource_json  # the old state

        seed_demo_data(_TENANT)

        row = _metformin(_TENANT)
        assert _NEW in row.resource_json and _OLD not in row.resource_json
        assert R6Resource.query.filter_by(
            tenant_id=_TENANT, resource_type="MedicationRequest").count() == 1


def test_the_rewrite_is_audited_without_the_record_content(app, clean):
    with app.app_context():
        seed_demo_data(_TENANT, _seed_as_before())
        seed_demo_data(_TENANT)

        rows = AuditEventRecord.query.filter_by(
            tenant_id=_TENANT, resource_id="demo-medreq-metformin").all()
        details = [r.detail or "" for r in rows]
        assert any("corrected medication code" in d for d in details), details
        assert not any("etformin" in d for d in details), details


def test_a_current_seed_is_still_skipped(app, clean):
    with app.app_context():
        seed_demo_data(_TENANT)
        before = _metformin(_TENANT).resource_json
        assert seed_demo_data(_TENANT) == 0
        assert _metformin(_TENANT).resource_json == before
