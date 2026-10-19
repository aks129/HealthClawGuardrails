"""Security probe for #912: retry_job auth, enumeration, audit fail-closed.

Synthetic tenant, connection and task ids only.
"""

from unittest.mock import patch

import pytest

from models import db
from r6.models import AuditEventRecord
from r6.stepup import generate_step_up_token
from tests.test_fasten_retry_auth import _failed_job, _retry, _status

OWNER = "retry-probe-owner"
OTHER = "retry-probe-other"


def test_no_token_answers_the_same_for_a_real_and_a_missing_job(client):
    _failed_job(client, OWNER, "oc-probe-1", "task-probe-1")
    real, _ = _retry(client, "task-probe-1", {"X-Tenant-Id": OWNER})
    ghost, _ = _retry(client, "task-probe-nope", {"X-Tenant-Id": OWNER})
    assert real.status_code == ghost.status_code == 401
    assert real.get_json() == ghost.get_json()


def test_a_cross_tenant_token_answers_the_same_for_real_and_missing(client):
    _failed_job(client, OWNER, "oc-probe-2", "task-probe-2")
    h = {"X-Tenant-Id": OWNER,
         "X-Step-Up-Token": generate_step_up_token(OTHER)}
    real, _ = _retry(client, "task-probe-2", h)
    ghost, _ = _retry(client, "task-probe-ghost", h)
    assert real.status_code == ghost.status_code == 401
    assert real.get_json() == ghost.get_json()


@pytest.mark.parametrize("bad", ["", "Bearer x", "not.a.token",
                                 "eyJ.eyJ.sig"])
def test_junk_tokens_are_refused(client, bad):
    _failed_job(client, OWNER, f"oc-probe-j{len(bad)}", f"task-probe-j{len(bad)}")
    r, thread = _retry(client, f"task-probe-j{len(bad)}",
                       {"X-Tenant-Id": OWNER, "X-Step-Up-Token": bad})
    assert r.status_code == 401
    assert not thread.called


def test_a_bearer_token_alone_is_not_accepted(client):
    _failed_job(client, OWNER, "oc-probe-3", "task-probe-3")
    r, thread = _retry(client, "task-probe-3", {
        "X-Tenant-Id": OWNER,
        "Authorization": "Bearer " + generate_step_up_token(OWNER)})
    assert r.status_code == 401
    assert not thread.called


def test_an_audit_write_failure_blocks_the_retry(client):
    """V2: if the success audit cannot be written, nothing is launched and
    the job is not flipped to pending."""
    _failed_job(client, OWNER, "oc-probe-4", "task-probe-4")
    with patch("r6.fasten.routes.add_audit_event",
               side_effect=RuntimeError("audit down")), \
            patch("r6.fasten.routes.threading.Thread") as thread:
        try:
            r = client.post("/fasten/jobs/task-probe-4/retry", headers={
                "X-Tenant-Id": OWNER,
                "X-Step-Up-Token": generate_step_up_token(OWNER)})
            assert r.status_code >= 500
        except RuntimeError:
            pass
        launched = thread.called
    db.session.rollback()
    assert not launched
    assert _status("task-probe-4") == "failed"


def test_the_audit_rows_carry_no_task_id_links_or_token(client):
    _failed_job(client, OWNER, "oc-probe-5", "task-probe-5")
    tok = generate_step_up_token(OWNER)
    before = {r.id for r in AuditEventRecord.query.all()}
    _retry(client, "task-probe-5", {"X-Tenant-Id": OWNER})  # refusal row
    _retry(client, "task-probe-5", {"X-Tenant-Id": OWNER,
                                    "X-Step-Up-Token": tok})
    new = [r for r in AuditEventRecord.query.all() if r.id not in before]
    kinds = sorted(r.event_type for r in new)
    assert "fasten_import_retried" in kinds and len(new) == 2, kinds
    for row in new:
        blob = " ".join(str(getattr(row, c.name))
                        for c in row.__table__.columns)
        assert "task-probe-5" not in blob
        assert "example.com" not in blob
        assert tok.split(".")[-1] not in blob


def test_a_replayed_write_token_relaunches(client):
    """Documents behaviour: the token is not single-use (consume_nonce is
    False, as at 15 of 17 require_grant sites), and a pending job can be
    retried again, so one token launches two concurrent ingests."""
    _failed_job(client, OWNER, "oc-probe-6", "task-probe-6")
    h = {"X-Tenant-Id": OWNER,
         "X-Step-Up-Token": generate_step_up_token(OWNER)}
    first, t1 = _retry(client, "task-probe-6", h)
    second, t2 = _retry(client, "task-probe-6", h)
    assert first.status_code == 202
    assert second.status_code == 202 and t2.called
