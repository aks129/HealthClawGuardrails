"""Choosing which set of records a check-in answers from
(careagents/checkin_wallet.py, POST /checkin/wallet/records).

Seen live 2026-10-26: an account with two real-record connections of its own
was told "this test version can answer from only one" and could not check
in. Now the relay lists the eligible sets, the person picks one, and the
browser sends that connection's id. What these pin:

- The list names each set by its connection label and provider only: no
  tenant id, no record content. Only the account holder's own active,
  non-sample connections are on it.
- The id from the page chooses nothing by itself. The server re-derives the
  tenant from the signed-in account's own connections on every call; an id
  that is not one of them (another account's, someone cared for, the
  sample, the test patient, pending, revoked, made up) is one fixed
  refusal, the same for all, and the engine is never called.
- One eligible set with no id answers exactly as before.

Synthetic data only.
"""
from __future__ import annotations

import pytest

from tests.test_checkin_relay import _careagents, _FakeClient, _post
from tests.test_checkin_wallet import _account, _sign_in

TYPES = {"resource_types": ["Patient", "Coverage"]}


def _pick(cid, types=None):
    return {"resource_types": types or TYPES["resource_types"],
            "connection_id": cid}


@pytest.fixture
def two(monkeypatch):
    """A signed-in browser whose account holds two real record sets."""
    hc = _FakeClient()
    app, svc = _careagents(hc)
    acct = _account(svc, monkeypatch)
    a = svc.add_connection(acct.id, "direct", "ca-pick-one", "Upload",
                           provider="Synthetic Clinic A")
    b = svc.add_connection(acct.id, "fasten", "ca-pick-two", "Portal",
                           provider="Synthetic Hospital B")
    browser = app.test_client()
    _sign_in(browser, acct)
    yield browser, hc, svc, acct, a, b
    svc.engine.dispose()


# --- the list ------------------------------------------------------------------

def test_two_sets_are_listed_by_label_how_and_when_only(two):
    """Each set by its label, a fixed phrase for how it was connected, and
    when: account bookkeeping, never a tenant id or anything from the
    records."""
    browser, hc, svc, acct, a, b = two
    r = _post(browser)
    assert r.status_code == 409
    assert r.headers["Cache-Control"] == "no-store"
    added = {c: svc.get_connection(acct.id, c)["connected_at"] for c in (a, b)}
    assert r.get_json() == {"error": "several_records", "choices": [
        {"id": a, "label": "Upload", "how": "Uploaded file",
         "added": added[a]},
        {"id": b, "label": "Portal", "how": "From a patient portal",
         "added": added[b]},
    ]}
    body = r.get_data(as_text=True)
    assert "ca-pick-one" not in body and "ca-pick-two" not in body
    assert hc.calls == []


@pytest.mark.parametrize("kind, provider, how", [
    ("fasten", "your doctor", "From a patient portal"),
    ("direct", "Direct upload", "Uploaded file"),
    ("wearable", "Oura", "From Oura"),
    ("wearable", "Somewhere <b>odd</b>", "From a device or app"),
    ("wearable", None, "From a device or app"),
    ("hbo", "Free text from anywhere", "Connected records"),
])
def test_how_is_a_fixed_phrase_never_free_text(kind, provider, how):
    """Only a wearable names its source, and only from our own catalog."""
    from careagents.checkin_wallet import connected_how
    assert connected_how({"kind": kind, "provider": provider}) == how


def test_only_the_persons_own_active_real_sets_are_listed(two):
    browser, hc, svc, acct, a, b = two
    other = svc.add_person(acct.id, "parent")
    svc.add_connection(acct.id, "direct", "ca-pick-parent", "Mum's records",
                       person_id=other)
    svc.add_connection(acct.id, "sample", "ca-pick-sample", "Sample")
    svc.add_connection(acct.id, "direct", "ca-pick-wait", "Waiting",
                       status="pending")
    gone = svc.add_connection(acct.id, "direct", "ca-pick-gone", "Gone")
    svc.revoke_connection(acct.id, gone)
    r = _post(browser)
    assert [c["id"] for c in r.get_json()["choices"]] == [a, b]


def test_two_connections_to_one_tenant_are_one_choice(two):
    browser, hc, svc, acct, a, b = two
    svc.add_connection(acct.id, "direct", "ca-pick-one", "Upload again")
    r = _post(browser)
    assert [c["id"] for c in r.get_json()["choices"]] == [a, b]


# --- the pick ------------------------------------------------------------------

def test_a_pick_answers_from_that_set_and_the_server_says_which(two):
    browser, hc, svc, acct, a, b = two
    r = _post(browser, _pick(b, ["Patient", "Coverage", "Patient"]))
    assert r.status_code == 200
    assert hc.calls == [("ca-pick-two", ["Coverage", "Patient"],
                         f"careagents:{acct.id}")]
    assert _post(browser, _pick(a)).status_code == 200
    assert [c[0] for c in hc.calls] == ["ca-pick-two", "ca-pick-one"]


def test_one_set_answers_as_before_with_or_without_its_id(monkeypatch):
    hc = _FakeClient()
    app, svc = _careagents(hc)
    try:
        acct = _account(svc, monkeypatch)
        cid = svc.add_connection(acct.id, "direct", "ca-pick-only", "Upload")
        browser = app.test_client()
        _sign_in(browser, acct)
        assert _post(browser).status_code == 200
        assert _post(browser, _pick(cid)).status_code == 200
        assert [c[0] for c in hc.calls] == ["ca-pick-only", "ca-pick-only"]
    finally:
        svc.engine.dispose()


# --- what the page cannot choose ---------------------------------------------

def test_another_accounts_connection_is_refused(two, monkeypatch):
    """IDOR: the id of a set on someone else's account opens nothing."""
    browser, hc, svc, acct, a, b = two
    them = _account(svc, monkeypatch, email="wallet-other@example.test")
    theirs = svc.add_connection(them.id, "direct", "ca-pick-theirs", "Upload")
    r = _post(browser, _pick(theirs))
    assert r.status_code == 409
    assert r.headers["Cache-Control"] == "no-store"
    assert r.get_json() == {"error": "unknown_records"}
    assert hc.calls == []


def test_an_id_that_is_not_an_eligible_set_is_one_refusal(two):
    browser, hc, svc, acct, a, b = two
    other = svc.add_person(acct.id, "parent")
    ids = [
        svc.add_connection(acct.id, "direct", "ca-pick-parent", "Upload",
                           person_id=other),
        svc.add_connection(acct.id, "sample", "ca-pick-sample", "Sample"),
        svc.add_connection(acct.id, "direct", "ca-pick-wait", "Waiting",
                           status="pending"),
        "conn_doesnotexist",
        "ca-pick-one",          # a tenant id is not a connection id
    ]
    gone = svc.add_connection(acct.id, "direct", "ca-pick-gone", "Gone")
    svc.revoke_connection(acct.id, gone)
    ids.append(gone)
    for cid in ids:
        r = _post(browser, _pick(cid))
        assert r.status_code == 409, cid
        assert r.get_json() == {"error": "unknown_records"}, cid
    assert hc.calls == []


def test_a_set_disconnected_after_the_pick_is_refused(two):
    browser, hc, svc, acct, a, b = two
    svc.revoke_connection(acct.id, b)
    r = _post(browser, _pick(b))
    assert r.get_json() == {"error": "unknown_records"}
    assert hc.calls == []


@pytest.mark.parametrize("body", [
    {"connection_id": "conn_x"},
    {"resource_types": ["Patient"], "connection_id": None},
    {"resource_types": ["Patient"], "connection_id": 7},
    {"resource_types": ["Patient"], "connection_id": ["conn_x"]},
    {"resource_types": ["Patient"], "connection_id": ""},
    {"resource_types": ["Patient"], "connection_id": "c" * 33},
    {"resource_types": ["Patient"], "connection_id": "conn_x",
     "tenant_id": "ca-pick-two"},
    {"resource_types": ["Patient"], "connection_id": "conn_x",
     "origin": "https://clinic.example"},
])
def test_the_body_is_still_the_types_and_at_most_one_id(two, body):
    browser, hc, *_ = two
    r = _post(browser, body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad_request"}
    assert hc.calls == []


# --- the connectathon test patient is unchanged -------------------------------

def test_the_test_patient_answers_without_an_id_and_never_with_one(
        monkeypatch):
    from careagents.checkin_wallet import TEST_LABEL, TEST_PROVIDER
    hc = _FakeClient()
    app, svc = _careagents(hc, CARE_CHECKIN_TEST_PATIENT="1")
    try:
        acct = _account(svc, monkeypatch)
        cid = svc.add_connection(acct.id, "sample", "ca-pick-test",
                                 TEST_LABEL, provider=TEST_PROVIDER)
        browser = app.test_client()
        _sign_in(browser, acct)
        assert _post(browser).status_code == 200
        r = _post(browser, _pick(cid))
        assert r.get_json() == {"error": "unknown_records"}
        assert [c[0] for c in hc.calls] == ["ca-pick-test"]
    finally:
        svc.engine.dispose()
