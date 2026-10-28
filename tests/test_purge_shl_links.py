""""Delete my records" reaches every tenant-keyed table. Synthetic data only.

1. Drift guard. Every table the migrations create with a `tenant_id` column
   is either emptied by `purge_tenant` or on the commented exemption list
   below. The purged set is observed, not read from the purge's own list:
   the purge runs against a migrated database and the test records which
   tables it issued a DELETE against, so a table the purge never names
   cannot pass by being missing from both sides.

2. A SMART Health Link issued before the purge stops serving after it.
   `shl_links` holds the patient's encrypted bundle under the tenant id
   (r6/shl_links.py). It used to survive the purge, so a link shared before
   account deletion kept answering, within its TTL and use count, after the
   purge had reported deleted:true.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from models import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECRET = "purge-shl-secret"
TENANT = "purge-shl-tenant"

#: Tenant-keyed tables a purge keeps, each on purpose.
EXEMPT = {
    # The PHI-free audit trail is the immutable record of prior access; a
    # deletion on request must not erase it (r6/purge.py docstring), and the
    # purge itself is audited into it.
    "audit_events",
    # The tombstone the purge WRITES (tenant id and time only), which stops a
    # late import from refilling the emptied tenant.
    "tenant_closures",
}

_PROBE = r"""
import json, os, re
from sqlalchemy import create_engine, event, inspect
from main import create_app
from models import db
from r6.database_migrations import upgrade_database
from r6.purge import purge_tenant
app = create_app({"TESTING": True,
                  "SQLALCHEMY_DATABASE_URI": os.environ["PROBE_DB"],
                  "LEGACY_BOOT_ON_CREATE": False})
with app.app_context():
    upgrade_database(db.engine)            # what `flask init-db` runs
    insp = inspect(db.engine)
    keyed = sorted(t for t in insp.get_table_names()
                   if any(c["name"] == "tenant_id" for c in insp.get_columns(t)))
    deleted_from = set()

    def watch(conn, cursor, statement, params, context, executemany):
        m = re.match(r"\s*DELETE FROM (\w+)", statement, re.I)
        if m:
            deleted_from.add(m.group(1))

    event.listen(db.engine, "before_cursor_execute", watch)
    purge_tenant(os.environ["PROBE_TENANT"])
    db.session.rollback()
print(json.dumps({"keyed": keyed, "deleted_from": sorted(deleted_from)}))
"""


def test_every_tenant_keyed_table_is_purged_or_exempt(tmp_path):
    env = os.environ.copy()
    env.update(PROBE_DB=f"sqlite:///{tmp_path / 'drift.db'}",
               PROBE_TENANT=TENANT, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT,
                            env=env, text=True, capture_output=True,
                            timeout=180, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])

    left_behind = set(out["keyed"]) - set(out["deleted_from"]) - EXEMPT
    assert not left_behind, (
        "tenant-keyed tables a purge leaves behind: "
        f"{sorted(left_behind)}. Purge them in r6/purge.py, or add them to "
        "EXEMPT here with the reason they must survive a deletion.")
    # An exemption that stops existing, or starts being purged, is stale.
    assert EXEMPT <= set(out["keyed"]) - set(out["deleted_from"])


def test_a_shared_health_link_stops_working_after_delete_my_records(
        client, monkeypatch):
    from r6 import shl_links

    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://engine.example.test")
    # More than one use, so the fetch before the purge leaves the link live.
    monkeypatch.setenv("SHL_LINK_MAX_USES", "5")
    link = shl_links.issue(TENANT, b'{"resourceType":"Bundle","synthetic":1}',
                           "synthetic share")
    db.session.commit()
    assert client.get(f"/shl/{link['link_id']}").status_code == 200

    resp = client.post("/r6/fhir/internal/purge-tenant",
                       json={"tenant_id": TENANT},
                       headers={"X-Internal-Secret": SECRET})
    assert resp.status_code == 200 and resp.get_json()["deleted"] is True

    after = client.get(f"/shl/{link['link_id']}")
    assert after.status_code == 404, (
        f"the link still served {len(after.data)} bytes of the patient's "
        "encrypted bundle after the purge reported deleted:true")
    assert resp.get_json()["detail"]["shl_links"] == 1
