"""Stage 1 holds at most 25 active invites (beta spec section 2).

The invite service itself shipped with #852 and its own tests live in
test_careagents_real_record_invites.py. This file pins the cap.
"""

from __future__ import annotations

import pytest

from careagents import beta
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)


def test_an_invite_is_stored_normalised_and_only_once(svc):  # noqa: F811
    assert svc.invite_real_records(" Tester@Example.COM ", "operator") is True
    assert svc.invite_real_records("tester@example.com", "operator") is False
    assert svc.real_records_invited("TESTER@example.com") is True
    assert len(svc.real_record_invites()) == 1


def test_the_cohort_stops_at_the_cap_counting_active_invites_only(svc):  # noqa: F811
    assert beta.STAGE1_INVITE_CAP == 25
    for i in range(beta.STAGE1_INVITE_CAP):
        svc.invite_real_records(f"t{i}@example.com", "operator")
    with pytest.raises(ValueError, match="full.*25"):
        svc.invite_real_records("one-more@example.com", "operator")
    assert svc.real_records_invited("one-more@example.com") is False
    svc.revoke_real_records_invite("t0@example.com")
    assert svc.invite_real_records("one-more@example.com", "operator") is True


def test_reopening_a_revoked_invite_counts_against_the_cap(svc):  # noqa: F811
    svc.invite_real_records("back@example.com", "operator")
    svc.revoke_real_records_invite("back@example.com")
    for i in range(beta.STAGE1_INVITE_CAP):
        svc.invite_real_records(f"t{i}@example.com", "operator")
    with pytest.raises(ValueError, match="full"):
        svc.invite_real_records("back@example.com", "operator")
    assert svc.real_records_invited("back@example.com") is False


def test_an_already_active_invite_is_not_refused_when_full(svc):  # noqa: F811
    for i in range(beta.STAGE1_INVITE_CAP):
        svc.invite_real_records(f"t{i}@example.com", "operator")
    assert svc.invite_real_records("t3@example.com", "operator") is False


def test_the_list_carries_only_the_listed_fields(svc):  # noqa: F811
    svc.invite_real_records("tester@example.com", "ops-1")
    [row] = svc.real_record_invites()
    assert set(row) == {"email", "invited_at", "invited_by", "revoked_at"}
    assert row["invited_by"] == "ops-1"
