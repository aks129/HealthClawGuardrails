"""Guard: the tool list a reader is handed must match the tools that exist.

`docs/quickstarts/mcp-generic.md` named `fhir_get_token` and `fhir_seed` as
things you can call. Both are in `PRIVILEGED_TOOL_NAMES` and are withheld from
network transports, so no hosted client has ever been able to see them. The
page also advertised "28 tools" while the deployed server serves 27 and the
catalogue defines 29 — three numbers, no two agreeing.

That is not pedantry about a count. A reader who believes `fhir_get_token`
exists will write an agent that calls it, and the failure arrives at runtime
in someone else's client.

Source of truth is `adapters/tools.manifest.json`, which is generated from the
same catalogue the server serves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "quickstarts" / "mcp-generic.md"
MANIFEST = REPO_ROOT / "adapters" / "tools.manifest.json"
TOOLS_TS = (REPO_ROOT / "services" / "agent-orchestrator" / "src" / "tools.ts")

# Prose that legitimately names the withheld tools in order to explain that
# they are withheld. Only the catalogue section is under inventory rules.
_TOOLS_SECTION = re.compile(r"^## The tools$(.*?)^## ", re.M | re.S)


def _manifest_names() -> set[str]:
    data = json.loads(MANIFEST.read_text())
    tools = data if isinstance(data, list) else data.get("tools", [])
    return {t["name"] for t in tools}


def _privileged_names() -> set[str]:
    src = TOOLS_TS.read_text(encoding="utf-8")
    m = re.search(r"PRIVILEGED_TOOL_NAMES\s*=\s*new Set\(\[(.*?)\]\)", src,
                  re.S)
    assert m, "PRIVILEGED_TOOL_NAMES not found — did tools.ts move?"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _doc_section() -> str:
    body = DOC.read_text(encoding="utf-8")
    m = _TOOLS_SECTION.search(body + "\n## ")
    assert m, "the '## The tools' section is gone from mcp-generic.md"
    return m.group(1)


def test_every_tool_the_docs_name_actually_exists():
    named = set(re.findall(r"`([a-z][a-z0-9_]+)`", _doc_section()))
    # Header names appear in the same section; they are not tool names.
    named -= {"x_step_up_token", "tools_list"}
    unknown = sorted(n for n in named if n not in _manifest_names())
    assert not unknown, (
        "mcp-generic.md names tools that are not in the catalogue: "
        + ", ".join(unknown))


def test_docs_do_not_offer_tools_the_server_withholds():
    """The specific error this file exists for.

    A privileged tool may be *mentioned* — explaining the withholding is
    useful — but it must not appear in the Read/Write/shim inventory lines,
    which read as "here is what you can call".
    """
    privileged = _privileged_names()
    assert privileged, "expected at least one privileged tool"

    # Exempt by MEANING, not by line shape. An earlier version of this guard
    # whitelisted the prefixes "Read"/"Write"/"ChatGPT-connector", so adding a
    # "Utility: `fhir_get_token`" line sailed straight through it — the guard
    # was pinned to the formatting of the day it was written. Any paragraph
    # that does not explain the withholding is an inventory paragraph.
    explains = re.compile(r"withheld|privileged|PRIVILEGED|not appear|"
                          r"withholding", re.I)

    leaked = set()
    for para in re.split(r"\n\s*\n", _doc_section()):
        if explains.search(para):
            continue
        leaked |= set(re.findall(r"`([a-z][a-z0-9_]+)`", para)) & privileged

    leaked = sorted(leaked)
    assert not leaked, (
        "mcp-generic.md offers withheld tools as callable: "
        + ", ".join(leaked)
        + " — these are in PRIVILEGED_TOOL_NAMES and never appear in a "
          "hosted client's tools/list.")


def test_the_documented_count_matches_what_a_hosted_client_sees():
    served = len(_manifest_names() - _privileged_names())
    m = re.search(r"serves \*\*(\d+)\*\*", _doc_section())
    assert m, "mcp-generic.md no longer states how many tools are served"
    assert int(m.group(1)) == served, (
        f"docs say a hosted deployment serves {m.group(1)}; catalogue minus "
        f"privileged is {served}")
