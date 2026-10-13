"""QA #945: mid-import, the first answer does not say records are arriving.

Seen live (QA walk, 2026-10-13, synthetic export streamed slowly, labs
first): the person switched to the new connection as soon as the hub
offered it, opened the chat and asked "show me summary". At that moment
the engine held 39 lab results and no conditions, medications or
allergies; those were still streaming in.

The chat page settles a `pending` connection to `active` once the page's
counts are READY (careagents/app.py, the `pending and intake.state ==
READY` branch), and one lab result is enough for READY. The run worker
adds ARRIVING_STYLE only for `pending` (careagents/worker.py,
`arriving=`), so the turn ran without it. The summary's empty-lists note
then took the "records are on file" branch, which says nothing about
loading. The answer was honest ("came back empty ... doesn't mean none
exist") but never said the records were still arriving.

Closed by keying `arriving` on the engine's Fasten import state (#940)
rather than the stored status, failing closed when the engine cannot say;
the records-on-file note then says more may still be loading. The strict
xfail was removed with the fix. The scenario is a Fasten import, so the
Chain's connection is made a consented Fasten connection with a job still
ingesting.
"""

from __future__ import annotations

from careagents import personas
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import (  # noqa: F401  (pytest fixtures)
    _login, cfg, svc)
from tests.test_careagents_first_answer import _as_fasten, _first_turn


def test_labs_landed_first_the_first_answer_still_says_records_are_arriving(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _as_fasten(svc)
    from models import db
    from r6.fasten.models import FastenJob
    from r6.models import R6Resource
    with chain.engine_app.app_context():
        # Labs have landed; the problem list, medications and allergies
        # have not yet.
        R6Resource.query.filter(
            R6Resource.tenant_id == TENANT,
            R6Resource.resource_type.in_((
                "Condition", "MedicationRequest", "MedicationStatement",
                "AllergyIntolerance"))).delete(synchronize_session=False)
        # The engine's job is still writing them.
        db.session.add(FastenJob(task_id="qa945-1", org_connection_id="oc-1",
                                 tenant_id=TENANT, status="ingesting"))
        db.session.commit()
        assert R6Resource.query.filter_by(
            tenant_id=TENANT, resource_type="Observation").count() > 0
    svc.set_connection_status(TENANT, "pending")

    # The person opens the chat page, as anyone does before typing.
    c = chain.app.test_client()
    _login(c, chain.svc, monkeypatch, email="acceptance@example.com")
    assert c.get(f"/chat?agent={chain.agent}").status_code == 200

    out, system = _first_turn(chain, monkeypatch)
    assert all(out[k] == [] for k in ("conditions", "medications",
                                      "medication_statements", "allergies"))
    says_arriving = (personas.ARRIVING_STYLE in system
                     or "loading" in out.get("note", "").lower())
    assert says_arriving, (
        "a first answer mid-import was given no sign the records are still "
        "arriving")
