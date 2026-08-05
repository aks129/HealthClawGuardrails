"""Guards for the opt-in runtime terminology resolver.

The resolver exists because a hand-curated table of 121 labels covered 1 of 15
distinct ICD-10-CM codes on a real MEDENT import. It sits on the PHI read path,
so the properties that matter are not "does it find labels" but "can it ever
break, slow, or widen a read". Each test below pins one of those.
"""
from __future__ import annotations

import time

import pytest

from r6 import terminology, terminology_resolver

ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SNOMED = "http://snomed.info/sct"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    terminology_resolver.reset_cache()
    terminology.reset_unlabelled()
    monkeypatch.setenv(terminology_resolver.ENABLED_ENV, "true")
    yield
    terminology_resolver.reset_cache()


def _never_called(*_args, **_kwargs):
    raise AssertionError("the terminology service must not have been called")


class _Recorder:
    """Stands in for CuratrEngine._lookup_code and counts calls."""

    def __init__(self, result=None, raises=None, delay=0.0):
        self.calls: list[tuple[str, str]] = []
        self._result = result
        self._raises = raises
        self._delay = delay

    def __call__(self, system, code):
        self.calls.append((system, code))
        if self._delay:
            time.sleep(self._delay)
        if self._raises:
            raise self._raises
        return self._result


def _install(monkeypatch, recorder):
    class _Engine:
        _lookup_code = staticmethod(recorder)

    monkeypatch.setattr(terminology_resolver, "_engine", lambda: _Engine())


# --- the switch ----------------------------------------------------------

def test_disabled_by_default_makes_no_call(monkeypatch):
    """MUTATION: drop the enabled() guard -> red.

    An unset env var must mean byte-identical behaviour to before this module
    existed. Nobody's clinical codes start leaving the box on a deploy.
    """
    monkeypatch.delenv(terminology_resolver.ENABLED_ENV, raising=False)
    _install(monkeypatch, _never_called)
    assert terminology_resolver.resolve(ICD10, "L40.9") is None


def test_enabled_resolves_a_code_the_static_table_lacks(monkeypatch):
    rec = _Recorder({"valid": True, "display": "Psoriasis, unspecified"})
    _install(monkeypatch, rec)
    assert terminology.lookup(ICD10, "L40.9") == "Psoriasis, unspecified"
    assert rec.calls == [(ICD10, "L40.9")]


def test_the_static_table_still_wins_and_makes_no_call(monkeypatch):
    """A hit in the dict must never reach the network."""
    _install(monkeypatch, _never_called)
    assert terminology.lookup(ICD10, "E78.5") is not None


# --- it cannot break a read ----------------------------------------------

def test_an_exception_is_swallowed_and_reads_as_unlabelled(monkeypatch):
    """MUTATION: let the exception propagate -> red.

    A terminology outage must degrade to "unreadable record", never to a 500
    on a patient's chat message.
    """
    _install(monkeypatch, _Recorder(raises=RuntimeError("tx down")))
    assert terminology.lookup(ICD10, "L40.9") is None


def test_a_malformed_response_reads_as_unlabelled(monkeypatch):
    for bad in (None, {}, {"valid": False, "display": "x"},
                {"valid": True, "display": None},
                {"valid": True, "display": "   "}, "not-a-dict"):
        terminology_resolver.reset_cache()
        _install(monkeypatch, _Recorder(bad))
        assert terminology_resolver.resolve(ICD10, "L40.9") is None, bad


def test_an_unroutable_system_is_never_sent(monkeypatch):
    """curatr can only route five systems; the rest are an immediate miss."""
    _install(monkeypatch, _never_called)
    assert terminology_resolver.resolve(
        "http://example.org/private-codes", "abc") is None


# --- it cannot slow a read -----------------------------------------------

def test_a_resolved_code_is_cached_and_queried_once(monkeypatch):
    rec = _Recorder({"valid": True, "display": "Psoriasis, unspecified"})
    _install(monkeypatch, rec)
    for _ in range(5):
        terminology_resolver.resolve(ICD10, "L40.9")
    assert len(rec.calls) == 1


def test_an_unknown_code_is_negatively_cached(monkeypatch):
    """MUTATION: cache only successful lookups -> red.

    Without negative caching, every unknown code costs a round trip on every
    message forever — the common case on a real import.
    """
    rec = _Recorder({"valid": False, "display": None})
    _install(monkeypatch, rec)
    for _ in range(5):
        terminology_resolver.resolve(ICD10, "ZZ.9")
    assert len(rec.calls) == 1


def test_lookups_are_capped_per_request(app, monkeypatch):
    """MUTATION: remove PER_REQUEST_MAX_LOOKUPS -> red.

    26 unknown codes on a cold cache must not become 26 round trips inside one
    patient-facing request.
    """
    rec = _Recorder({"valid": True, "display": "something"})
    _install(monkeypatch, rec)
    with app.test_request_context("/"):
        for i in range(terminology_resolver.PER_REQUEST_MAX_LOOKUPS + 6):
            terminology_resolver.resolve(ICD10, f"X{i}.0")
    assert len(rec.calls) == terminology_resolver.PER_REQUEST_MAX_LOOKUPS


def test_the_wall_clock_budget_stops_further_lookups(app, monkeypatch):
    """MUTATION: drop the deadline check -> red.

    A service that is up but slow is the dangerous case: the call-count cap
    alone still permits 8 x timeout inside one request.
    """
    monkeypatch.setattr(terminology_resolver, "PER_REQUEST_BUDGET_SECONDS", 0.05)
    rec = _Recorder({"valid": True, "display": "something"}, delay=0.06)
    _install(monkeypatch, rec)
    with app.test_request_context("/"):
        for i in range(6):
            terminology_resolver.resolve(ICD10, f"Y{i}.0")
    assert len(rec.calls) == 1


def test_a_budget_exhausted_code_is_not_cached_as_a_miss(app, monkeypatch):
    """Running out of budget is OUR limit, not the server's answer.

    Caching it would let one slow request blank a label permanently.
    """
    monkeypatch.setattr(terminology_resolver, "PER_REQUEST_MAX_LOOKUPS", 0)
    rec = _Recorder({"valid": True, "display": "Psoriasis, unspecified"})
    _install(monkeypatch, rec)
    with app.test_request_context("/"):
        assert terminology_resolver.resolve(ICD10, "L40.9") is None
    assert terminology_resolver.cache_size() == 0

    monkeypatch.setattr(terminology_resolver, "PER_REQUEST_MAX_LOOKUPS", 8)
    assert terminology_resolver.resolve(ICD10, "L40.9") == "Psoriasis, unspecified"


def test_the_budget_is_per_request_not_per_process(app, monkeypatch):
    """MUTATION: hold the budget in a module global -> red."""
    monkeypatch.setattr(terminology_resolver, "PER_REQUEST_MAX_LOOKUPS", 2)
    rec = _Recorder({"valid": True, "display": "something"})
    _install(monkeypatch, rec)
    for req in range(3):
        with app.test_request_context("/"):
            for i in range(4):
                terminology_resolver.resolve(ICD10, f"R{req}-{i}")
    assert len(rec.calls) == 6


# --- it cannot leak ------------------------------------------------------

def test_only_the_system_and_code_are_sent(monkeypatch):
    """The whole safety argument is that a code's meaning is not patient-specific.

    If anything else ever reaches the wire, that argument fails.
    """
    rec = _Recorder({"valid": True, "display": "Psoriasis, unspecified"})
    _install(monkeypatch, rec)
    terminology_resolver.resolve(SNOMED, "9014002")
    assert rec.calls == [(SNOMED, "9014002")]


def test_label_codings_still_refuses_to_carry_upstream_display(monkeypatch):
    """The resolver must not become a way for upstream text to survive.

    A coding arrives with a PHI-bearing display (redaction is what removes it);
    whatever ends up on the resource must come from the resolver, never from
    the value that was already there.
    """
    _install(monkeypatch, _Recorder({"valid": True, "display": "Psoriasis, unspecified"}))
    concept = {"coding": [{"system": ICD10, "code": "L40.9",
                           "display": "Psoriasis for Jane Secret"}]}
    terminology.label_codings(concept)
    assert concept["coding"][0]["display"] == "Psoriasis, unspecified"
    assert "Jane Secret" not in str(concept)


# ---------------------------------------------------------------------------
# The RxNorm path never returned a name (found live 2026-08-05).
#
# A patient asked "what medications am I on?" and their agent answered "I
# cannot read the specific names of these medications ... they've been
# redacted for your privacy." That sentence was true and the cause was ours:
# four Medication rows carried correct RxNorm codings, redaction stripped the
# upstream display as designed, and nothing put a label back.
#
# Two independent faults, either of which alone is enough to lose every drug
# name:
#
#   1. The request went to /REST/rxcui.json?rxcui=<code>. That endpoint
#      searches BY NAME (?name=); passing rxcui= is the wrong parameter and
#      RXNav answers HTTP 400 "Path or Query Parameter error". Verified against
#      the live service. It has done so since the call was written, so curatr's
#      RxNorm validation has never once succeeded either — it returned None,
#      which reads as "could not check", so nothing ever looked wrong.
#   2. Even on success the method hardcoded "display": None. It was written as
#      a VALIDITY checker for curatr's quality scan, where the question is
#      "is this a real code?", and terminology_resolver.resolve() reuses it
#      expecting a name it could never return.
#
# The fix uses /REST/rxcui/<code>/properties.json, which returns
# {"properties": {"name": "atorvastatin 40 MG Oral Tablet", ...}}, and reports
# an unknown code as {} with HTTP 200 — so absent properties means "not a
# concept", not "lookup failed".
# ---------------------------------------------------------------------------
class _RxResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _engine_with(monkeypatch, responses):
    """A CuratrEngine whose HTTP session replays `responses` by URL."""
    from r6.curatr import CuratrEngine

    engine = CuratrEngine()
    seen = []

    def fake_get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        for fragment, resp in responses.items():
            if fragment in url:
                return resp
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(engine._session, "get", fake_get)
    return engine, seen


def test_a_valid_rxcui_returns_the_drug_name(monkeypatch):
    """The whole point: a name the patient can read."""
    engine, seen = _engine_with(monkeypatch, {
        "/rxcui/617311/properties.json": _RxResp(
            200, {"properties": {"rxcui": "617311",
                                 "name": "atorvastatin 40 MG Oral Tablet"}}),
    })

    result = engine._lookup_code(
        "http://www.nlm.nih.gov/research/umls/rxnorm", "617311")

    assert result == {"valid": True,
                      "display": "atorvastatin 40 MG Oral Tablet",
                      "message": None}
    assert not any("rxcui.json" in url for url in seen), (
        "went back to the name-search endpoint, which answers HTTP 400 for a "
        "rxcui= parameter")


def test_rxnav_is_asked_for_json_not_fhir_json(monkeypatch):
    """The third fault, and the one no mock could have caught.

    CuratrEngine sets `Accept: application/fhir+json` once on the shared
    session, which is correct for tx.fhir.org. RXNav answers that Accept with
    HTTP 406 Not Acceptable. Found only by calling the live service — a mocked
    session has no opinion about headers, so every unit test passed while the
    real call returned None.
    """
    from r6.curatr import CuratrEngine

    engine = CuratrEngine()
    sent = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        sent["headers"] = headers or {}
        return _RxResp(200, {"properties": {"name": "atorvastatin 40 MG"}})

    monkeypatch.setattr(engine._session, "get", fake_get)
    engine._lookup_code("http://www.nlm.nih.gov/research/umls/rxnorm", "617311")

    assert sent["headers"].get("Accept") == "application/json", (
        "RXNav was asked with the session's FHIR Accept header, which it "
        "refuses with 406"
    )


def test_an_unknown_rxcui_is_reported_invalid_not_unavailable(monkeypatch):
    """RXNav answers 200 with {} for a code that is not a concept.

    That is an authoritative 'no', and it must not be collapsed into None —
    None means 'we could not check', which is what every RxNorm lookup has
    silently returned until now.
    """
    engine, _ = _engine_with(monkeypatch, {
        "/rxcui/99999999/properties.json": _RxResp(200, {}),
    })

    result = engine._lookup_code(
        "http://www.nlm.nih.gov/research/umls/rxnorm", "99999999")

    assert result is not None, "an authoritative 'not a concept' became 'unknown'"
    assert result["valid"] is False
    assert result["display"] is None


def test_a_transport_failure_stays_unavailable(monkeypatch):
    """A 404 or an exception is 'could not check', which is NOT 'invalid'.

    Same distinction as D2 in #344: only an authoritative verdict may be
    cached, so returning valid=False here would poison the label permanently.
    """
    engine, _ = _engine_with(monkeypatch, {
        "/rxcui/617311/properties.json": _RxResp(404, "Path or Query error"),
    })
    assert engine._lookup_code(
        "http://www.nlm.nih.gov/research/umls/rxnorm", "617311") is None


def test_the_resolver_now_produces_a_label_for_a_medication_code(monkeypatch):
    """End to end through resolve(), which is what the read path calls.

    MUTATION: restore `"display": None` in _validate_rxnorm. This goes red
    while every validity assertion above stays green — which is exactly how
    the defect survived: the code was 'working' at the only thing it was
    tested for.
    """
    from r6 import terminology_resolver as resolver

    engine, _ = _engine_with(monkeypatch, {
        "/rxcui/314076/properties.json": _RxResp(
            200, {"properties": {"name": "lisinopril 10 MG Oral Tablet"}}),
    })
    monkeypatch.setattr(resolver, "_engine", lambda: engine)
    monkeypatch.setenv("TERMINOLOGY_LOOKUP_ENABLED", "true")
    resolver.reset_cache()

    label = resolver.resolve(
        "http://www.nlm.nih.gov/research/umls/rxnorm", "314076")

    assert label == "lisinopril 10 MG Oral Tablet"
