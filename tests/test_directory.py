"""The provider-directory client (r6/directory.py) and its search route.

No test reaches a directory. What is recorded and what is invented:

- NPPES: tests/fixtures/directory_nppes_capture.json is one answer captured
  on 2026-11-27. Its header gives the exact request and what was replaced:
  keys, nesting and types are as answered, every value that could name a
  provider is invented. The hand-built NPPES rows below are checked against
  it (`test_the_hand_built_rows_have_the_captured_shape`).
- AINPI: no capture is checked in. Its field names and nesting are the ones
  it answered with on 2026-11-25, as the first commit and the pull request
  say; the command and the raw answer were not recorded.
- Every value is invented: names, NPIs (with a valid check digit), numbers,
  cities, counts and dates. The rows marked "Dropped" and every body built
  inside a test were constructed for that test and never seen live.
"""

import json
from pathlib import Path

import pytest
import requests

from r6 import directory
from r6.models import AuditEventRecord

AINPI_BODY = {
    "type": "search",
    "query": {"name": "rivera", "state": "PA", "type": "all", "limit": 20},
    "total_results": 3,
    "data": {
        "practitioners": [
            {"npi": "1999999992", "family_name": "RIVERA",
             "given_name": "SAMPLE", "gender": "female", "state": "PA",
             "city": "York", "postal_code": "17408", "active": True,
             "all_states": "PA"},
            # Dropped: a check digit that does not hold, and a name that is
            # not a name.
            {"npi": "1999999999", "family_name": "RIVERA",
             "given_name": "WRONG", "state": "PA", "city": "York",
             "postal_code": "17408", "active": True},
            {"npi": "1888888884", "family_name": "<b>RIVERA</b>",
             "given_name": "MARKUP", "state": "PA", "city": "York",
             "postal_code": "17408", "active": True},
        ],
        "organizations": [
            {"id": "Organization-1777777776", "npi": "1777777776",
             "name": "RIVERA FAMILY PRACTICE", "org_type": "prov",
             "state": "PA", "city": "Bethany", "active": True,
             "part_of_ref": None},
        ],
        "locations": [], "endpoints": [],
    },
    "source": "cms_npd", "release_date": "2026-10-07",
}


def _pharmacy(npi, name, location_zip, phone="215-555-0100", code="3336C0003X"):
    return {
        "number": npi, "enumeration_type": "NPI-2",
        "basic": {"organization_name": name, "status": "A",
                  "authorized_official_telephone_number": "8885550199"},
        "addresses": [
            {"address_purpose": "MAILING", "city": "NEW YORK", "state": "NY",
             "postal_code": "100393802", "telephone_number": "888-555-0199"},
            {"address_purpose": "LOCATION", "city": "PHILADELPHIA",
             "state": "PA", "postal_code": location_zip,
             "telephone_number": phone, "fax_number": "215-555-0101"},
        ],
        "taxonomies": [{"code": code, "desc": "Pharmacy, upstream wording",
                        "primary": True}],
    }


#: The ends of every phone and fax number above; no NPI or ZIP holds one.
NUMBER_PARTS = ("0100", "0101", "0199")

NPPES_BODY = {"result_count": 4, "results": [
    _pharmacy("1666666668", "SAMPLE PHARMACY #12", "191034606"),
    _pharmacy("1555555550", "NO PHONE PHARMACY", "19103", phone=None),
    # Dropped: only the mailing address is in the ZIP; a mail-order type.
    _pharmacy("1234567893", "ELSEWHERE PHARMACY", "191470000"),
    _pharmacy("1999999992", "MAIL ORDER PHARMACY", "19103", code="3336M0002X"),
]}


class FakeSession:
    """Answers every GET with one body and keeps what it was asked."""

    def __init__(self, body=None, status=200, error=None):
        self.body, self.status, self.error = body, status, error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        resp = requests.Response()
        resp.status_code = self.status
        resp._content = json.dumps(self.body).encode()
        return resp

    def sent_url(self, i=0):
        url, kwargs = self.calls[i]
        return requests.Request("GET", url, params=kwargs["params"]).prepare().url


def test_clinician_search_sends_only_name_and_state_and_reads_the_rows():
    session = FakeSession(AINPI_BODY)
    got = directory.find_clinicians("  Rivera ", "pa", session=session)
    found = got.listings
    assert (got.dropped, got.truncated) == (2, False)
    assert session.sent_url() == \
        "https://ainpi.dev/api/npd/search?name=Rivera&state=PA"
    _, kwargs = session.calls[0]
    assert kwargs["headers"] == {"Accept": "application/json"}
    assert kwargs["timeout"] == directory.TIMEOUT
    assert kwargs["allow_redirects"] is False
    assert [(e.npi, e.name, e.kind, e.city, e.postal_code) for e in found] == [
        ("1999999992", "SAMPLE RIVERA", "clinician", "York", "17408"),
        ("1777777776", "RIVERA FAMILY PRACTICE", "practice", "Bethany", None),
    ]
    # The source does not say, and nothing is inferred.
    assert all(e.has_phone is None and e.taxonomy is None for e in found)


@pytest.mark.parametrize("echo", [
    {"state": "PA", "type": "all", "limit": 20},   # the ignored-filter case
    {"name": "smith", "state": "PA"},
    {"name": "rivera", "state": "NJ"},
    None,
])
def test_rows_are_refused_when_the_echoed_query_is_not_what_was_sent(echo):
    """MUTATION: `any` -> `all` in the echo check -> one matching term is
    enough -> red."""
    session = FakeSession({**AINPI_BODY, "query": echo})
    with pytest.raises(directory.Unavailable):
        directory.find_clinicians("Rivera", "PA", session=session)


@pytest.mark.parametrize("name, state", [
    ("Rivera", "Pennsylvania"), ("Rivera", "P1"), ("R", "PA"),
    ("x" * 41, "PA"), ("Rivera 1961-04-02", "PA"), ("Rivera&npi=1", "PA"),
    ("Rivera\nX-Tenant-Id: t", "PA"), (None, "PA"), ("Rivera", None),
])
def test_a_malformed_clinician_term_sends_nothing(name, state):
    session = FakeSession(AINPI_BODY)
    with pytest.raises(ValueError):
        directory.find_clinicians(name, state, session=session)
    assert session.calls == []


def test_pharmacy_search_keeps_practice_locations_in_the_zip_only():
    session = FakeSession(NPPES_BODY)
    got = directory.find_pharmacies("19103", session=session)
    found = got.listings
    assert (got.dropped, got.truncated) == (2, False)
    assert session.sent_url() == (
        "https://npiregistry.cms.hhs.gov/api/?version=2.1"
        "&enumeration_type=NPI-2&taxonomy_description=Pharmacy"
        "&postal_code=19103&limit=50")
    assert [(e.npi, e.name, e.taxonomy, e.city, e.state, e.has_phone)
            for e in found] == [
        ("1666666668", "SAMPLE PHARMACY #12", "Community pharmacy",
         "PHILADELPHIA", "PA", True),
        ("1555555550", "NO PHONE PHARMACY", "Community pharmacy",
         "PHILADELPHIA", "PA", False),
    ]
    assert not hasattr(found[0], "phone")
    # has_phone is a yes or no about ten digits, and keeps none of them.
    worded = _pharmacy("1666666668", "SAMPLE PHARMACY", "19104", phone="CALL")
    assert directory.find_pharmacies("19104", session=FakeSession(
        {"results": [worded]})).listings[0].has_phone is False
    assert not any(part in repr(found) for part in NUMBER_PARTS)
    assert "upstream wording" not in repr(found)


@pytest.mark.parametrize("bad", ["1910", "191034606", "19103*", "abcde", None])
def test_a_malformed_zip_sends_nothing(bad):
    session = FakeSession(NPPES_BODY)
    with pytest.raises(ValueError):
        directory.find_pharmacies(bad, session=session)
    assert session.calls == []


CAPTURE = json.loads((Path(__file__).parent / "fixtures"
                      / "directory_nppes_capture.json").read_text())


def test_the_client_reads_the_captured_nppes_answer():
    """The capture is a real answer with its values replaced (see its
    header). Rows, in order: a pharmacy in the ZIP; one whose second
    practice location is in the ZIP; one with only a mailing address there;
    one in the ZIP with no listed pharmacy type; a pharmacy in the ZIP.

    MUTATION: read `addresses` only, not `practiceLocations` -> the second
    row is dropped -> red. MUTATION: match `address_purpose` on a value the
    registry does not use -> nothing is listed -> red. MUTATION: add a code
    to PHARMACY_TYPES that was not in the answer -> KeyError -> red."""
    session = FakeSession(CAPTURE["body"])
    got = directory.find_pharmacies("19103", session=session)
    # The request the capture was made with is the one the client sends.
    assert "GET " + session.sent_url() == CAPTURE["captured"]["request"]
    assert [(e.name, e.taxonomy, e.city, e.state, e.postal_code, e.has_phone)
            for e in got.listings] == [
        ("SAMPLE PHARMACY A", "Community pharmacy", "PHILADELPHIA", "PA",
         "19103", True),
        ("SAMPLE PHARMACY B", "Specialty pharmacy", "PHILADELPHIA", "PA",
         "19103", True),
        ("SAMPLE PHARMACY E", "Community pharmacy", "PHILADELPHIA", "PA",
         "19103", True),
    ]
    assert (got.dropped, got.truncated) == (2, False)
    assert all(directory.is_npi(e.npi) for e in got.listings)
    # Every type the client lists was in the full answer, as a pharmacy.
    seen = CAPTURE["captured"]["pharmacy_taxonomies_in_the_full_answer"]
    assert all(seen[code].startswith("Pharmacy")
               for code in directory.PHARMACY_TYPES)


def _paths(value, at=""):
    """Every key path in a body, with the type found at its end."""
    if isinstance(value, dict):
        return set().union(*(_paths(v, f"{at}.{k}") for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(_paths(v, at + "[]") for v in value))
    return {f"{at}:{type(value).__name__}"}


def test_the_hand_built_rows_have_the_captured_shape():
    """MUTATION: rename a key in `_pharmacy`, or change a value's type ->
    red."""
    assert _paths([_pharmacy("1666666668", "SAMPLE PHARMACY", "191034606")]) \
        <= _paths(CAPTURE["body"]["results"])
    assert _paths({k: v for k, v in NPPES_BODY.items() if k != "results"}) \
        <= _paths({k: v for k, v in CAPTURE["body"].items() if k != "results"})


@pytest.mark.parametrize("session", [
    FakeSession(error=requests.Timeout("https://example.invalid/?name=x")),
    FakeSession({"error": "rate limited"}, status=429),
    FakeSession({"Errors": [{"description": "refused", "number": "06"}]}),
    FakeSession(["not", "an", "object"]),
])
def test_an_outage_names_no_url_and_is_remembered_briefly_as_a_failure(
        session, monkeypatch):
    """MUTATION: keep a failure for CACHE_SECONDS -> the last ask still
    fails -> red. MUTATION: answer a remembered failure with an empty Found
    -> the second ask does not raise -> red. MUTATION: do not remember a
    failure -> two outbound calls -> red."""
    clock = [1000.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: clock[0])
    session.calls.clear()
    for _ in range(2):
        with pytest.raises(directory.Unavailable) as caught:
            directory.find_pharmacies("19103", session=session)
        assert "example" not in str(caught.value)
        assert caught.value.__cause__ is None
    assert len(session.calls) == 1
    clock[0] += directory.UNAVAILABLE_SECONDS + 1
    back = FakeSession(NPPES_BODY)
    assert len(directory.find_pharmacies("19103", session=back).listings) == 2


def _echoed(body):
    return {"query": AINPI_BODY["query"], **body}


@pytest.mark.parametrize("ask, body", [
    (lambda s: directory.find_clinicians("Rivera", "PA", session=s),
     _echoed({})),
    (lambda s: directory.find_clinicians("Rivera", "PA", session=s),
     _echoed({"data": {}})),
    (lambda s: directory.find_clinicians("Rivera", "PA", session=s),
     _echoed({"data": {"practitioners": [], "organizations": {}}})),
    (lambda s: directory.find_pharmacies("19103", session=s), {}),
    (lambda s: directory.find_pharmacies("19103", session=s), {"data": {}}),
    (lambda s: directory.find_pharmacies("19103", session=s),
     {"results": {"0": {}}}),
])
def test_an_answer_without_its_list_of_rows_is_unavailable_never_none(
        ask, body, monkeypatch):
    """MUTATION: `_listed` answers [] for anything but a list -> "none"
    -> red. MUTATION: drop the `data` check -> AttributeError -> red."""
    clock = [1000.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: clock[0])
    with pytest.raises(directory.Unavailable):
        ask(FakeSession(body))
    assert directory._FAILED in [kept for _, kept in directory._CACHE.values()]
    # Not kept as an answer: once the short failure memory lapses, the
    # directory is asked again.
    clock[0] += directory.UNAVAILABLE_SECONDS + 1
    again = FakeSession(body)
    with pytest.raises(directory.Unavailable):
        ask(again)
    assert len(again.calls) == 1


@pytest.mark.parametrize("name", [
    "SAMPLE PHARMACY 215-555-0100", "SAMPLE PHARMACY 215 555 0100",
    "SAMPLE PHARMACY (215) 555-0100", "SAMPLE PHARMACY 555-0100",
    "SAMPLE PHARMACY 215.555.0100", "CALL 215 OR 555 THEN 0100 PHARMACY",
])
def test_no_listing_string_holds_a_phone_shaped_number(name):
    """MUTATION: put back the old rule alone (seven digits in a row, in the
    text as written) -> all six names pass -> red. MUTATION: search the
    text as written but keep the ten-digit rule -> the 555-0100 name passes
    -> red. MUTATION: drop the ten-digits-in-all rule -> the last name
    passes -> red."""
    rows = [_pharmacy("1666666668", name, "19103"),
            _pharmacy("1555555550", "NO PHONE PHARMACY", "19103")]
    got = directory.find_pharmacies("19103", session=FakeSession(
        {"result_count": 2, "results": rows}))
    assert [e.name for e in got.listings] == ["NO PHONE PHARMACY"]
    assert got.dropped == 1
    assert not any(part in repr(got) for part in NUMBER_PARTS)
    assert directory._shown(name) is None
    # A city that holds one is left out; the row's other fields stand.
    row = _pharmacy("1666666668", "SAMPLE PHARMACY", "19104")
    row["addresses"][1]["city"] = name
    got = directory.find_pharmacies("19104", session=FakeSession(
        {"result_count": 1, "results": [row]}))
    assert [(e.name, e.city) for e in got.listings] == [
        ("SAMPLE PHARMACY", None)]


def _npi(n):
    return next(f"1{n:08d}{d}" for d in "0123456789"
                if directory.is_npi(f"1{n:08d}{d}"))


def test_more_matches_than_are_listed_says_truncated():
    """MUTATION: `truncated` always False, for either directory -> red.
    MUTATION: drop the full-page half -> the last assert -> red."""
    many = [_pharmacy(_npi(n), "SAMPLE PHARMACY", "19103") for n in range(12)]
    got = directory.find_pharmacies("19103", session=FakeSession(
        {"result_count": 12, "results": many}))
    assert (len(got.listings), got.dropped, got.truncated) == (
        directory.MAX_RESULTS, 0, True)
    people = [{**AINPI_BODY["data"]["practitioners"][0], "npi": _npi(n)}
              for n in range(12)]
    got = directory.find_clinicians("Rivera", "PA", session=FakeSession(
        {**AINPI_BODY, "data": {"practitioners": people,
                                "organizations": []}}))
    assert (len(got.listings), got.truncated) == (directory.MAX_RESULTS, True)
    # A full page that kept nothing may be hiding matches on the next one.
    page = [_pharmacy(_npi(n), "ELSEWHERE PHARMACY", "19147")
            for n in range(directory.NPPES_PAGE)]
    got = directory.find_pharmacies("19104", session=FakeSession(
        {"result_count": 50, "results": page}))
    assert (got.listings, got.dropped, got.truncated) == (
        (), directory.NPPES_PAGE, True)


def test_the_offline_fixture_fails_a_test_that_reaches_the_real_session(
        monkeypatch):
    """The autouse `_directory_stays_offline` (conftest) is what keeps every
    test here off the network. `requests.Session` is faked so this test is
    offline even when the fixture is not.

    MUTATION: drop the `patch.setattr(directory, "_session", ...)` line in
    conftest -> the fake answers and nothing fails -> red."""
    monkeypatch.setattr(directory, "_SESSION", None)
    monkeypatch.setattr(directory.requests, "Session",
                        lambda: FakeSession(NPPES_BODY))
    # Not an Exception, so the client's own `except Exception` cannot turn
    # the failure into Unavailable.
    assert not issubclass(pytest.fail.Exception, Exception)
    with pytest.raises(pytest.fail.Exception, match="provider directory"):
        directory.find_pharmacies("19103")


def test_an_answer_is_cached_by_its_terms_and_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: clock[0])
    session = FakeSession(NPPES_BODY)
    first = directory.find_pharmacies("19103", session=session)
    assert directory.find_pharmacies("19103", session=session) == first
    assert len(session.calls) == 1
    empty = FakeSession({"result_count": 0, "results": []})
    none = directory.Found((), 0, False)
    assert directory.find_pharmacies("19104", session=empty) == none
    assert directory.find_pharmacies("19104", session=empty) == none
    assert len(empty.calls) == 1
    clock[0] += directory.CACHE_SECONDS + 1
    directory.find_pharmacies("19103", session=session)
    assert len(session.calls) == 2
    assert len(directory._CACHE) <= directory.CACHE_MAX


def _audits():
    return [(a.outcome, a.detail) for a in AuditEventRecord.query.filter_by(
        resource_type="Directory").order_by(AuditEventRecord.recorded).all()]


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("DIRECTORY_SEARCH_ENABLED", "true")


@pytest.mark.parametrize("with_token", [True, False])
def test_the_route_is_off_by_default_and_then_does_nothing(
        client, auth_headers, tenant_headers, monkeypatch, with_token):
    """MUTATION: `search_enabled` returns True -> 200 or 401, not 404 ->
    red."""
    monkeypatch.delenv("DIRECTORY_SEARCH_ENABLED", raising=False)
    session = FakeSession(NPPES_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    before = AuditEventRecord.query.count()
    # 404 without a token too: the switch is read before the gate.
    resp = client.get("/r6/directory/search?kind=pharmacy&zip=19103",
                      headers=auth_headers if with_token else tenant_headers)
    assert resp.status_code == 404
    assert session.calls == []
    assert AuditEventRecord.query.count() == before


def test_the_route_sends_no_tenant_and_returns_no_number(
        client, search_on, auth_headers, tenant_id, monkeypatch):
    """MUTATION: add the request's X-Tenant-Id to the params `_get` sends
    -> red."""
    session = FakeSession(NPPES_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get("/r6/directory/search?kind=pharmacy&zip=19103"
                      "&patient=pt-1&note=free+text", headers=auth_headers)
    assert resp.status_code == 200
    answer = resp.get_json()
    assert set(answer["results"][0]) == {
        "npi", "name", "kind", "taxonomy", "city", "state", "postal_code",
        "has_phone"}
    assert (answer["dropped"], answer["truncated"]) == (2, False)
    assert not any(part in resp.get_data(as_text=True) for part in NUMBER_PARTS)
    sent = repr(session.calls)
    for leaked in (tenant_id, auth_headers["X-Step-Up-Token"], "pt-1",
                   "free text", "X-Tenant-Id"):
        assert leaked not in sent
    assert _audits() == [
        ("success", "directory search: source=nppes; results=2; dropped=2")]


def test_the_route_tells_all_rows_dropped_from_none_and_no_list_from_both(
        client, search_on, auth_headers, monkeypatch):
    """MUTATION: report `dropped` as 0 in the route, or `truncated` as
    False -> red."""
    url = "/r6/directory/search?kind=pharmacy&zip="
    bodies = iter([
        {"results": [_pharmacy("1999999992", "MAIL ORDER PHARMACY", "19103",
                               code="3336M0002X")]},
        {"results": []}, {},
        {"results": [_pharmacy(_npi(n), "SAMPLE PHARMACY", "19106")
                     for n in range(12)]}])
    monkeypatch.setattr(directory, "_session",
                        lambda: FakeSession(next(bodies)))
    resp = client.get(url + "19103", headers=auth_headers)
    assert resp.get_json() == {"status": "ok", "results": [], "dropped": 1,
                               "truncated": False}
    resp = client.get(url + "19104", headers=auth_headers)
    assert resp.get_json() == {"status": "ok", "results": [], "dropped": 0,
                               "truncated": False}
    resp = client.get(url + "19105", headers=auth_headers)
    assert resp.status_code == 503
    resp = client.get(url + "19106", headers=auth_headers)
    assert (len(resp.get_json()["results"]), resp.get_json()["truncated"]) \
        == (directory.MAX_RESULTS, True)
    assert _audits()[:3] == [
        ("success", "directory search: source=nppes; results=0; dropped=1"),
        ("success", "directory search: source=nppes; results=0; dropped=0"),
        ("failure", "directory search: source=nppes; unavailable")]


def test_the_route_needs_a_token_before_anything_is_sent(
        client, search_on, tenant_headers, monkeypatch):
    session = FakeSession(NPPES_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get("/r6/directory/search?kind=pharmacy&zip=19103",
                      headers=tenant_headers)
    assert resp.status_code == 401
    assert session.calls == []


@pytest.mark.parametrize("query", [
    "kind=pharmacy&zip=1910", "kind=clinician&name=Rivera", "kind=other",
    "name=Rivera&state=PA",
])
def test_the_route_answers_400_for_bad_terms_and_sends_nothing(
        client, search_on, auth_headers, monkeypatch, query):
    session = FakeSession(AINPI_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get(f"/r6/directory/search?{query}", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_parameters"}
    assert session.calls == []


def test_only_the_terms_can_answer_400(
        client, search_on, auth_headers, monkeypatch):
    """MUTATION: call the finder inside the route's validation `try` -> its
    ValueError is answered 400 invalid_parameters -> red."""
    def broken(*terms):
        raise ValueError("raised while a directory's answer was read")

    monkeypatch.setattr(directory, "find_pharmacies", broken)
    try:
        status = client.get("/r6/directory/search?kind=pharmacy&zip=19103",
                            headers=auth_headers).status_code
    except ValueError:
        status = 500  # the test client re-raises what a server answers 500
    assert status == 500


def test_the_route_says_unavailable_and_audits_without_the_terms(
        client, search_on, auth_headers, monkeypatch):
    session = FakeSession({**AINPI_BODY, "query": {"state": "PA"}})
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get("/r6/directory/search?kind=clinician&name=Rivera"
                      "&state=PA", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unavailable", "results": []}
    assert _audits() == [("failure",
                          "directory search: source=ainpi; unavailable")]
