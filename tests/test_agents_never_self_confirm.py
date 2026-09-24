"""Agent-facing text must never tell an agent to set X-Human-Confirmed (#214).

The header on a direct clinical FHIR write is client-supplied: whoever sends
the request can set it. That is the known gap #214 tracks. The code is only
half of it — until this test, the persona, the skills and the MCP proposal
reply all told an agent that a 428 meant "add the header and retry", which is
the gap written down as an instruction: we were teaching agents to confirm
for the human.

The rule for agent-facing text is now: on a 428, stop and tell the person a
human must confirm; never set the header yourself; outward actions go through
the action rail's out-of-band approval. A line may still *name* the header,
but only to forbid setting it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# What an agent reads as instructions: the Hermes persona, the shared skills,
# the Telegram bot, and the MCP tool replies (propose_write's next_steps).
_SCANNED_DIRS = ('hermes', 'skills', 'openclaw')
_SCANNED_FILES = ('services/agent-orchestrator/src/tools.ts',)
_TEXT_SUFFIXES = {'.md', '.py', '.sh', '.json', '.toml', '.txt', '.yaml',
                  '.yml', '.ts'}

_HEADER = re.compile(r'x-human-confirmed', re.IGNORECASE)

# The one allowed shape: a prohibition that governs the header directly —
# "never set `X-Human-Confirmed` yourself", "do not send X-Human-Confirmed".
# "never go straight to commit without ... X-Human-Confirmed" does NOT pass:
# it tells the agent to go get the header.
_PROHIBITION = re.compile(
    r"\b(?:never|do not|don't|must not)\s+"
    r"(?:set|send|add|pass|include|attach|supply)\b[^.\n]{0,40}"
    r"x-human-confirmed",
    re.IGNORECASE)


def _agent_facing_files() -> list[Path]:
    paths: list[Path] = []
    for directory in _SCANNED_DIRS:
        paths.extend(p for p in sorted((REPO_ROOT / directory).rglob('*'))
                     if p.is_file() and p.suffix in _TEXT_SUFFIXES)
    paths.extend(REPO_ROOT / f for f in _SCANNED_FILES)
    return paths


def _offending_lines(path: Path) -> list[str]:
    hits = []
    text = path.read_text(encoding='utf-8', errors='replace')
    for lineno, line in enumerate(text.splitlines(), 1):
        if not _HEADER.search(line):
            continue
        # A code comment is read by maintainers, not sent to an agent.
        if path.suffix == '.ts' and line.lstrip().startswith('//'):
            continue
        if _PROHIBITION.search(line):
            continue
        hits.append('%s:%d: %s' % (path.relative_to(REPO_ROOT), lineno,
                                   line.strip()))
    return hits


def test_the_scan_reaches_the_agent_facing_files():
    """Guard the guard: a scan that silently covers nothing passes forever."""
    scanned = {p.relative_to(REPO_ROOT).as_posix()
               for p in _agent_facing_files()}
    for expected in ('hermes/SOUL.md', 'skills/curatr/SKILL.md',
                     'skills/fhir-r6-guardrails/SKILL.md',
                     'services/agent-orchestrator/src/tools.ts'):
        assert expected in scanned, 'scan lost %s' % expected


def test_no_agent_facing_text_tells_an_agent_to_set_the_header():
    hits = [h for p in _agent_facing_files() for h in _offending_lines(p)]
    assert not hits, (
        'agent-facing text names X-Human-Confirmed other than to forbid '
        'setting it (#214). On a 428 an agent stops and tells the person a '
        'human must confirm; it never sets the header itself.\n'
        + '\n'.join(hits))


def test_the_rule_rejects_the_old_instructions():
    """The shapes this test exists to catch, as they shipped before #214."""
    shipped = [
        "never go straight to `fhir_commit_write` without a step-up token "
        "+ `X-Human-Confirmed`.",
        "didn't get the `X-Human-Confirmed` header — re-confirm with the "
        "user and retry.",
        "| Add `X-Human-Confirmed: true` after reviewing the proposal |",
        "**Requires:** `X-Step-Up-Token` header + `X-Human-Confirmed: true`",
    ]
    for line in shipped:
        assert _HEADER.search(line) and not _PROHIBITION.search(line), line
    assert _PROHIBITION.search('never set `X-Human-Confirmed` yourself')
