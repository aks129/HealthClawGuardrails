"""Fail-closed AgentRun and AgentToolCall state transitions."""

RUN_STATES = frozenset({
    "queued",
    "running",
    "waiting_for_human",
    "completed",
    "failed",
    "cancelled",
})

TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

RUN_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({
        "queued", "waiting_for_human", "completed", "failed", "cancelled"}),
    "waiting_for_human": frozenset({"queued", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TOOL_STATES = frozenset({"pending", "running", "completed", "failed"})
TOOL_TRANSITIONS = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset({"running"}),
}


class InvalidTransition(ValueError):
    pass


def require_run_transition(current: str, target: str) -> None:
    if current not in RUN_STATES or target not in RUN_STATES:
        raise InvalidTransition(f"unknown run state: {current} -> {target}")
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid run transition: {current} -> {target}")


def require_tool_transition(current: str, target: str) -> None:
    if current not in TOOL_STATES or target not in TOOL_STATES:
        raise InvalidTransition(f"unknown tool state: {current} -> {target}")
    if target not in TOOL_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid tool transition: {current} -> {target}")
