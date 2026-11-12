"""The RxNav client: what is sent, which concepts get a name, which name.

No network. tests/fixtures/rxnav_historystatus.json holds historystatus
bodies recorded from RxNav on 2026-11-11, keyed by code;
tests/fixtures/rxnav_concepts.json is the parsed copy of every static-table
row. A body marked "varied" below is a recorded one with one field changed.

MUTATION, each caught here:
- drop the status check in name_for: test_a_retired_concept_gets_no_name;
- compare the status case-sensitively: test_the_status_is_compared_
  without_case;
- loosen or remove is_rxcui: test_a_code_that_is_not_an_rxcui_is_never_sent;
- name an SBD by its own (brand) name: test_a_branded_drug_is_named_by_
  its_generic.
The full list (42 mutants, with tests/test_qa_1126_rxnav.py and
tests/test_rxnav_exploits_pr1126.py) is in the pull request.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import flask
import pytest

from r6 import rxnav, terminology, terminology_resolver

FIXTURES = Path(__file__).parent / "fixtures"
BODIES = json.loads((FIXTURES / "rxnav_historystatus.json").read_text())
CONCEPTS = json.loads((FIXTURES / "rxnav_concepts.json").read_text())
URL = "https://rxnav.nlm.nih.gov/REST/rxcui/%s/historystatus.json"


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    """Replays bodies by code and records every call it is given."""

    def __init__(self, bodies=BODIES, status=200, raises=None):
        self.calls: list[tuple[str, dict]] = []
        self._bodies, self._status, self._raises = bodies, status, raises

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raises:
            raise self._raises
        code = url.rsplit("/", 2)[1]
        return _Resp(self._bodies[code], self._status)

    @property
    def urls(self):
        return [url for url, _ in self.calls]


def _varied(code, **changes):
    """The recorded body for `code` with fields of its history replaced."""
    body = copy.deepcopy(BODIES[code])
    for path, value in changes.items():
        section, key = path.split("__")
        if value is None:
            del body["rxcuiStatusHistory"][section][key]
        else:
            body["rxcuiStatusHistory"][section][key] = value
    return body


# --- what leaves ---------------------------------------------------------

@pytest.mark.parametrize("code", [
    "860975 Jane", "860975\n", " 860975", "86097a", "123456789", "", "-1",
    "٣٤٥",  # Arabic-Indic digits: isdigit() says yes
    "１２", None, 860975, ["860975"],
])
def test_a_code_that_is_not_an_rxcui_is_never_sent(code):
    session = _Session()
    assert rxnav.is_rxcui(code) is False
    assert rxnav.fetch(code, session=session) is None
    with pytest.raises(ValueError):
        rxnav.history(code, session=session)
    assert session.calls == []


def test_only_the_code_is_sent_once_with_a_timeout_and_no_redirect():
    session = _Session()
    rxnav.fetch("861007", session=session)
    assert session.calls == [(URL % "861007", {
        "headers": {"Accept": "application/json"},
        "timeout": rxnav.TIMEOUT,
        "allow_redirects": False,
    })]
    assert 0 < rxnav.TIMEOUT <= 5


@pytest.mark.parametrize("session", [
    _Session(status=500), _Session(status=429), _Session(status=302),
    _Session(raises=TimeoutError("slow")),
    _Session(bodies={"861007": ValueError("not json")}),
    _Session(bodies={"861007": {}}),
    _Session(bodies={"861007": {"rxcuiStatusHistory": ["x"]}}),
    _Session(bodies={"861007": {"rxcuiStatusHistory": {"metaData": {}}}}),
], ids=["500", "429", "redirect", "timeout", "not-json", "empty", "list",
        "no-status"])
def test_a_failed_request_is_unavailable_and_is_not_retried(session):
    with pytest.raises(rxnav.Unavailable):
        rxnav.fetch("861007", session=session)
    assert len(session.calls) == 1


# --- which concepts get a name, and which name ---------------------------

def test_a_clinical_drug_is_named_by_rxnorm():
    concept = rxnav.fetch("861007", session=_Session())
    assert concept == rxnav.Concept(
        rxcui="861007", status="active", tty="SCD",
        label="metformin hydrochloride 500 MG Oral Tablet", kind="product",
        remapped_to=None, ingredients=("6809",))


def test_a_branded_drug_is_named_by_its_generic():
    concept = rxnav.fetch("617314", session=_Session())
    assert (concept.label, concept.kind, concept.tty) == (
        "atorvastatin 10 MG Oral Tablet", "product", "SBD")


def test_a_branded_drug_with_no_generic_in_the_answer_gets_no_name():
    body = _varied("617314", derivedConcepts__scdConcept=None)
    concept = rxnav.fetch("617314", session=_Session({"617314": body}))
    assert (concept.label, concept.kind) == (None, None)


def test_a_pack_is_named_by_its_ingredients_and_is_not_a_product():
    concept = rxnav.fetch("748856", session=_Session())
    assert (concept.label, concept.kind) == (
        "drospirenone / ethinyl estradiol pack", "named")


@pytest.mark.parametrize("tty", ["BN", "SBDC", "SBDF", "SBDG", "DF", ""])
def test_a_brand_or_other_term_type_gets_no_name(tty):
    body = _varied("861007", attributes__tty=tty)
    concept = rxnav.fetch("861007", session=_Session({"861007": body}))
    assert (concept.label, concept.kind) == (None, None)


@pytest.mark.parametrize("code,status", [
    ("205517", "obsolete"),    # an SBD whose answer still carries an SCD name
    ("99999999", "unknown"),   # the live API writes UNKNOWN
])
def test_a_retired_concept_gets_no_name(code, status):
    session = _Session()
    concept = rxnav.fetch(code, session=session)
    assert (concept.status, concept.label, concept.kind) == (status, None, None)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", ["Quantified", "NotCurrent", "Retired?"])
def test_any_status_but_active_gets_no_name(status):
    body = _varied("861007", metaData__status=status)  # varied
    concept = rxnav.fetch("861007", session=_Session({"861007": body}))
    assert concept.label is None
    assert concept.status in rxnav.STATUSES


def test_the_status_is_compared_without_case():
    active = _varied("861007", metaData__status="ACTIVE")  # varied
    concept = rxnav.fetch("861007", session=_Session({"861007": active}))
    assert (concept.status, concept.kind) == ("active", "product")

    remapped = _varied("205510", metaData__status="REMAPPED")  # varied
    bodies = {"205510": remapped, "1012457": BODIES["1012457"]}
    concept = rxnav.fetch("205510", session=_Session(bodies))
    assert concept.remapped_to == "1012457" and concept.kind == "product"


def test_a_remap_to_one_concept_is_followed_one_hop():
    session = _Session()
    concept = rxnav.fetch("205510", session=session)
    assert session.urls == [URL % "205510", URL % "1012457"]
    assert concept == rxnav.Concept(
        rxcui="205510", status="remapped", tty="SBD",
        label="bupivacaine hydrochloride 5 MG/ML Injectable Solution",
        kind="product", remapped_to="1012457", ingredients=("1815",))


def test_a_remap_to_two_concepts_gets_no_name_and_no_second_request():
    session = _Session()
    concept = rxnav.fetch("205516", session=session)
    assert (concept.status, concept.label, concept.remapped_to) == (
        "remapped", None, None)
    assert session.urls == [URL % "205516"]


def test_a_remap_target_is_followed_once_and_must_itself_be_active():
    bodies = {"205510": BODIES["205510"],
              "1012457": _varied("205510", attributes__rxcui="1012457")}
    session = _Session(bodies)
    concept = rxnav.fetch("205510", session=session)
    assert (concept.label, concept.kind) == (None, None)
    assert len(session.calls) == 2


def test_a_remap_target_that_is_not_an_rxcui_is_never_sent():
    body = _varied("205510", derivedConcepts__remappedConcept=[
        {"remappedRxCui": "1012457/../../x", "remappedActive": "YES"}])
    session = _Session({"205510": body})
    concept = rxnav.fetch("205510", session=session)
    assert (concept.label, concept.remapped_to) == (None, None)
    assert session.urls == [URL % "205510"]


def test_a_failed_second_hop_is_unavailable_not_a_verdict():
    with pytest.raises(rxnav.Unavailable):
        rxnav.fetch("205510", session=_Session({"205510": BODIES["205510"]}))


def test_the_second_request_is_made_only_if_the_caller_allows_it():
    session = _Session()
    with pytest.raises(rxnav.Unavailable):
        rxnav.fetch("205510", session=session, may_follow=lambda: False)
    assert session.urls == [URL % "205510"]

    asked = []
    rxnav.fetch("205510", session=_Session(),
                may_follow=lambda: asked.append(1) or True)
    rxnav.fetch("861007", session=_Session(), may_follow=asked.append)
    rxnav.fetch("205516", session=_Session(), may_follow=asked.append)
    assert asked == [1]  # asked once, and only where a second request follows


# --- ingredient codes: reported whether or not there is a name -----------

@pytest.mark.parametrize("code,tty", [
    ("541892", "SCD"),  # RxNorm's name is 142 characters: no label
    ("84815", "BN"),    # a brand name: no label
])
def test_a_concept_with_no_label_still_reports_its_ingredient_codes(code, tty):
    concept = rxnav.fetch(code, session=_Session())
    assert (concept.status, concept.tty, concept.label) == ("active", tty, None)
    assert concept.ingredients == ("3288", "725")


def test_ingredient_codes_are_rxcuis_once_each_and_nothing_else():
    body = _varied("541892", derivedConcepts__ingredientConcept=[
        {"ingredientRxcui": "3288", "ingredientName": "<b>x</b>"},
        {"ingredientRxcui": "3288"}, {"ingredientRxcui": "725 Jane"},
        {"ingredientRxcui": 725}, {"ingredientName": "amphetamine"},
        {"ingredientRxcui": "725"}])
    concept = rxnav.fetch("541892", session=_Session({"541892": body}))
    assert concept.ingredients == ("3288", "725")
    assert rxnav.fetch("99999999", session=_Session()).ingredients == ()
    assert rxnav.fetch("205516", session=_Session()).ingredients == ()


def test_a_retired_concept_reports_its_ingredient_codes_without_a_name():
    concept = rxnav.fetch("205517", session=_Session())
    assert (concept.label, concept.ingredients) == (None, ("873",))


# --- every static-table row, from the recorded parsed copy ---------------

@pytest.mark.parametrize("code", sorted(CONCEPTS, key=int))
def test_the_name_rule_over_every_recorded_concept(code):
    concept = CONCEPTS[code]
    label, kind = rxnav.name_for(concept)
    tty = concept["tty"]
    if tty == "SCD":
        assert (label, kind) == (concept["name"], "product")
    elif tty == "SBD":  # the parsed copy records no generic name
        assert (label, kind) == (None, None)
    elif tty in ("GPCK", "BPCK"):
        assert kind == "named" and label.endswith(" pack")
        assert "inert" not in label
    else:
        assert kind == "named"
        assert label.lower() == concept["name"].lower()
        assert label[0].isupper() == (tty in ("IN", "PIN", "MIN"))
    if label is not None:
        assert rxnav.valid_label(label) and "[" not in label


@pytest.mark.parametrize("code", ["861007", "617314", "748856"])
def test_the_recorder_script_records_through_the_client(code, monkeypatch):
    """One parser: the script that wrote rxnav_concepts.json reads a body
    through r6/rxnav.py and reproduces the recorded row."""
    from scripts import record_rxnav_fixture as recorder

    monkeypatch.setattr(rxnav, "_session", lambda: _Session())
    assert recorder.fetch(code) == CONCEPTS[code]
    with pytest.raises(SystemExit):
        recorder.fetch(code + " Jane")


# --- what may be a label -------------------------------------------------

@pytest.mark.parametrize("text", [
    "<b>metformin</b>", "metformin\x1b[31m", "metformin ‮gnol",
    "metformin​ 500 MG", "No known drug allergy", "see http://x.example",
    "metformin [Glucophage]", "`metformin`", "metformin | 500", "500 / 10",
    "m", "", " metformin", "métformin", "x" * 121, None, 500,
])
def test_a_label_that_is_not_plain_text_is_refused(text):
    assert rxnav.valid_label(text) is False


@pytest.mark.parametrize("text", [
    "24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet",
    "NDA020800 0.3 ML epinephrine 1 MG/ML Auto-Injector", "x" * 120,
])
def test_a_plain_rxnorm_name_is_a_label(text):
    assert rxnav.valid_label(text) is True


@pytest.mark.parametrize("name", [
    "metformin <b>500</b> MG Oral Tablet", "No known drug allergy",
    "metformin 500 MG Oral Tablet " + "x" * 100,  # refused whole, never cut
])
def test_an_upstream_name_that_fails_is_dropped_not_trimmed(name):
    body = _varied("861007", attributes__name=name)
    concept = rxnav.fetch("861007", session=_Session({"861007": body}))
    assert (concept.label, concept.kind) == (None, None)


# --- the resolver's RxNorm path ------------------------------------------

@pytest.fixture
def resolver(monkeypatch):
    """The resolver switched on, RxNav replayed, every other route closed."""
    def _closed():
        raise AssertionError("RxNorm must not reach the curatr engine")

    session = _Session()
    terminology_resolver.reset_cache()
    monkeypatch.setenv(terminology_resolver.ENABLED_ENV, "true")
    monkeypatch.setattr(terminology_resolver, "_engine", _closed)
    monkeypatch.setattr(rxnav, "_session", lambda: session)
    yield session
    terminology_resolver.reset_cache()


def test_the_resolver_names_an_rxnorm_code_through_the_client(resolver):
    assert terminology_resolver.resolve(terminology.RXNORM, "617314") == (
        "atorvastatin 10 MG Oral Tablet")
    assert terminology_resolver.resolve(terminology.RXNORM, "617314") == (
        "atorvastatin 10 MG Oral Tablet")
    assert resolver.urls == [URL % "617314"]  # the second read is cached


def test_the_resolver_never_sends_or_keeps_free_text(resolver):
    assert terminology_resolver.resolve(
        terminology.RXNORM, "860975 Jane") is None
    assert resolver.calls == []
    assert terminology_resolver.cache_size() == 0


def test_the_resolver_caches_a_verdict_but_not_an_outage(resolver, monkeypatch):
    assert terminology_resolver.resolve(terminology.RXNORM, "205516") is None
    assert terminology_resolver.cache_size() == 1

    down = _Session(status=503)
    monkeypatch.setattr(rxnav, "_session", lambda: down)
    assert terminology_resolver.resolve(terminology.RXNORM, "861007") is None
    assert len(down.calls) == 1
    assert terminology_resolver.cache_size() == 1


def test_a_remap_spends_the_budget_once_per_request(resolver):
    """MUTATION: drop `may_follow=_budget_take` in resolve() -> one unit for
    two requests."""
    with flask.Flask(__name__).test_request_context("/"):
        terminology_resolver.resolve(terminology.RXNORM, "205510")
        assert terminology_resolver._budget_state()["used"] == 2
    assert len(resolver.calls) == 2


def test_the_second_request_respects_the_deadline(resolver, monkeypatch):
    """MUTATION: spend without asking `_budget_allows()` in _budget_take ->
    the second request goes out after the deadline."""
    class _Slow(_Session):
        def get(self, url, **kwargs):  # the first answer uses up the time
            terminology_resolver._budget_state()["deadline"] = 0.0
            return super().get(url, **kwargs)

    slow = _Slow()
    monkeypatch.setattr(rxnav, "_session", lambda: slow)
    with flask.Flask(__name__).test_request_context("/"):
        assert terminology_resolver.resolve(
            terminology.RXNORM, "205510") is None
    assert slow.urls == [URL % "205510"]
    assert terminology_resolver.cache_size() == 0  # not a verdict


def test_a_failed_lookup_does_not_log_the_code(resolver, monkeypatch, caplog):
    monkeypatch.setattr(rxnav, "_session", lambda: _Session(status=503))
    with caplog.at_level("DEBUG"):
        terminology_resolver.resolve(terminology.RXNORM, "861007")
    assert "861007" not in caplog.text
