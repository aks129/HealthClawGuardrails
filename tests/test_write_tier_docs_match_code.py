"""The documented write-tier tool list must match the tier declared in code.

`docs/recipes/any-agent-framework.md` told integrators which tools need
`X-Step-Up-Token`. It listed four. The gate then moved from a hardcoded name
list to the declared `tier` (#328), which made it eight — and the doc was left
stale in the *other* direction: it now under-reported which tools need a token,
so an integrator following it would build a client that 400s on four tools.

That is the same drift class as the tool manifest, which claimed to be
generated and was not. The answer there was a generator plus a test; the answer
here is this test. A prose list that restates a value the code owns needs
something holding the two together, or it is only accurate on the day it is
written.

Scope: this checks the doc does not CONTRADICT the code. It cannot check that
the surrounding prose is true.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_TS = REPO_ROOT / 'services' / 'agent-orchestrator' / 'src' / 'tools.ts'
RECIPE = REPO_ROOT / 'docs' / 'recipes' / 'any-agent-framework.md'

# `name: "x", ... tier: "write"` within one registration object. Non-greedy up
# to the next `}` keeps it inside a single entry.
_WRITE_TIER = re.compile(r'name:\s*"([a-z_]+)"[^}]*?tier:\s*"write"', re.S)


def _write_tier_tools() -> set[str]:
    return set(_WRITE_TIER.findall(TOOLS_TS.read_text(encoding='utf-8')))


def test_the_registry_still_declares_write_tier_tools():
    """Guards the guard: a regex that matches nothing would pass everything."""
    tools = _write_tier_tools()
    assert len(tools) >= 5, (
        f'only {len(tools)} write-tier tools found in tools.ts — the pattern '
        'has probably stopped matching, which would make the next assertion '
        'vacuous')


def test_every_write_tier_tool_is_named_in_the_integrator_recipe():
    """MUTATION: delete a tool name from the recipe's list -> red.

    An integrator who builds against a short list ships a client that 400s on
    the tools it omitted.
    """
    doc = RECIPE.read_text(encoding='utf-8')
    missing = sorted(t for t in _write_tier_tools() if f'`{t}`' not in doc)
    assert not missing, (
        'these tools are tier:"write" in tools.ts but are not named in '
        f'{RECIPE.relative_to(REPO_ROOT)}: {", ".join(missing)}. A client '
        'built from that doc will be refused when it calls them without a '
        'step-up token.')


def test_the_recipe_does_not_promise_a_write_tool_needs_no_token():
    """The specific false claim that shipped: naming a write-tier tool as an
    example of something safe to call without step-up.

    MUTATION: re-add 'Safe to call without step-up' next to a write tool -> red.
    """
    doc = RECIPE.read_text(encoding='utf-8').lower()
    for tool in sorted(_write_tier_tools()):
        for phrase in ('safe to call without step-up',
                       f'{tool} does not require'):
            assert phrase not in doc, (
                f'the recipe tells integrators {tool!r} needs no step-up '
                'token, but it is declared tier:"write"')
