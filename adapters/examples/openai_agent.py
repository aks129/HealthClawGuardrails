"""Minimal example: HealthClaw tools on the OpenAI Chat Completions tool loop.

Requires: `pip install openai` and OPENAI_API_KEY. This is a reference example
(not run in CI). The guardrails are enforced by HealthClaw server-side.

    python adapters/examples/openai_agent.py \
        --mcp-base https://mcp-server-production-5112.up.railway.app \
        --tenant desktop-demo --prompt "Summarize the patient's recent vitals"
"""
import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("/adapters/", 1)[0])
from adapters.healthclaw_bridge import (  # noqa: E402
    load_manifest, to_openai_tools, HealthClawClient,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-base", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--step-up-token")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default="gpt-4o")
    args = ap.parse_args()

    from openai import OpenAI
    oai = OpenAI()
    hc = HealthClawClient(args.mcp_base, args.tenant, args.step_up_token,
                          agent_id="openai-example")
    tools = to_openai_tools(load_manifest())
    messages = [{"role": "user", "content": args.prompt}]

    for _ in range(6):  # bounded tool loop
        resp = oai.chat.completions.create(model=args.model, messages=messages,
                                           tools=tools)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            print(msg.content)
            return
        for tc in msg.tool_calls:
            result = hc.call(tc.function.name,
                             json.loads(tc.function.arguments or "{}"))
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)[:12000]})


if __name__ == "__main__":
    main()
