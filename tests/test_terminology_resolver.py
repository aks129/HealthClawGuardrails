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


def test_a_transient_failure_is_not_cached_but_a_verdict_is(monkeypatch):
    """MUTATION: cache every non-exception result -> red.

    curatr answers None for "the server did not answer" and a dict for an
    authoritative verdict. Caching the None would let a five-minute NLM
    outage permanently blank every code tried during it — for the life of
    the process, on the exact flaky public endpoints this will meet.
    """
    # Outage: not cached, so recovery is possible.
    rec = _Recorder(None)
    _install(monkeypatch, rec)
    assert terminology_resolver.resolve(ICD10, "L40.9") is None
    assert terminology_resolver.cache_size() == 0

    # Recovery on the next request: same code now resolves.
    _install(monkeypatch, _Recorder(
        {"valid": True, "display": "Psoriasis, unspecified"}))
    assert terminology_resolver.resolve(ICD10, "L40.9") == "Psoriasis, unspecified"

    # An authoritative "no such code" IS cached — that is a fact, not a
    # failure, and re-asking it every message is the round trip negative
    # caching exists to avoid.
    terminology_resolver.reset_cache()
    rec = _Recorder({"valid": False, "display": None})
    _install(monkeypatch, rec)
    terminology_resolver.resolve(ICD10, "ZZ.9")
    terminology_resolver.resolve(ICD10, "ZZ.9")
    assert rec.calls == [(ICD10, "ZZ.9")]
    assert terminology_resolver.cache_size() == 1


def test_an_exception_is_not_cached_either(monkeypatch):
    """A raised failure is as transient as a returned None."""
    _install(monkeypatch, _Recorder(raises=RuntimeError("tx down")))
    terminology_resolver.resolve(ICD10, "L40.9")
    assert terminology_resolver.cache_size() == 0


def test_the_resolver_engine_uses_the_short_timeout(monkeypatch):
    """MUTATION: build CuratrEngine() with the 5s default -> red.

    The wall-clock budget stops the NEXT lookup, not the one in flight, so
    the engine timeout is the true per-call bound on a patient read.
    """
    captured = {}

    class _Engine:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

    monkeypatch.setattr(terminology_resolver, "_ENGINE", None)
    monkeypatch.setattr(terminology_resolver._curatr_mod, "CuratrEngine", _Engine)
    terminology_resolver._engine()
    assert captured["timeout"] == terminology_resolver.RESOLVER_TIMEOUT_SECONDS
    assert terminology_resolver.RESOLVER_TIMEOUT_SECONDS <= 1.0
    monkeypatch.setattr(terminology_resolver, "_ENGINE", None)


def test_the_cache_cannot_grow_without_bound(monkeypatch):
    """MUTATION: remove the overflow clear -> red. The #339 shape."""
    monkeypatch.setattr(terminology_resolver, "CACHE_MAX_ENTRIES", 5)
    _install(monkeypatch, _Recorder({"valid": False, "display": None}))
    for i in range(12):
        terminology_resolver.resolve(ICD10, f"J{i}.0")
    assert terminology_resolver.cache_size() <= 5


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
