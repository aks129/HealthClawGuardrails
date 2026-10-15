"""Security exploit test for the SMART Health Check-in test patient (#959).

The HTEST tag sits only on the fixture's outer Bundle. The wallet (#960,
careagents/static/checkin_answer.js buildResponse) sends new collection
Bundles built from the entries, and seed_demo_tenant.py stores the entries,
so the machine-readable "this is test data" marker is lost exactly where the
records leave the file. Each resource must carry it on its own.
"""

import json
from pathlib import Path

FIXTURE = (Path(__file__).resolve().parent.parent / "careagents" / "checkin"
           / "us_core_test_patient.json")


def test_every_resource_is_tagged_htest_not_only_the_bundle():
    bundle = json.loads(FIXTURE.read_text())
    untagged = [
        e["fullUrl"] for e in bundle["entry"]
        if "HTEST" not in {t.get("code") for t in
                           (e["resource"].get("meta") or {}).get("tag", [])}
    ]
    assert untagged == []
