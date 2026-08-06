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

from careagents import agent, llm, worker


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
    assert text == agent.RATE_LIMITED_TEXT
    assert "went wrong" not in text.lower()
    assert "your records" in text.lower()


def test_a_real_defect_still_reads_as_a_defect():
    assert worker._failure_text(ValueError("boom")) == agent.GENERIC_FAILURE_TEXT
    assert worker._failure_text(llm.LLMError("HTTP 500")) == agent.GENERIC_FAILURE_TEXT


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


# --- the synchronous path (run_turn) --------------------------------------
#
# The durable worker was fixed first, but the streamed browser chat and the
# SMS/iMessage collapse go through agent.run_turn — which yielded str(exc)
# as the error event's text. A patient on those surfaces saw the literal
# "model call failed (HTTP 429)".

def _turn_events(monkeypatch, exc):
    from careagents import agent

    def _boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(llm, "complete", _boom)
    return list(agent.run_turn(
        types.SimpleNamespace(), None, "t", "sys", [], "hi"))


def test_the_sync_path_does_not_leak_exception_internals(monkeypatch):
    """MUTATION: yield str(exc) again -> red."""
    events = _turn_events(monkeypatch, llm.LLMError("model call failed (HTTP 500)"))
    assert events == [{"type": "error", "text": agent.GENERIC_FAILURE_TEXT}]
    assert "HTTP" not in events[0]["text"]


def test_the_sync_path_tells_the_truth_about_a_rate_limit(monkeypatch):
    events = _turn_events(monkeypatch, llm.LLMRateLimited("HTTP 429"))
    assert events == [{"type": "error", "text": agent.RATE_LIMITED_TEXT}]


def test_the_failure_text_has_exactly_one_home():
    """MUTATION: re-add a local copy anywhere -> red.

    Four sites carried diverging literals of this string; one showed raw
    exception text. The constant lives in careagents/agent.py and everything
    else imports it.
    """
    import pathlib
    root = pathlib.Path(worker.__file__).parent
    owners = [p.name for p in root.glob("*.py")
              if "Something went wrong" in p.read_text(encoding="utf-8")]
    assert owners == ["agent.py"], (
        f"patient-facing failure text is defined in {owners}; only agent.py "
        "may own it — import GENERIC_FAILURE_TEXT instead")


# --- the call must not outlive the run waiting for it ----------------------
#
# A run holds a lease (60s) and a hard deadline (120s). The deadline is only
# checked BETWEEN calls (careagents/worker.py), and nothing cancels a call in
# flight, so any budget larger than the deadline is a worker slot pinned to a
# run that is already dead.

class _DeadlineCfg(_Cfg):
    run_deadline_seconds = 120


def test_the_openai_budget_fits_inside_the_run_deadline(monkeypatch, slept):
    """MUTATION: drop the per-attempt timeout back to a flat 90 -> red.

    3 attempts x 90s = 270s against a 120s deadline: the run is abandoned at
    120 and the slot stays busy for another two and a half minutes. Ran it,
    saw red.
    """
    seen: list[float] = []

    def post(*_args, **kwargs):
        seen.append(kwargs["timeout"])
        return _Resp(200)

    monkeypatch.setattr(llm.requests, "post", post)
    llm.complete(_DeadlineCfg(), "sys", [{"role": "user", "content": "hi"}], [])

    assert seen, "no call was made"
    budget = seen[0] * llm.MAX_ATTEMPTS + llm.MAX_BACKOFF_SECONDS * (
        llm.MAX_ATTEMPTS - 1)
    assert budget <= _DeadlineCfg.run_deadline_seconds, (
        f"worst-case model budget {budget}s exceeds the "
        f"{_DeadlineCfg.run_deadline_seconds}s run deadline")


def test_a_config_without_a_deadline_still_gets_a_bounded_timeout(
        monkeypatch, slept):
    """Config objects in the wild may predate the field; never fall back to
    the library default, which is unbounded in practice.

    MUTATION: return None when the attribute is missing -> red.
    """
    seen: list[float] = []

    def post(*_args, **kwargs):
        seen.append(kwargs["timeout"])
        return _Resp(200)

    monkeypatch.setattr(llm.requests, "post", post)
    llm.complete(_Cfg(), "sys", [{"role": "user", "content": "hi"}], [])
    assert seen and 0 < seen[0] <= 90


def test_the_anthropic_client_is_given_an_explicit_budget(monkeypatch):
    """The SDK default is 600s with 2 internal retries — 30 minutes of a
    worker slot for a run that is abandoned at 120 seconds. Nothing here
    cancels an in-flight call, so the budget IS the exposure.

    MUTATION: construct Anthropic() without timeout/max_retries -> red.
    Ran it, saw red.
    """
    captured: dict = {}

    class _Messages:
        def create(self, **_kw):
            raise AssertionError("not reached")

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.messages = _Messages()

    fake_sdk = types.SimpleNamespace(
        Anthropic=_Client, APIError=Exception,
        NOT_GIVEN=None)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_sdk)

    class _ACfg:
        provider = "anthropic"
        anthropic_api_key = "k"
        anthropic_model = "m"
        anthropic_oauth_token = ""
        run_deadline_seconds = 120

    with pytest.raises(Exception):
        llm.complete(_ACfg(), "sys", [{"role": "user", "content": "hi"}], [])

    assert "timeout" in captured, "the client was built without a timeout"
    assert "max_retries" in captured, "retries were left to the SDK default"
    budget = captured["timeout"] * (1 + captured["max_retries"])
    assert budget <= 120, f"worst-case Anthropic budget {budget}s exceeds 120s"


# --- one heartbeat blip is not a lost lease -------------------------------

class _FlakyHC:
    """Fails the first N heartbeats, then succeeds."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def heartbeat_agent_run(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise worker.HealthClawError("heartbeat failed", 0)
        return {"cancel_requested": False}


def _beat(hc, times=1):
    """Drive LeaseHeartbeat._loop's body without the thread or the sleep."""
    hb = worker.LeaseHeartbeat(hc, "run-1", "w-1", lease_seconds=60)
    for _ in range(times):
        if hb.lost:
            break
        hb._beat_once()
    return hb


def test_a_single_failed_heartbeat_does_not_abandon_the_run():
    """One 25s blip on a 20s interval aborted a turn the model may already
    have answered, and the patient read "something went wrong on our side".

    Recovery cost up to a full 60s lease expiry inside a 120s deadline, so the
    run usually died rather than being retried.

    MUTATION: set self.lost on the first failure -> red. Ran it, saw red.
    """
    hc = _FlakyHC(failures=1)
    hb = _beat(hc, times=2)
    assert hb.lost is False, "one failure ended the run"
    assert hc.calls == 2, "the heartbeat did not try again"


def test_consecutive_failures_still_lose_the_lease():
    """The tolerance must not become "never give up".

    If the engine is genuinely gone the run has to stop, or the worker keeps
    a slot busy for a lease nobody is honouring.

    MUTATION: never set self.lost -> red.
    """
    hc = _FlakyHC(failures=99)
    hb = _beat(hc, times=4)
    assert hb.lost is True, "the lease was never given up"
