"""QA for #1126: what the RxNav client's own tests leave unpinned.

No network. The first half pins behaviour the client already has but that no
test held (each row names the mutation it catches; all survived the PR's two
test files). The second half is three reproductions, marked
``xfail(strict=True)``: each goes red on the day its defect is fixed, which
is the cue to drop the marker.
"""
from __future__ import annotations

import json
from pathlib import Path

import flask
import pytest
import requests

from r6 import rxnav, terminology, terminology_resolver
from r6.actions import rx_transfer

CONCEPTS = json.loads(
    (Path(__file__).parent / "fixtures" / "rxnav_concepts.json").read_text())
RXNORM = terminology.RXNORM


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, bodies=None, raises=None):
        self.calls: list[str] = []
        self._bodies, self._raises = bodies or {}, raises

    def get(self, url, **_kwargs):
        self.calls.append(url)
        if self._raises:
            raise self._raises
        return _Resp(self._bodies[url.rsplit("/", 2)[1]])


def _body(name, tty="SCD", status="Active"):
    return {"rxcuiStatusHistory": {
        "metaData": {"status": status},
        "attributes": {"name": name, "tty": tty}}}


@pytest.fixture
def switched_on(monkeypatch):
    terminology_resolver.reset_cache()
    monkeypatch.setenv(terminology_resolver.ENABLED_ENV, "true")
    yield
    terminology_resolver.reset_cache()


# --- pinned: behaviour the client has, that nothing held -----------------

@pytest.mark.parametrize("code,label", [
    ("748856", "drospirenone / ethinyl estradiol pack"),
    # RxNav lists norgestimate three times (one per strength in the pack).
    ("751905", "norgestimate / ethinyl estradiol pack"),
])
def test_a_pack_names_each_ingredient_once(code, label):
    """MUTATION: drop `ingredient not in names` -> "norgestimate /
    norgestimate / norgestimate / ethinyl estradiol pack"."""
    assert rxnav.name_for(CONCEPTS[code]) == (label, "named")


@pytest.mark.parametrize("ingredients", [
    [], [{"ingredient": "inert ingredients"}], [{"ingredient": ""}],
])
def test_a_pack_with_nothing_to_name_gets_no_name(ingredients):
    """MUTATION: drop `if names else ""` -> the label is the word "pack",
    which valid_label accepts."""
    concept = {"status": "Active", "tty": "BPCK", "name": "{...} Pack [X]",
               "ingredients": ingredients}
    assert rxnav.name_for(concept) == (None, None)


def test_a_transport_error_does_not_put_the_code_in_the_log(
        switched_on, monkeypatch, caplog):
    """MUTATION: drop `from None` in rxnav.history -> the chained error,
    whose text carries the request URL, is written by the resolver's
    `exc_info=True`. The PR's log test uses a 503, which raises nothing."""
    error = requests.ConnectionError(
        "HTTPSConnectionPool(host='rxnav.nlm.nih.gov', port=443): Max "
        "retries exceeded with url: /REST/rxcui/861007/historystatus.json")
    monkeypatch.setattr(rxnav, "_session", lambda: _Session(raises=error))
    with caplog.at_level("DEBUG"):
        assert terminology_resolver.resolve(RXNORM, "861007") is None
    assert "terminology lookup failed" in caplog.text
    assert "861007" not in caplog.text


def test_a_code_that_is_not_an_rxcui_spends_no_budget(switched_on, monkeypatch):
    """MUTATION: move the resolver's shape check below `_budget_spend()` ->
    eight free-text codes in one record use up the request's lookups and the
    real codes after them go unlabelled. The comment in resolve() says
    "never spends the budget"; this is the test for it."""
    session = _Session({"861007": _body("metformin hydrochloride 500 MG "
                                        "Oral Tablet")})
    monkeypatch.setattr(rxnav, "_session", lambda: session)
    with flask.Flask(__name__).test_request_context("/"):
        for n in range(terminology_resolver.PER_REQUEST_MAX_LOOKUPS + 2):
            assert terminology_resolver.resolve(
                RXNORM, "86097%d Jane" % n) is None
        assert terminology_resolver._budget_state()["used"] == 0
        assert terminology_resolver.resolve(RXNORM, "861007") == (
            "metformin hydrochloride 500 MG Oral Tablet")
    assert len(session.calls) == 1


@pytest.mark.parametrize("tty", ["<b>SCD</b>", "scd", "S", "TOOLONG", "S CD"])
def test_a_term_type_that_is_not_shaped_like_one_is_not_kept(tty):
    """MUTATION: drop the `_TTY` shape gate -> upstream text is carried on
    the Concept as its term type."""
    session = _Session({"861007": _body("metformin 500 MG", tty)})
    concept = rxnav.fetch("861007", session=session)
    assert (concept.tty, concept.label) == ("", None)


def test_an_upstream_name_is_stripped_before_it_is_checked():
    """MUTATION: check the raw name instead of the stripped one -> a name
    with a trailing space is refused whole."""
    session = _Session({"861007": _body(
        " metformin hydrochloride 500 MG Oral Tablet ")})
    assert rxnav.fetch("861007", session=session).label == (
        "metformin hydrochloride 500 MG Oral Tablet")


def test_a_label_with_a_trailing_space_is_not_this_exact_string():
    """MUTATION: drop the `text != normalize(text).strip()` line -> the
    allowlist alone lets a trailing space through."""
    assert rxnav.valid_label("metformin 500 MG ") is False
    assert rxnav.valid_label("metformin 500 MG") is True


def test_a_remap_to_a_retired_concept_that_has_a_generic_name_gets_no_name():
    """MUTATION: treat the remap target as Active -> named "anthralin
    0.0025 MG/MG Topical Ointment". The PR's own test for this replays a
    target with no generic name in it, which gets no name either way."""
    bodies = json.loads((Path(__file__).parent / "fixtures"
                         / "rxnav_historystatus.json").read_text())
    remapped = json.loads(json.dumps(bodies["205510"]))
    remapped["rxcuiStatusHistory"]["derivedConcepts"]["remappedConcept"] = [
        {"remappedRxCui": "205517"}]
    session = _Session({"205510": remapped, "205517": bodies["205517"]})

    concept = rxnav.fetch("205510", session=session)

    assert len(session.calls) == 2
    assert (concept.label, concept.kind, concept.remapped_to) == (
        None, None, "205517")


# --- reproductions: red when fixed ---------------------------------------

#: RxNorm's own name for 541892, an Active SCD (asked 2026-11-11): 142
#: characters, so valid_label refuses it and the code reads as unlabelled.
MIXED_SALTS = ("amphetamine aspartate 2.5 MG / amphetamine sulfate 2.5 MG / "
               "dextroamphetamine saccharate 2.5 MG / dextroamphetamine "
               "sulfate 2.5 MG Oral Tablet")


@pytest.mark.xfail(strict=True, reason=(
    "#1126 QA finding 1: with the lookup on, a Schedule II product whose "
    "RxNorm name the client refuses is no longer refused for transfer"))
@pytest.mark.parametrize("code,body,text", [
    ("541892", _body(MIXED_SALTS, "SCD"), "MAS 10 mg tab"),
    ("84815", _body("Adderall", "BN"), "ADD 10 tab"),
], ids=["a-generic-with-a-long-name", "a-brand-name-code"])
def test_a_schedule_ii_order_is_still_refused_with_the_lookup_on(
        code, body, text, switched_on, monkeypatch):
    """On main the resolver named both codes (properties.json), the name
    carried "amphetamine" or "adderall", and SCHEDULE_II_TERMS refused the
    transfer. Product and brand codes are caught by name only: the code
    list in rx_transfer.py is ingredient-level. The feed's own text here
    names no listed term, as an abbreviation does not."""
    assert len(MIXED_SALTS) > rxnav.LABEL_MAX
    monkeypatch.setattr(rxnav, "_session", lambda: _Session({code: body}))
    order = {"resourceType": "MedicationRequest", "status": "active",
             "medicationCodeableConcept": {
                 "text": text, "coding": [{"system": RXNORM, "code": code}]}}

    result = rx_transfer.build_transfer_request(
        [order], {"name": "Corner Pharmacy", "phone": "555-0100"})

    # Either refusal will do (as Schedule II, or as unverifiable).
    assert result["allowed"] == []
    assert len(result["refused"]) == 1


@pytest.mark.xfail(strict=True, reason=(
    "#1126 QA finding 1, through the route: the proposal is drafted"))
def test_the_transfer_route_drafts_no_call_for_that_order(
        switched_on, monkeypatch, client, auth_headers):
    """The same order through POST /r6/actions/rx-transfer/propose, which
    reads the stored rows as written (text and display included)."""
    monkeypatch.setattr(rxnav, "_session", lambda: _Session(
        {"541892": _body(MIXED_SALTS, "SCD")}))
    order = {"resourceType": "MedicationRequest", "status": "active",
             "intent": "order", "subject": {"reference": "Patient/qa-1126"},
             "medicationCodeableConcept": {
                 "text": "MAS 10 mg tab",
                 "coding": [{"system": RXNORM, "code": "541892"}]}}
    created = client.post(
        "/r6/fhir/MedicationRequest", data=json.dumps(order),
        headers={**auth_headers, "X-Human-Confirmed": "true",
                 "Content-Type": "application/fhir+json"})
    assert created.status_code == 201, created.get_data(as_text=True)

    resp = client.post(
        "/r6/actions/rx-transfer/propose",
        headers={**auth_headers, "Content-Type": "application/json"},
        data=json.dumps({"to_pharmacy": {"name": "Corner Pharmacy",
                                         "phone": "+15551230000"}}))

    assert resp.status_code == 422, resp.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=(
    "#1126 QA finding 2: the offline guard's AssertionError is caught by "
    "rxnav.history and again by resolve(), so it cannot fail a test"))
def test_the_offline_guard_fails_a_test_that_reaches_rxnav(switched_on):
    """A test that reaches RxNav without replaying an answer should go red.
    Today it gets None, the same answer as an outage. If the fix records
    the attempt and fails at teardown instead of raising, rewrite this."""
    with pytest.raises(BaseException) as caught:
        terminology_resolver.resolve(RXNORM, "861007")
    assert not isinstance(caught.value, rxnav.Unavailable)
