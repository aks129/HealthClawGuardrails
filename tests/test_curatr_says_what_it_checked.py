"""/curatr must say what it checked, not what it did not (#458).

Found by the physician advisor rehearsing the launch demo: /curatr answered
"quality: good (score: 1), no data quality issues found" on a tenant holding
a dozen duplicate Type 2 diabetes Conditions. Both halves of the reply were
true of what ran and false of what they claimed. `cmd_curatr` fetches ONE
Observation (`_count: 1`) and `CuratrEngine.evaluate` grades that single
resource; duplication is a property of a set, so no single-resource pass can
ever see it. "No data quality issues found" was a verdict on the record
printed by a check that had examined one row of it.

This is the repo's recurring shape (docs/2026-08-02-retro.md): a check that
examined one thing printing the verdict of a check that examined everything.

Council ruling D14: `/curatr` means "evaluate my record". Until a set-level
evaluator exists (Cohort 2 — deliberately NOT built here), the output must
say what it actually checked. Two strings carry that:

  1. The engine's clean-record summary names the ONE resource it graded and
     says duplicates and other types were not examined. The FHIR facade
     route ($curatr-evaluate) returns `summary` verbatim, so pinning it at
     the engine covers the route too.

     services/agent-orchestrator/src/tools.ts holds a second copy of the
     sentence in `_mcp_summary.note`, which a direct MCP consumer reads
     instead of `summary`. That copy was fixed in the same PR (commit
     9ff27eb) and is pinned in `tools.test.ts`, not here — this file's
     first commit described it as out of scope, which stopped being true
     one commit later.
  2. The bot's headline says "Checked 1 of N <Type>s (most recent)" with N
     from the search Bundle's `total`, and the score sits on the SAME line
     as that scope so "good" can never be read alone. When the Bundle
     carries no total the bot says "count unknown" — it never invents N.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from r6.curatr import CuratrEngine

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token-for-curatr-scope')

sys.path.insert(0, str(Path(__file__).parent.parent / "openclaw"))

import bot  # noqa: E402


# --- the engine names the one resource it graded ----------------------------

def _clean(resource_type: str) -> dict:
    """A resource that produces zero issues once the terminology lookup is
    stubbed valid — the only path that reaches the clean-record summary."""
    common = {
        "id": f"{resource_type.lower()}-clean",
        "subject": {"reference": "Patient/pt-1"},
    }
    if resource_type == "Observation":
        return {
            "resourceType": "Observation",
            "status": "final",
            "code": {"coding": [{
                "system": "http://loinc.org", "code": "2339-0",
                "display": "Glucose",
            }]},
            "effectiveDateTime": "2026-04-20",
            "valueQuantity": {"value": 100, "unit": "mg/dL"},
            **common,
        }
    return {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
            "code": "active",
        }]},
        "verificationStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
            "code": "confirmed",
        }]},
        "code": {"coding": [{
            "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9",
            "display": "Type 2 diabetes mellitus without complications",
        }]},
        "onsetDateTime": "2020-01-01",
        **common,
    }


@pytest.mark.parametrize("resource_type", ["Observation", "Condition"])
def test_clean_record_summary_says_it_checked_one_resource(resource_type):
    """MUTATION: restore "No data quality issues found in this X record." -> red.

    The type in the sentence is the type that was evaluated, not a constant:
    the Condition case fails if the string hard-codes Observation.
    """
    engine = CuratrEngine(timeout=1)
    with patch.object(engine, '_lookup_code',
                      return_value={"valid": True, "display": None, "message": None}):
        result = engine.evaluate(_clean(resource_type))

    assert result.issues == [], "fixture is not clean; the test proves nothing"
    assert result.overall_quality == "good"
    summary = result.summary
    assert "checked one resource" in summary, summary
    assert f"this one {resource_type} record" in summary, summary
    assert "duplicates" in summary and "not examined" in summary, summary
    assert "No data quality issues found" not in summary, (
        f"the record-level verdict is back: {summary!r}")


# --- the bot headline carries the scope beside the score --------------------

def _run(coro):
    return asyncio.run(coro)


def _update():
    return SimpleNamespace(
        effective_message=SimpleNamespace(text="/curatr"),
        effective_chat=SimpleNamespace(id=4243),
        effective_user=SimpleNamespace(username="advisor", id=99),
        message=SimpleNamespace(text="/curatr"),
    )


def _search_bundle(total):
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}],
        # The MCP layer's own rollup, which coerces a missing total to 0
        # (tools.ts searchResources: `result.total as number ?? 0`). Reading N
        # from here instead of the Bundle would print "1 of 0" — a count that
        # looks measured and is not. Present in both cases so the no-total
        # test catches that substitution.
        "_mcp_summary": {"total": total if total is not None else 0},
    }
    if total is not None:
        bundle["total"] = total
    return bundle


_EVALUATE = {
    "resource_type": "Observation",
    "resource_id": "obs-1",
    "overall_quality": "good",
    "summary": (
        "No coding or structural issues found in this one Observation record. "
        "This checked one resource, not your record; duplicates and other "
        "record types were not examined."
    ),
    "issue_count": 0,
    "issues": [],
    "quality_score": 1.0,
}


def _curatr(total):
    """Drive cmd_curatr with the MCP bridge faked; return what was replied."""
    sent = []

    def rpc(tool, **params):
        if tool == 'fhir_search':
            assert params.get('params', {}).get('_count') == 1
            return _search_bundle(total)
        if tool == 'curatr_evaluate':
            return dict(_EVALUATE)
        raise AssertionError(f"unexpected tool {tool}")

    with patch.object(bot, '_reply', side_effect=lambda u, t, a, **k: sent.append(t)), \
         patch.object(bot, '_persist_turn'), \
         patch.object(bot, '_rpc', side_effect=rpc):
        _run(bot.cmd_curatr(_update(), SimpleNamespace(args=[])))
    return sent


def _headline(sent):
    """The reply line that carries the score. There must be exactly one and it
    must also carry the scope — a bare "quality: good (score: 1)" line is the
    defect."""
    body = [ln for msg in sent for ln in msg.splitlines() if 'score' in ln]
    assert len(body) == 1, f"expected one score line, got {body}"
    return body[0]


def test_bot_says_checked_one_of_total_when_the_bundle_has_a_total():
    """MUTATION: drop `total` from the headline -> red.

    N is the Bundle's `total` (tenant-wide count for the type), not the
    number of entries the `_count: 1` page returned.
    """
    sent = _curatr(total=12)
    line = _headline(sent)
    assert 'Checked 1 of 12 Observations (most recent)' in line, line
    assert 'good' in line and 'score: 1.0' in line, (
        f"the score moved off the scope line: {line}")


def test_bot_says_count_unknown_when_the_bundle_has_no_total():
    """MUTATION: fall back to len(entries) for N -> red.

    A missing total is reported as unknown. Inventing N from the one-entry
    page would print "1 of 1" — a false claim that the record holds one
    Observation.
    """
    sent = _curatr(total=None)
    line = _headline(sent)
    assert 'Checked 1 of ? Observations' in line, line
    assert 'count unknown' in line, line
    assert 'of 1 Observations' not in line, f"N was invented: {line}"


def test_bot_passes_the_engine_summary_through():
    """The engine's scope sentence reaches the chat unchanged.

    MUTATION: drop the `lines.append(summary)` branch -> red. The headline
    alone would then be the whole verdict.
    """
    sent = _curatr(total=12)
    body = "\n".join(sent)
    assert "This checked one resource, not your record" in body, body


# --- "(most recent)" is a contract, not a lucky default ---------------------
#
# QA addition (review of PR #555). The bot never sends `_sort`, and its
# `_count: 1` does not survive the MCP bridge: the tool handler reads
# `input._count` while `cmd_curatr` nests it under `arguments.params`, so the
# search runs at the handler's default of 20. Verified live on 2026-09-03
# against a seeded tenant — the bot's exact JSON-RPC payload came back with
# `total: 25` and 20 entries, and `entry[0]` was the newest row.
#
# So "Checked 1 of N ... (most recent)" is true only because an unsorted
# search defaults to `-_lastUpdated` (`r6/routes.py`). Nothing pinned that
# default, which left a patient-facing claim resting on a line any refactor
# could flip. Pin it beside the claim that depends on it.

def _seed_observations(ids):
    import json as _json
    from datetime import datetime, timedelta, timezone as _tz

    from models import db
    from r6.models import R6Resource

    base = datetime(2026, 4, 1, tzinfo=_tz.utc)
    for offset, rid in enumerate(ids):
        row = R6Resource(
            resource_type='Observation',
            resource_json=_json.dumps({
                'resourceType': 'Observation', 'id': rid, 'status': 'final',
                'code': {'coding': [{'system': 'http://loinc.org',
                                     'code': '2339-0'}]},
            }),
            resource_id=rid,
            tenant_id='test-tenant',
        )
        # The model stamps `last_updated` at construction, so every row would
        # otherwise share a timestamp and the ordering under test would be
        # decided by insertion order rather than by the sort.
        row.last_updated = base + timedelta(days=offset)
        db.session.add(row)
    db.session.commit()


def test_search_without_sort_returns_the_most_recent_first(
        app, client, tenant_headers):
    """MUTATION: flip the `_sort` default in `r6/routes.py` to ascending (or
    make the else-branch `.asc()`) -> red.

    The bot's headline calls the graded resource "(most recent)" while
    sending no `_sort` and grading `entry[0]`. That word is true only while
    an unsorted search answers newest-first.
    """
    with app.app_context():
        _seed_observations(['obs-oldest', 'obs-middle', 'obs-newest'])
        resp = client.get('/r6/fhir/Observation', headers=tenant_headers)
        assert resp.status_code == 200
        ids = [e['resource']['id'] for e in resp.get_json().get('entry', [])]
        assert ids and ids[0] == 'obs-newest', (
            "an unsorted search no longer answers newest-first, so the bot's "
            f'"(most recent)" is now false: {ids}')


def test_search_total_is_the_tenant_count_not_the_page_size(
        app, client, tenant_headers):
    """MUTATION: set `'total': len(entries)` in the local search branch of
    `r6/routes.py` -> red.

    The bot prints this number as "Checked 1 of N", a claim about how many
    records of that type the tenant holds. If `total` ever tracked the page
    instead, the headline would under-report the record by exactly the amount
    that makes the caveat pointless — and it would still read as measured.
    """
    with app.app_context():
        _seed_observations([f'obs-t{i:02d}' for i in range(7)])
        resp = client.get('/r6/fhir/Observation?_count=2',
                          headers=tenant_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body.get('entry', [])) == 2
        assert body['total'] == 7, (
            f"total tracked the page, not the tenant: {body['total']}")
