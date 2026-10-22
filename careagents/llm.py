"""Provider adapter for the agent loop.

One call shape: complete(system, messages, tools) -> LLMTurn. Anthropic (the
preferred provider, used whenever ANTHROPIC_API_KEY is set) via the official
SDK; otherwise an OpenAI-compatible chat-completions fallback over plain HTTP
so v1 works before an Anthropic key is provisioned.

Both paths are synchronous per model turn; streaming to the browser happens at
the event level in agent.py (tool chips appear live between rounds). Messages
use a neutral internal shape:

    {"role": "user"|"assistant", "content": str}                    # text
    {"role": "assistant", "tool_calls": [{"id","name","arguments"}]}
    {"role": "tool", "tool_call_id": str, "content": str}

Tools use a neutral shape: {"name", "description", "parameters": JSONSchema}.
"""

from __future__ import annotations

import json
import logging
import time as _time_mod
from dataclasses import dataclass, field

import requests

from careagents import routing

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-native tool_call objects, replayed verbatim on the next turn.
    # Some OpenAI-compatible backends (Gemini) require echoing opaque fields
    # like thought_signature that a reconstructed call would drop.
    raw_tool_calls: list = field(default_factory=list)
    # Anthropic only: for routing, the log line and the daily counts.
    model: str = ""
    tier: int = 0
    escalated: bool = False
    advisor_calls: int = 0
    advisor_errors: int = 0
    advisor_timed_out: bool = False
    # The advised attempt failed (timeout, 5xx, connection) and the call was
    # answered without it. advisor_calls is then 1: the turn's consultation
    # is spent.
    advisor_failed: bool = False
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    advisor_output_tokens: int = 0


class LLMError(RuntimeError):
    """A model call failed. `status` is the provider's HTTP status, 0 when
    there was none; with the class, it is all careagents/causes.py reads."""

    def __init__(self, message: str = "", status: int | None = 0):
        super().__init__(message)
        self.status = status or 0


class LLMTransient(LLMError):
    """A failure the same request may not repeat: 5xx, 529 overloaded, or
    a dropped connection, after whatever retries the call was given."""


class LLMTimeout(LLMTransient):
    """The provider did not answer within this call's budget."""


class LLMRateLimited(LLMError):
    """The provider asked us to slow down, and backing off did not clear it.

    Separate from LLMError because the honest thing to tell a patient differs.
    A bug on our side is "something went wrong"; being rate limited is "we are
    busy, ask again in a moment" — which is true, actionable, and does not
    invite them to report a defect that does not exist.
    """


class LLMOutOfCredit(LLMError):
    """The provider refused on billing: the key is out of credit or quota.

    Not a rate limit, although OpenAI sends it as a 429. "Ask again in a
    moment" is false here — nothing clears until an operator tops the account
    up — so the patient is told the assistant is unavailable, and the
    operator learns why from the log. Retrying only spends the run's deadline.
    """


class ModelHostNotApproved(LLMError):
    """A real-record turn was refused before any model call: the configured
    model host is not one approved for real records. Raised by the worker,
    never by a provider. Like a refused key, only an operator can clear it."""


def _error_fields(body) -> tuple[str, str, str]:
    """(type, code, message) from a provider error body, '' when absent.

    OpenAI and Anthropic both nest it under `error`. Only the first two are
    ever logged; the message is read for classification and nowhere else.
    """
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", "", ""
    return (str(err.get("type") or ""), str(err.get("code") or ""),
            str(err.get("message") or ""))


def _log_out_of_credit(provider: str, status, err_type: str, code: str) -> None:
    # For the operator only. Status and the provider's error type/code say
    # which account to fix; the request itself never enters the log.
    logger.error("model provider %s refused on billing (HTTP %s, type=%s, "
                 "code=%s): top up or rotate the key", provider, status,
                 err_type or "-", code or "-")


def _openai_out_of_credit(r) -> bool:
    """402, or OpenAI's `insufficient_quota` 429 — never a plain 429."""
    if r.status_code not in (402, 429):
        return False
    try:
        err_type, code, _msg = _error_fields(r.json())
    except ValueError:
        err_type = code = ""
    if r.status_code == 402 or "insufficient_quota" in (err_type, code):
        _log_out_of_credit("openai-compatible", r.status_code, err_type, code)
        return True
    return False


def _anthropic_out_of_credit(exc) -> bool:
    """402 `billing_error`, or the 400 "credit balance is too low"."""
    status = getattr(exc, "status_code", None)
    err_type, code, message = _error_fields(getattr(exc, "body", None))
    if (status == 402 or err_type == "billing_error"
            or (status == 400 and "credit balance" in message.lower())):
        _log_out_of_credit("anthropic", status, err_type, code)
        return True
    return False


# Transient by definition: the same request may succeed unchanged. 400/401/403
# are deliberately absent — retrying a malformed or unauthorised call just
# spends the run's deadline before failing identically.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Bounded on purpose. The run holds a lease and a hard deadline while this
# sleeps, so an unbounded retry loop would trade a visible error for an
# invisible timeout.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 8.0

#: The Anthropic SDK retries 429/5xx itself; this is that count made explicit
#: so the timeout below can be divided by it.
_ANTHROPIC_MAX_RETRIES = 2


#: Used when a Config predates `run_deadline_seconds`. Never fall through to a
#: library default: the Anthropic SDK's is 600 seconds, which is five times the
#: deadline the run is measured against.
_FALLBACK_DEADLINE_SECONDS = 120.0

#: Leave the run somewhere to land. The deadline is checked BETWEEN calls
#: (careagents/worker.py) and nothing cancels a call in flight, so a model
#: budget equal to the deadline guarantees the check happens after it has
#: already passed.
_DEADLINE_SHARE = 0.75


def _attempt_timeout(cfg, attempts: int) -> float:
    """Seconds one model call may take, so `attempts` of them still fit.

    The run holds a lease and a hard deadline while this is in flight. A
    budget larger than the deadline does not make the call succeed; it pins a
    worker slot to a run that has already been abandoned, which is how one
    slow provider turns into no capacity at all.
    """
    deadline = float(getattr(cfg, "run_deadline_seconds", 0)
                     or _FALLBACK_DEADLINE_SECONDS)
    backoff = MAX_BACKOFF_SECONDS * max(0, attempts - 1)
    return max(5.0, (deadline * _DEADLINE_SHARE - backoff) / attempts)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before attempt N+1. Honours Retry-After when sane."""
    if retry_after:
        try:
            supplied = float(retry_after)
        except (TypeError, ValueError):
            supplied = -1.0
        if 0 <= supplied <= MAX_BACKOFF_SECONDS:
            return supplied
        if supplied > MAX_BACKOFF_SECONDS:
            # The provider wants longer than we can hold the lease for. Give
            # up now rather than sleep past the deadline and fail anyway.
            return -1.0
    return min(BACKOFF_BASE_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)


def complete(cfg, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
    if cfg.provider == "anthropic":
        return _anthropic_complete(cfg, system, messages, tools)
    return _openai_complete(cfg, system, messages, tools)


# --- Anthropic (preferred) -------------------------------------------------

def _anthropic_complete(cfg, system, messages, tools) -> LLMTurn:
    import anthropic

    # The SDK defaults to a 600s timeout and 2 internal retries — up to half an
    # hour of a worker slot for a run abandoned at 120 seconds. Nothing here
    # cancels an in-flight call, so this budget IS the exposure. Both are set
    # explicitly; the retries stay, because the SDK is the only thing that can
    # retry an Anthropic call, but they now fit inside the deadline.
    _budget = {"timeout": _attempt_timeout(cfg, 1 + _ANTHROPIC_MAX_RETRIES),
               "max_retries": _ANTHROPIC_MAX_RETRIES}
    a_tools = [{"name": t["name"], "description": t["description"],
                "input_schema": t["parameters"]} for t in tools]
    a_messages = _to_anthropic_messages(messages)
    max_tokens = getattr(cfg, "anthropic_max_tokens", 8000)
    effort = getattr(cfg, "anthropic_effort", "medium")

    if not getattr(cfg, "routing", False):
        # CARE_MODEL for every turn, exactly as before routing existed: no
        # betas, no advisor, no log line and no counts (#931 F4).
        return _anthropic_call(
            anthropic, _anthropic_client(anthropic, cfg, **_budget).messages,
            model=cfg.anthropic_model, max_tokens=max_tokens,
            output_config={"effort": effort},
            system=system, messages=a_messages, tools=a_tools)

    # CARE_ROUTING=on (careagents/routing.py).
    started = _time_mod.monotonic()
    tier = routing.tier_for(messages)
    escalated = advisor_timed_out = advisor_failed = False
    if tier == routing.TIER_HAIKU:
        # Haiku sees the current turn only. An earlier answer may hold the
        # person's results, and small talk does not need them (#931 F2).
        start = routing.turn_start(messages) or 0
        # No fallbacks: Haiku 5.5 has no server-side fallback, and a list of
        # fallback models is a 400 there.
        t0 = _time_mod.monotonic()
        turn = _anthropic_call(
            anthropic, _anthropic_client(anthropic, cfg, **_budget).messages,
            model=routing.HAIKU_MODEL, max_tokens=max_tokens,
            output_config={"effort": "low"},
            system=routing.HAIKU_CARD + system,
            messages=_to_anthropic_messages(messages[start:]), tools=a_tools)
        if (any(c.name in routing.CLINICAL_TOOLS for c in turn.tool_calls)
                or routing.haiku_passed(turn.text)):
            # The turn was clinical after all, or not on Haiku's card (#935).
            # Haiku's reply is discarded unexecuted, so its tool call never
            # reaches the history.
            tier, escalated = routing.TIER_SONNET, True
        _log_call("haiku", turn, t0, "discarded" if escalated else "ok")
    if tier == routing.TIER_SONNET:
        sonnet = {"model": routing.SONNET_MODEL, "max_tokens": max_tokens,
                  "output_config": {"effort": effort},
                  "messages": a_messages,
                  "extra_body": {"fallbacks": "default"}}
        # A per-request anthropic-beta header REPLACES the client's default
        # one in the SDK, so the OAuth beta has to be in the list (#931 F3).
        oauth = ([cfg.anthropic_oauth_beta]
                 if getattr(cfg, "anthropic_oauth_token", "") else [])
        turn = None
        if (getattr(cfg, "advisor", False)
                and not routing.advisor_used(messages)):
            # Offered until the turn has consulted once, the no-tools final
            # call included. The history we send never carries advisor blocks
            # (it is rebuilt provider-neutrally), which the API accepts with
            # the advisor still offered (live, 2026-10-12).
            #
            # One attempt with most of the deadline, no SDK retries: a
            # consultation runs well past the ordinary per-attempt budget,
            # and a retry would buy the Opus consultation again (#931 F6).
            client = _anthropic_client(anthropic, cfg,
                                       timeout=_advised_timeout(cfg),
                                       max_retries=0)
            t0 = _time_mod.monotonic()
            try:
                turn = _anthropic_call(
                    anthropic, client.beta.messages,
                    system=system + routing.ADVISOR_NOTE,
                    tools=[*a_tools, routing.ADVISOR_TOOL],
                    betas=[*oauth, routing.ADVISOR_BETA,
                           routing.FALLBACK_BETA], **sonnet)
                _log_call("advised", turn, t0, "ok")
            except LLMTransient as exc:
                # A timeout, a 5xx/529 or a dropped connection: with no SDK
                # retries on this call, answer without the advisor rather
                # than fail the turn (#931 round two R2), in the share of
                # the deadline kept back for it.
                advisor_failed = True
                advisor_timed_out = isinstance(exc, LLMTimeout)
                _log_call("advised", None, t0, type(exc.__cause__).__name__)
        if turn is None:
            budget = _fallback_budget(cfg) if advisor_failed else _budget
            spent = (getattr(cfg, "advisor", False)
                     and (advisor_failed or routing.advisor_used(messages)))
            t0 = _time_mod.monotonic()
            turn = _anthropic_call(
                anthropic,
                _anthropic_client(anthropic, cfg, **budget).beta.messages,
                system=system + (routing.ADVISOR_SPENT_NOTE if spent else ""),
                tools=a_tools,
                betas=[*oauth, routing.FALLBACK_BETA], **sonnet)
            _log_call("fallback" if advisor_failed else "sonnet", turn, t0,
                      "ok")
    turn.tier, turn.escalated = tier, escalated
    turn.advisor_timed_out = advisor_timed_out
    turn.advisor_failed = advisor_failed
    if advisor_failed:
        # The failed attempt is the turn's one consultation: later rounds
        # are not offered the advisor again, with a fresh budget, after this
        # round already spent most of its share (#931 round two R3). The
        # callers record advisor_calls in the history and checkpoints.
        turn.advisor_calls = max(turn.advisor_calls, 1)
    _observe(turn, tier, started)
    return turn


def _anthropic_client(anthropic, cfg, *, timeout: float, max_retries: int):
    # An OAuth access token (Claude subscription / OpenClaw) authenticates as a
    # Bearer token with the oauth beta header, instead of an x-api-key. The
    # token is short-lived — refresh it out of band when it expires.
    if getattr(cfg, "anthropic_oauth_token", ""):
        return anthropic.Anthropic(
            auth_token=cfg.anthropic_oauth_token,
            default_headers={"anthropic-beta": cfg.anthropic_oauth_beta},
            timeout=timeout, max_retries=max_retries, **_base_url(cfg))
    return anthropic.Anthropic(api_key=cfg.anthropic_api_key,
                               timeout=timeout, max_retries=max_retries,
                               **_base_url(cfg))


def _base_url(cfg) -> dict:
    """The validated Anthropic base URL from config (config.model_endpoint),
    passed explicitly so the SDK never reads ANTHROPIC_BASE_URL itself: the
    host the model-host gate approved is the host called."""
    url = getattr(cfg, "anthropic_base_url", "")
    return {"base_url": url} if url else {}


def _deadline(cfg) -> float:
    return float(getattr(cfg, "run_deadline_seconds", 0)
                 or _FALLBACK_DEADLINE_SECONDS)


#: Of a call's share of the deadline (90s at the default 120s), what the
#: advised attempt may take: 54s. The other 36s are kept for answering
#: without the advisor if it fails, as two attempts of 18s, so a failed
#: advised call, its fallback and one ordinary tool round (24.7s) still
#: fit inside 120s (#931 round two R3). A one-consult call measured
#: 13-29s live (#931 F6 probes); max_uses is 1.
_ADVISED_SHARE = 0.6
_FALLBACK_RETRIES = 1


def _advised_timeout(cfg) -> float:
    return max(5.0, _deadline(cfg) * _DEADLINE_SHARE * _ADVISED_SHARE)


def _fallback_budget(cfg) -> dict:
    """The kept-back share for answering without a failed advisor, split
    over the attempts the SDK may make (it retries 5xx, 529, connection
    errors and timeouts)."""
    reserve = _deadline(cfg) * _DEADLINE_SHARE - _advised_timeout(cfg)
    return {"timeout": max(5.0, reserve / (1 + _FALLBACK_RETRIES)),
            "max_retries": _FALLBACK_RETRIES}


def _anthropic_call(anthropic, endpoint, **kw) -> LLMTurn:
    try:
        resp = endpoint.create(**kw)
    except anthropic.APIError as exc:  # surface a category, not internals
        timeout_cls = getattr(anthropic, "APITimeoutError", None)
        if timeout_cls is not None and isinstance(exc, timeout_cls):
            raise LLMTimeout("model call timed out") from exc
        # No retry loop here on purpose: the Anthropic SDK already retries
        # 429 and 5xx internally (max_retries), so wrapping it in another
        # would multiply the delay while the run holds its lease. What was
        # missing is the CLASSIFICATION — a rate limit reaching here has
        # already been retried and deserves its own honest message.
        if _anthropic_out_of_credit(exc):
            raise LLMOutOfCredit("model call refused on billing") from exc
        if getattr(exc, "status_code", None) == 429:
            raise LLMRateLimited("model call rate limited (HTTP 429)") from exc
        conn_cls = getattr(anthropic, "APIConnectionError", None)
        if ((getattr(exc, "status_code", None) or 0) >= 500
                or (conn_cls is not None and isinstance(exc, conn_cls))):
            raise LLMTransient(
                f"model call failed ({type(exc).__name__})") from exc
        raise LLMError(f"model call failed ({type(exc).__name__})",
                       getattr(exc, "status_code", 0)) from exc

    turn = LLMTurn(model=kw["model"])
    for block in resp.content:
        if block.type == "text":
            turn.text += block.text
        elif block.type == "tool_use":
            turn.tool_calls.append(ToolCall(
                id=block.id, name=block.name, arguments=dict(block.input)))
        elif block.type == "advisor_tool_result":
            # Opus advice is encrypted (advisor_redacted_result) and is
            # never read. Text before it is the executor announcing that it
            # will consult someone, which the person should not see.
            content = getattr(block, "content", None)
            kind = (content.get("type") if isinstance(content, dict)
                    else getattr(content, "type", None))
            if kind == "advisor_tool_result_error":
                turn.advisor_errors += 1
            else:
                turn.advisor_calls += 1
            turn.text = ""
    stop = getattr(resp, "stop_reason", None)
    turn.stop_reason = stop or ""
    usage = getattr(resp, "usage", None)
    turn.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    turn.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    for it in getattr(usage, "iterations", None) or []:
        get = it.get if isinstance(it, dict) else (
            lambda k, _it=it: getattr(_it, k, None))
        if get("type") == "advisor_message":
            turn.advisor_output_tokens += int(get("output_tokens") or 0)
    if stop == "refusal" and not turn.text.strip():
        # A safety decline arrives as HTTP 200 with no text; an empty
        # bubble reads as a broken app.
        turn.text = REFUSAL_TEXT
    elif stop == "max_tokens" and not turn.tool_calls:
        # Cut off mid-answer: say so instead of ending mid-sentence.
        turn.text = turn.text.rstrip() + "\n\n" + CUT_OFF_TEXT
    return turn


def _log_call(kind: str, turn: LLMTurn | None, t0: float,
              outcome: str) -> None:
    """One line per API call, the discarded Haiku call and a failed advised
    attempt included, so escalation and fallback cost can be read from the
    logs (QA #931 Q3). Never content: kind, model, outcome, stop reason,
    seconds and token counts only."""
    logger.info(
        "model call kind=%s model=%s outcome=%s stop=%s secs=%.1f in=%d "
        "out=%d advisor=%d advisor_out=%d", kind,
        turn.model if turn else "-", outcome,
        (turn.stop_reason if turn else "") or "-",
        _time_mod.monotonic() - t0, turn.input_tokens if turn else 0,
        turn.output_tokens if turn else 0, turn.advisor_calls if turn else 0,
        turn.advisor_output_tokens if turn else 0)


def _observe(turn: LLMTurn, tier, started: float) -> None:
    """The day's counts, once per routed llm.complete. Never content."""
    name = "haiku" if tier == routing.TIER_HAIKU else "sonnet"
    # A call that answers (no tool calls) ends the turn: count it once there.
    answered = not turn.tool_calls
    routing.count({
        f"turns_{name}": answered,
        "escalations": turn.escalated,
        # A failed advised attempt spends the turn's consultation without
        # being one; it is counted as an error (or a timeout) instead.
        "advisor_consults": 0 if turn.advisor_failed else turn.advisor_calls,
        "advisor_errors": turn.advisor_errors + (
            turn.advisor_failed and not turn.advisor_timed_out),
        "advisor_timeouts": turn.advisor_timed_out,
        "refusals": turn.stop_reason == "refusal",
        "cut_offs": turn.stop_reason == "max_tokens",
    })


REFUSAL_TEXT = ("I can't help with that one. Try asking it another way, "
                "or ask your doctor.")
CUT_OFF_TEXT = "(That ran long and was cut off. Ask me to continue.)"


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m["tool_call_id"],
                "content": m["content"]}]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for c in m["tool_calls"]:
                content.append({"type": "tool_use", "id": c["id"],
                                "name": c["name"], "input": c["arguments"]})
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


# --- OpenAI-compatible fallback ---------------------------------------------

def _openai_complete(cfg, system, messages, tools) -> LLMTurn:
    o_tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["parameters"]}} for t in tools]
    o_messages = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            o_messages.append({"role": "tool",
                               "tool_call_id": m["tool_call_id"],
                               "content": m["content"]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            # Replay the provider's exact tool_call objects when we captured
            # them (preserves Gemini's thought_signature); else reconstruct.
            raw = m.get("_openai_tool_calls")
            tool_calls = raw or [{
                "id": c["id"], "type": "function",
                "function": {"name": c["name"],
                             "arguments": json.dumps(c["arguments"])}}
                for c in m["tool_calls"]]
            o_messages.append({"role": "assistant",
                               "content": m.get("content") or None,
                               "tool_calls": tool_calls})
        else:
            o_messages.append({"role": m["role"], "content": m["content"]})

    r = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = requests.post(
                f"{cfg.openai_base}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
                # Generous budget: some OpenAI-compatible backends (e.g.
                # Gemini's compat endpoint) spend completion tokens on
                # internal reasoning before the visible answer.
                json={"model": cfg.openai_model, "messages": o_messages,
                      "tools": o_tools, "max_tokens": 4000},
                timeout=_attempt_timeout(cfg, MAX_ATTEMPTS))
        except requests.RequestException as exc:
            # Previously uncaught: a connection reset or read timeout escaped
            # as a raw requests exception rather than an LLMError, so the
            # worker recorded an unhelpful error class.
            if attempt + 1 >= MAX_ATTEMPTS:
                raise LLMError(
                    f"model call failed ({type(exc).__name__})") from exc
            _time_mod.sleep(_retry_delay(attempt, None))
            continue

        if r.status_code == 200:
            break
        # Before the retry decision: an exhausted quota arrives as a 429,
        # and backing off from it only burns the run's deadline.
        if _openai_out_of_credit(r):
            raise LLMOutOfCredit("model call refused on billing")
        if r.status_code not in RETRYABLE_STATUSES:
            raise LLMError(f"model call failed (HTTP {r.status_code})",
                           r.status_code)
        delay = _retry_delay(attempt, r.headers.get("Retry-After"))
        if delay < 0 or attempt + 1 >= MAX_ATTEMPTS:
            break
        _time_mod.sleep(delay)

    if r is None or r.status_code != 200:
        status = r.status_code if r is not None else None
        if status == 429:
            raise LLMRateLimited("model call rate limited (HTTP 429)")
        raise LLMError(f"model call failed (HTTP {status})", status)
    msg = r.json()["choices"][0]["message"]

    turn = LLMTurn(text=msg.get("content") or "",
                   raw_tool_calls=msg.get("tool_calls") or [])
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        turn.tool_calls.append(ToolCall(
            id=c["id"], name=c["function"]["name"], arguments=args))
    if msg.get("refusal"):
        # OpenAI's decline arrives as `message.refusal`, with no content.
        # The same as Anthropic's stop_reason "refusal": our fixed sentence,
        # never the provider's wording, and the worker adds the 911 line.
        turn.stop_reason = "refusal"
        if not turn.text.strip():
            turn.text = REFUSAL_TEXT
    return turn
