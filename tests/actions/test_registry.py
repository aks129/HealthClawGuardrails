import pytest
from r6.actions.registry import (ActionExecutor, ExecutionResult,
                                  register_executor, get_executor, all_kinds, _clear)


class _Toy:
    kind = 'toy'
    required_env = ()

    def validate(self, payload):
        return [] if payload.get('body') else ['payload_invalid']

    def execute(self, action):
        return ExecutionResult(status='completed')

    def reconcile(self, action):
        return ExecutionResult(status='completed')


def test_register_and_lookup():
    _clear()
    register_executor(_Toy())
    assert 'toy' in all_kinds()
    assert isinstance(get_executor('toy'), _Toy)


def test_unregistered_kind_returns_none():
    _clear()
    assert get_executor('never-registered') is None


def test_duplicate_kind_rejected():
    _clear()
    register_executor(_Toy())
    with pytest.raises(ValueError):
        register_executor(_Toy())


def test_non_conforming_object_rejected():
    _clear()

    class _Broken:
        kind = 'broken'

    with pytest.raises(TypeError):
        register_executor(_Broken())


def test_execution_result_shape():
    r = ExecutionResult(status='needs_review', provider_ref='x', outcome={'k': 1})
    assert r.status == 'needs_review'
    assert r.provider_ref == 'x'
    assert r.outcome == {'k': 1}
    assert r.error is None
    assert r.outcome_unknown is False


def test_execution_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        ExecutionResult(status='sorta-done')
