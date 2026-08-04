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
import time as _time_mod
from dataclasses import dataclass, field

import requests


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


class LLMError(RuntimeError):
    pass


class LLMRateLimited(LLMError):
    """The provider asked us to slow down, and backing off did not clear it.

    Separate from LLMError because the honest thing to tell a patient differs.
    A bug on our side is "something went wrong"; being rate limited is "we are
    busy, ask again in a moment" — which is true, actionable, and does not
    invite them to report a defect that does not exist.
    """


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

    # An OAuth access token (Claude subscription / OpenClaw) authenticates as a
    # Bearer token with the oauth beta header, instead of an x-api-key. The
    # token is short-lived — refresh it out of band when it expires.
    if getattr(cfg, "anthropic_oauth_token", ""):
        client = anthropic.Anthropic(
            auth_token=cfg.anthropic_oauth_token,
            default_headers={"anthropic-beta": cfg.anthropic_oauth_beta})
    else:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    a_tools = [{"name": t["name"], "description": t["description"],
                "input_schema": t["parameters"]} for t in tools]
    a_messages = _to_anthropic_messages(messages)
    try:
        resp = client.messages.create(
            model=cfg.anthropic_model, max_tokens=1200, system=system,
            messages=a_messages, tools=a_tools)
    except anthropic.APIError as exc:  # surface a category, not internals
        # No retry loop here on purpose: the Anthropic SDK already retries
        # 429 and 5xx internally (max_retries), so wrapping it in another
        # would multiply the delay while the run holds its lease. What was
        # missing is the CLASSIFICATION — a rate limit reaching here has
        # already been retried and deserves its own honest message.
        if getattr(exc, "status_code", None) == 429:
            raise LLMRateLimited("model call rate limited (HTTP 429)") from exc
        raise LLMError(f"model call failed ({type(exc).__name__})") from exc

    turn = LLMTurn()
    for block in resp.content:
        if block.type == "text":
            turn.text += block.text
        elif block.type == "tool_use":
            turn.tool_calls.append(ToolCall(
                id=block.id, name=block.name, arguments=dict(block.input)))
    return turn


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
                timeout=90)
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
        if r.status_code not in RETRYABLE_STATUSES:
            raise LLMError(f"model call failed (HTTP {r.status_code})")
        delay = _retry_delay(attempt, r.headers.get("Retry-After"))
        if delay < 0 or attempt + 1 >= MAX_ATTEMPTS:
            break
        _time_mod.sleep(delay)

    if r is None or r.status_code != 200:
        status = r.status_code if r is not None else None
        if status == 429:
            raise LLMRateLimited("model call rate limited (HTTP 429)")
        raise LLMError(f"model call failed (HTTP {status})")
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
    return turn
