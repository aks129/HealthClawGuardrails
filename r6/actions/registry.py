"""The action extension point (public, versioned). A rail implements
ActionExecutor and calls register_executor() from its module in
r6/actions/rails/ (imported at boot). VALID kinds are derived from this
registry — rails never edit a shared tuple, so parallel rails can't collide.

This is the contributor surface the docs advertise: implement the three
methods (~50 lines), register, and your capability inherits the entire
guardrail rail — propose validation, the out-of-band human gate, audit,
observability — for free.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Terminal-ish statuses an executor may return. 'executing' means a provider
# webhook will resolve the outcome later; the rest are immediate verdicts.
_RESULT_STATUSES = ('executing', 'completed', 'failed', 'needs_review')


@dataclass
class ExecutionResult:
    status: str
    provider_ref: str | None = None
    outcome: dict = field(default_factory=dict)
    error: str | None = None
    outcome_unknown: bool = False  # provider MAY have acted -> caller maps to 'unknown'

    def __post_init__(self):
        if self.status not in _RESULT_STATUSES:
            raise ValueError('Unknown ExecutionResult status: %s' % self.status)


@runtime_checkable
class ActionExecutor(Protocol):
    kind: str
    required_env: tuple            # env vars preflight checks for this rail

    def validate(self, payload: dict) -> list: ...      # [] ok; error codes otherwise
    def execute(self, action) -> ExecutionResult: ...
    def reconcile(self, action) -> ExecutionResult: ...  # query provider truth


_REGISTRY = {}


def register_executor(executor):
    # isinstance against a runtime_checkable Protocol with data members
    # (kind, required_env) checks attribute/method PRESENCE only, not
    # signatures — verified stable on this repo's Python (3.13): it does not
    # raise TypeError for non-method members, and correctly rejects objects
    # missing any of the five members. That's exactly the contract we want:
    # a helpful, early rejection of a rail that forgot a piece, not a
    # guarantee of correct behavior (tests still own that).
    if not isinstance(executor, ActionExecutor):
        raise TypeError('executor must implement ActionExecutor Protocol '
                        '(kind, required_env, validate, execute, reconcile)')
    if executor.kind in _REGISTRY:
        raise ValueError('executor already registered for kind: %s' % executor.kind)
    _REGISTRY[executor.kind] = executor


def get_executor(kind):
    return _REGISTRY.get(kind)


def all_kinds():
    return tuple(_REGISTRY.keys())


def _clear():
    """Test-only: reset the registry between tests."""
    _REGISTRY.clear()
