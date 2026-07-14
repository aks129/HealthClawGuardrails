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


def complete(cfg, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
    if cfg.provider == "anthropic":
        return _anthropic_complete(cfg, system, messages, tools)
    return _openai_complete(cfg, system, messages, tools)


# --- Anthropic (preferred) -------------------------------------------------

def _anthropic_complete(cfg, system, messages, tools) -> LLMTurn:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    a_tools = [{"name": t["name"], "description": t["description"],
                "input_schema": t["parameters"]} for t in tools]
    a_messages = _to_anthropic_messages(messages)
    try:
        resp = client.messages.create(
            model=cfg.anthropic_model, max_tokens=1200, system=system,
            messages=a_messages, tools=a_tools)
    except anthropic.APIError as exc:  # surface a category, not internals
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

    r = requests.post(
        f"{cfg.openai_base}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
        # Generous budget: some OpenAI-compatible backends (e.g. Gemini's
        # compat endpoint) spend completion tokens on internal reasoning
        # before the visible answer.
        json={"model": cfg.openai_model, "messages": o_messages,
              "tools": o_tools, "max_tokens": 4000},
        timeout=90)
    if r.status_code != 200:
        raise LLMError(f"model call failed (HTTP {r.status_code})")
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
