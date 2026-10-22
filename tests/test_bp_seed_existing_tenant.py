"""Re-seeding a tenant seeded before the BP history (QA on #1021, F1).

On such a tenant `demo-obs-bp` is already on file with its old date and is
skipped, while the new history rows are inserted. Dated from seed time,
they landed after it: 131/83 became the latest reading, and care gaps'
"last on" moved with it. The history is anchored to the stored reading
instead, so it stays strictly older and the latest reading stays the one
the tenant already had.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from models import db
from r6.models import R6Resource
from r6.seed import _built_in_resources, seed_demo_data

_TENANT = "bp-seed-existing-tenant"
_FMT = "%Y-%m-%dT%H:%M:%SZ"
_DAYS_AGO = 40


def _older_seed(days: int) -> list[dict]:
    """The built-in set as main seeded it `days` ago: no BP history."""
    out = []
    for res in _built_in_resources():
        if str(res.get("id", "")).startswith("demo-obs-bp-"):
            continue
        res = json.loads(json.dumps(res))
        if "effectiveDateTime" in res:
            when = datetime.strptime(res["effectiveDateTime"], _FMT)
            res["effectiveDateTime"] = (when - timedelta(days=days)).strftime(
                _FMT)
        out.append(res)
    return out


def _bp_dates(tenant: str) -> dict[str, str]:
    out = {}
    for row in R6Resource.query.filter_by(tenant_id=tenant,
                                          resource_type="Observation").all():
        if row.id == "demo-obs-bp" or row.id.startswith("demo-obs-bp-"):
            out[row.id] = row.to_fhir_json()["effectiveDateTime"]
    return out


@pytest.fixture
def clean(app):
    with app.app_context():
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()
        yield
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()


def test_reseeding_an_older_tenant_keeps_its_latest_bp_reading(app, clean):
    """MUTATION: anchor the history to now regardless -> red."""
    with app.app_context():
        seed_demo_data(_TENANT, resources=_older_seed(_DAYS_AGO),
                       source="sample")
        before = _bp_dates(_TENANT)
        assert set(before) == {"demo-obs-bp"}

        seed_demo_data(_TENANT)
        after = _bp_dates(_TENANT)

    assert after["demo-obs-bp"] == before["demo-obs-bp"]
    history = {k: v for k, v in after.items() if k != "demo-obs-bp"}
    assert len(history) == 6
    # ISO-8601 Z strings of one format order as text.
    assert max(history.values()) < after["demo-obs-bp"]


def test_a_fresh_tenant_dates_the_history_before_seeding(app, clean):
    with app.app_context():
        seed_demo_data(_TENANT)
        dates = _bp_dates(_TENANT)
    history = [v for k, v in dates.items() if k != "demo-obs-bp"]
    assert len(history) == 6
    assert max(history) < dates["demo-obs-bp"]
