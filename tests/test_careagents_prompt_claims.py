"""The agent's system prompt may only promise what the product does.

SAFETY_CORE used to tell the model every access is written to "an audit trail
the person can see". There is no patient-facing audit view, so a model taking
its instructions literally would send a user looking for a page that does not
exist. Every access IS recorded in an audit log; that is what it may say.

MUTATION: restore "an audit trail the person can see" in SAFETY_CORE -> red.
"""

from __future__ import annotations

import re

import pytest

from careagents.personas import PERSONAS, SAFETY_CORE, system_prompt

#: Words that turn "there is an audit log" into "you can look at it".
_VIEW_WORDS = re.compile(
    r"\b(see|sees|seen|view|views|visible|look|browse|open|check|review)\b",
    re.IGNORECASE)


def _audit_clauses(prompt: str) -> list[str]:
    """Each sentence or bullet of the prompt that mentions an audit."""
    parts = re.split(r"(?<=[.!?])\s+|\n-\s", prompt)
    return [p for p in parts if "audit" in p.lower()]


@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_the_prompt_does_not_promise_a_user_visible_audit_view(persona):
    clauses = _audit_clauses(system_prompt("Juniper", persona))
    assert clauses, "the prompt no longer mentions the audit log at all"
    for clause in clauses:
        assert not _VIEW_WORDS.search(clause), (
            f"the prompt tells the model the person can view the audit "
            f"trail: {clause!r}")


def test_the_prompt_states_what_is_true_about_the_audit_log():
    text = " ".join(SAFETY_CORE.split()).lower()
    assert "every access is recorded in an audit log" in text
