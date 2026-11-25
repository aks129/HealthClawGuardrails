"""The provider-directory client (r6/directory.py) and its search route.

No test reaches a directory. The bodies below have the shape both services
answered with on 2026-11-25; every name, NPI and number in them is made up
(the NPIs carry a valid check digit).
"""

import json

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
    found = directory.find_clinicians("  Rivera ", "pa", session=session)
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
    found = directory.find_pharmacies("19103", session=session)
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
    assert not any(part in repr(found) for part in NUMBER_PARTS)
    assert "upstream wording" not in repr(found)


@pytest.mark.parametrize("bad", ["1910", "191034606", "19103*", "abcde", None])
def test_a_malformed_zip_or_npi_sends_nothing(bad):
    session = FakeSession(NPPES_BODY)
    with pytest.raises(ValueError):
        directory.find_pharmacies(bad, session=session)
    with pytest.raises(ValueError):
        directory.contact(bad, session=session)
    with pytest.raises(ValueError):
        directory.contact("1666666669", session=session)  # check digit
    assert session.calls == []


def test_numbers_come_only_from_the_by_npi_call_and_the_location_address():
    session = FakeSession({"result_count": 1, "results": [NPPES_BODY["results"][0]]})
    got = directory.contact("1666666668", session=session)
    assert session.sent_url() == \
        "https://npiregistry.cms.hhs.gov/api/?version=2.1&number=1666666668"
    assert got == directory.Contact("1666666668", "SAMPLE PHARMACY #12",
                                    "2155550100", "2155550101")
    assert directory.contact(
        "1234567893", session=FakeSession({"result_count": 0, "results": []})
    ) is None


@pytest.mark.parametrize("session", [
    FakeSession(error=requests.Timeout("https://example.invalid/?name=x")),
    FakeSession({"error": "rate limited"}, status=429),
    FakeSession({"Errors": [{"description": "refused", "number": "06"}]}),
    FakeSession(["not", "an", "object"]),
])
def test_an_outage_is_unavailable_is_not_cached_and_names_no_url(session):
    for _ in range(2):
        with pytest.raises(directory.Unavailable) as caught:
            directory.find_pharmacies("19103", session=session)
        assert "example" not in str(caught.value)
        assert caught.value.__cause__ is None
    assert len(session.calls) == 2


def test_an_answer_is_cached_by_its_terms_and_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: clock[0])
    session = FakeSession(NPPES_BODY)
    first = directory.find_pharmacies("19103", session=session)
    assert directory.find_pharmacies("19103", session=session) == first
    assert len(session.calls) == 1
    empty = FakeSession({"result_count": 0, "results": []})
    assert directory.find_pharmacies("19104", session=empty) == ()
    assert directory.find_pharmacies("19104", session=empty) == ()
    assert len(empty.calls) == 1
    clock[0] += directory.CACHE_SECONDS + 1
    directory.find_pharmacies("19103", session=session)
    assert len(session.calls) == 2
    assert len(directory._CACHE) <= directory.CACHE_MAX


def _audits():
    return [(a.outcome, a.detail) for a in AuditEventRecord.query.filter_by(
        resource_type="Directory").all()]


def test_the_route_sends_no_tenant_and_returns_no_number(
        client, auth_headers, tenant_id, monkeypatch):
    session = FakeSession(NPPES_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get("/r6/directory/search?kind=pharmacy&zip=19103"
                      "&patient=pt-1&note=free+text", headers=auth_headers)
    assert resp.status_code == 200
    assert set(resp.get_json()["results"][0]) == {
        "npi", "name", "kind", "taxonomy", "city", "state", "postal_code",
        "has_phone"}
    assert not any(part in resp.get_data(as_text=True) for part in NUMBER_PARTS)
    sent = repr(session.calls)
    for leaked in (tenant_id, auth_headers["X-Step-Up-Token"], "pt-1",
                   "free text", "X-Tenant-Id"):
        assert leaked not in sent
    assert _audits() == [("success", "directory search: source=nppes; results=2")]


def test_the_route_needs_a_token_before_anything_is_sent(
        client, tenant_headers, monkeypatch):
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
        client, auth_headers, monkeypatch, query):
    session = FakeSession(AINPI_BODY)
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get(f"/r6/directory/search?{query}", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_parameters"}
    assert session.calls == []


def test_the_route_says_unavailable_and_audits_without_the_terms(
        client, auth_headers, monkeypatch):
    session = FakeSession({**AINPI_BODY, "query": {"state": "PA"}})
    monkeypatch.setattr(directory, "_session", lambda: session)
    resp = client.get("/r6/directory/search?kind=clinician&name=Rivera"
                      "&state=PA", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unavailable", "results": []}
    assert _audits() == [("failure",
                          "directory search: source=ainpi; unavailable")]
