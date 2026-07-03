"""Minimal example: HealthClaw tools on Google Gemini function-calling.

Requires: `pip install google-genai` and GEMINI_API_KEY. Reference example (not
run in CI). Guardrails enforced by HealthClaw server-side.

    python adapters/examples/gemini_agent.py \
        --mcp-base https://mcp-server-production-5112.up.railway.app \
        --tenant desktop-demo --prompt "List the patient's active conditions"
"""
import argparse
import sys

sys.path.insert(0, __file__.rsplit("/adapters/", 1)[0])
from adapters.healthclaw_bridge import (  # noqa: E402
    load_manifest, to_gemini_declarations, HealthClawClient,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-base", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--step-up-token")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    args = ap.parse_args()

    from google import genai
    from google.genai import types

    hc = HealthClawClient(args.mcp_base, args.tenant, args.step_up_token,
                          agent_id="gemini-example")
    tool = types.Tool(function_declarations=to_gemini_declarations(load_manifest()))
    client = genai.Client()
    contents = [types.Content(role="user",
                              parts=[types.Part(text=args.prompt)])]

    for _ in range(6):
        resp = client.models.generate_content(
            model=args.model, contents=contents,
            config=types.GenerateContentConfig(tools=[tool]))
        parts = resp.candidates[0].content.parts
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        contents.append(resp.candidates[0].content)
        if not calls:
            print(resp.text)
            return
        responses = []
        for call in calls:
            result = hc.call(call.name, dict(call.args or {}))
            responses.append(types.Part.from_function_response(
                name=call.name, response={"result": result}))
        contents.append(types.Content(role="user", parts=responses))


if __name__ == "__main__":
    main()
