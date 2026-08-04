"""Guards for provider retry and failure classification.

Reported live on 2026-08-04. A patient asked "give me a timeline of my
cholesterol results", the run made eight tool calls, and then showed
"Something went wrong on our side." The recorded error class was LLMError and
the worker traceback was:

    careagents.llm.LLMError: model call failed (HTTP 429)

Nothing was wrong on our side. The provider rate limited one request, the
first attempt was the only attempt, and a transient 429 was presented to a
patient as a defect in their health records.
"""
from __future__ import annotations

import types

import pytest
import requests

from careagents import llm, worker


class _Cfg:
    provider = "openai"
    openai_base = "https://provider.invalid/v1"
    openai_api_key = "k"
    openai_model = "m"


class _Resp:
    def __init__(self, status, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {
            "choices": [{"message": {"content": "ok"}}]}

    def json(self):
        return self._payload


class _Post:
    """Replays a scripted sequence of responses/exceptions and counts calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def slept(monkeypatch):
    """Record backoff without spending it."""
    recorded: list[float] = []
    monkeypatch.setattr(llm._time_mod, "sleep", recorded.append)
    return recorded


def _complete(monkeypatch, post):
    monkeypatch.setattr(llm.requests, "post", post)
    return llm.complete(_Cfg(), "sys", [{"role": "user", "content": "hi"}], [])


# --- retry ---------------------------------------------------------------

def test_a_429_is_retried_and_can_succeed(monkeypatch, slept):
    """MUTATION: remove the retry loop -> red. This is the reported failure."""
    post = _Post(_Resp(429), _Resp(200))
    turn = _complete(monkeypatch, post)
    assert turn.text == "ok"
    assert post.calls == 2
    assert len(slept) == 1


def test_retries_are_bounded(monkeypatch, slept):
    """MUTATION: drop MAX_ATTEMPTS -> red.

    The run holds a lease and a hard deadline while this sleeps; an unbounded
    loop trades a visible error for an invisible timeout.
    """
    post = _Post(_Resp(429))
    with pytest.raises(llm.LLMRateLimited):
        _complete(monkeypatch, post)
    assert post.calls == llm.MAX_ATTEMPTS


def test_a_5xx_is_retried(monkeypatch, slept):
    post = _Post(_Resp(503), _Resp(200))
    assert _complete(monkeypatch, post).text == "ok"
    assert post.calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_is_not_retried(monkeypatch, slept, status):
    """MUTATION: retry every non-200 -> red.

    Retrying a malformed or unauthorised call just spends the deadline before
    failing identically.
    """
    post = _Post(_Resp(status))
    with pytest.raises(llm.LLMError) as caught:
        _complete(monkeypatch, post)
    assert not isinstance(caught.value, llm.LLMRateLimited)
    assert post.calls == 1
    assert slept == []


def test_a_connection_error_is_retried_then_wrapped(monkeypatch, slept):
    """MUTATION: delete the RequestException handler -> red.

    A read timeout previously escaped as a raw requests exception, so the
    worker recorded an error class that named the HTTP library rather than
    the failure.
    """
    post = _Post(requests.ConnectionError("reset"))
    with pytest.raises(llm.LLMError) as caught:
        _complete(monkeypatch, post)
    assert not isinstance(caught.value, llm.LLMRateLimited)
    assert post.calls == llm.MAX_ATTEMPTS


def test_a_connection_error_that_clears_succeeds(monkeypatch, slept):
    post = _Post(requests.Timeout("slow"), _Resp(200))
    assert _complete(monkeypatch, post).text == "ok"


# --- Retry-After ---------------------------------------------------------

def test_retry_after_is_honoured(monkeypatch, slept):
    post = _Post(_Resp(429, {"Retry-After": "2"}), _Resp(200))
    _complete(monkeypatch, post)
    assert slept == [2.0]


def test_an_unreasonable_retry_after_gives_up_instead_of_sleeping(
        monkeypatch, slept):
    """MUTATION: clamp instead of giving up -> red.

    If the provider wants longer than we can hold the lease, sleeping just
    means failing later. Fail now, honestly, and let the person re-ask.
    """
    post = _Post(_Resp(429, {"Retry-After": "600"}))
    with pytest.raises(llm.LLMRateLimited):
        _complete(monkeypatch, post)
    assert post.calls == 1
    assert slept == []


def test_a_garbage_retry_after_falls_back_to_backoff(monkeypatch, slept):
    post = _Post(_Resp(429, {"Retry-After": "soon"}), _Resp(200))
    _complete(monkeypatch, post)
    assert slept and slept[0] > 0


def test_backoff_grows_and_is_capped():
    delays = [llm._retry_delay(i, None) for i in range(8)]
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= llm.MAX_BACKOFF_SECONDS


# --- classification reaching the patient ---------------------------------

def test_the_patient_is_not_told_a_rate_limit_is_our_defect():
    """MUTATION: return the generic text for every exception -> red."""
    text = worker._failure_text(llm.LLMRateLimited("429"))
    assert text == worker.RATE_LIMITED_TEXT
    assert "went wrong" not in text.lower()
    assert "your records" in text.lower()


def test_a_real_defect_still_reads_as_a_defect():
    assert worker._failure_text(ValueError("boom")) == worker.GENERIC_FAILURE_TEXT
    assert worker._failure_text(llm.LLMError("HTTP 500")) == worker.GENERIC_FAILURE_TEXT


def test_rate_limited_is_an_llm_error_so_existing_handlers_still_catch_it():
    """Callers that only know LLMError must not start leaking a new type."""
    assert issubclass(llm.LLMRateLimited, llm.LLMError)


# --- the Anthropic path --------------------------------------------------

def test_anthropic_429_is_classified_without_a_second_retry_layer(monkeypatch):
    """The SDK already retries; what was missing is the classification.

    MUTATION: drop the status_code check -> red.
    """
    class _APIError(Exception):
        def __init__(self, status):
            super().__init__("rate limited")
            self.status_code = status

    fake = types.ModuleType("anthropic")
    fake.APIError = _APIError

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **_kw):
            raise _APIError(429)

    fake.Anthropic = _Client
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)

    cfg = types.SimpleNamespace(
        provider="anthropic", anthropic_api_key="k", anthropic_model="m",
        anthropic_oauth_token="")
    with pytest.raises(llm.LLMRateLimited):
        llm.complete(cfg, "sys", [{"role": "user", "content": "hi"}], [])
