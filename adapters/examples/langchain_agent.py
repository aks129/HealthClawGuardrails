"""Minimal example: HealthClaw tools on a LangChain tool-calling loop.

Requires: `pip install "langchain-core>=1.0" langchain-openai` (v1 accepts a
JSON-Schema dict as args_schema directly) and OPENAI_API_KEY. Reference
example (not run in CI). Guardrails enforced by HealthClaw server-side.

    python adapters/examples/langchain_agent.py \
        --mcp-base https://mcp-server-production-5112.up.railway.app \
        --tenant desktop-demo --prompt "List the patient's active conditions"
"""
import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("/adapters/", 1)[0])
from adapters.healthclaw_bridge import (  # noqa: E402
    load_manifest, HealthClawClient,
)


def to_langchain_tools(manifest, hc):
    """Manifest -> LangChain StructuredTools relaying through HealthClawClient."""
    from langchain_core.tools import StructuredTool
    tools = []
    for t in (manifest["tools"] if isinstance(manifest, dict) else manifest):
        def _run(_name=t["name"], **kwargs):
            return json.dumps(hc.call(_name, kwargs))[:12000]
        tools.append(StructuredTool.from_function(
            func=_run, name=t["name"], description=t.get("description", ""),
            args_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
        ))
    return tools


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-base", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--step-up-token")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default="gpt-4o")
    args = ap.parse_args()

    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_openai import ChatOpenAI

    hc = HealthClawClient(args.mcp_base, args.tenant, args.step_up_token,
                          agent_id="langchain-example")
    tools = to_langchain_tools(load_manifest(), hc)
    tool_map = {t.name: t for t in tools}
    llm = ChatOpenAI(model=args.model).bind_tools(tools)
    messages = [HumanMessage(args.prompt)]

    for _ in range(6):  # bounded tool loop
        ai = llm.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            print(ai.content)
            return
        for tc in ai.tool_calls:
            tool = tool_map.get(tc["name"])
            result = (tool.invoke(tc["args"]) if tool
                      else f"error: unknown tool {tc['name']}")
            messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))


if __name__ == "__main__":
    main()
