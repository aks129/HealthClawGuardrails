#!/usr/bin/env python3
"""Run the same agent benchmark cases through the authenticated `claude` CLI.

Methodology note: the local-model benchmark uses Ollama's native tool-calling
API. The claude CLI does not accept custom tool schemas, so here the tool
schema is embedded in the prompt and the model is asked to reply with either
a JSON tool call or plain text. Scoring is identical. This slightly
disadvantages Claude (prompted JSON vs native tools); treat its score as a
floor, not a ceiling.

Usage: python3 benchmark_claude_cli.py [--model claude-sonnet-4-6]
"""

import argparse
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from benchmark_local_models import CASES, score  # noqa: E402

TOOL_PROTOCOL = """
You have access to these tools:
{tools}

If you decide to use a tool, reply with ONLY a JSON object on a single line:
{{"tool_call": {{"name": "<tool name>", "input": {{...arguments...}}}}}}
Otherwise reply with plain text (no JSON).
"""


def run_case(model, case):
    prompt_parts = [case["system"]]
    if case["tools"]:
        tools_desc = json.dumps(
            [
                {"name": t["name"], "description": t["description"], "schema": t["input_schema"]}
                for t in case["tools"]
            ],
            indent=1,
        )
        prompt_parts.append(TOOL_PROTOCOL.format(tools=tools_desc))
    prompt_parts.append("User message:\n" + case["messages"][0]["content"])
    prompt = "\n\n".join(prompt_parts)

    start = time.monotonic()
    proc = subprocess.run(
        ["claude", "-p", "--model", model, prompt],
        capture_output=True,
        text=True,
        timeout=180,
    )
    latency = time.monotonic() - start
    out = proc.stdout.strip()

    tool_calls = []
    text = out
    m = re.search(r'\{"tool_call":.*\}', out, re.DOTALL)
    if m:
        try:
            tc = json.loads(m.group(0))["tool_call"]
            tool_calls = [{"name": tc["name"], "input": tc.get("input", {})}]
            text = out[: m.start()] + out[m.end():]
        except (json.JSONDecodeError, KeyError):
            pass
    return {"text": text, "tool_calls": tool_calls, "latency_s": round(latency, 2), "tok_per_s": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-4-6")
    args = ap.parse_args()

    results = []
    for make in CASES:
        case = make()
        try:
            r = run_case(args.model, case)
            passed, detail = score(case["id"], r)
        except Exception as e:  # noqa: BLE001
            r = {"latency_s": None, "tok_per_s": None}
            passed, detail = False, f"error: {e}"
        row = {
            "case": case["id"],
            "pass": passed,
            "detail": detail,
            "latency_s": r["latency_s"],
        }
        results.append(row)
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {case['id']:<12} {detail:<40} {r['latency_s'] or '-'}s", file=sys.stderr)

    passed_n = sum(1 for r in results if r["pass"])
    lat = [r["latency_s"] for r in results if r["latency_s"] is not None]
    print(
        json.dumps(
            {
                "label": f"claude-cli:{args.model}",
                "score": f"{passed_n}/{len(results)}",
                "avg_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
