"""Generic contract suite — parametrized over EVERY registered executor. A rail
merges only when it passes this. New rails add zero lines here."""
import pytest
from r6.actions.registry import all_kinds, get_executor, ExecutionResult


def _kinds():
    from r6.actions.registry import _clear
    from r6.actions.rails import register_all
    _clear()
    register_all()
    return list(all_kinds())


@pytest.fixture(autouse=True)
def _fresh_registry_per_test(action_registry):
    """_kinds() above only runs once, at COLLECTION time, to build the
    parametrize list — by the time a test actually RUNS, some other test
    module may have _clear()'d the shared registry for its own purposes.
    Re-registering the real rails before (and after) every test function
    here, via the action_registry fixture, keeps get_executor(kind) valid
    regardless of test execution order across the whole suite."""
    yield


@pytest.mark.parametrize('kind', _kinds())
def test_validate_rejects_empty_payload(kind):
    errs = get_executor(kind).validate({})
    assert isinstance(errs, list) and errs


@pytest.mark.parametrize('kind', _kinds())
def test_executor_declares_required_env(kind):
    assert isinstance(get_executor(kind).required_env, tuple)
    assert get_executor(kind).required_env  # non-empty for real-provider rails


@pytest.mark.parametrize('kind', _kinds())
def test_reconcile_is_implemented(kind):
    assert callable(getattr(get_executor(kind), 'reconcile', None))


@pytest.mark.parametrize('kind', _kinds())
def test_execute_without_credentials_fails_loud(kind, monkeypatch):
    """No silent simulation, ever: missing provider config must be a loud
    failure, never a fake success."""
    ex = get_executor(kind)
    for var in ex.required_env:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv('BLAND_API_KEY', raising=False)  # alias
    class _A:
        payload = {'phone': '+15550000000', 'body': 'test'}
        external_ref = None
        id = 'test-action'
    r = ex.execute(_A())
    assert r.status == 'failed'
    from r6.actions import errors
    assert r.error == errors.PROVIDER_NOT_CONFIGURED
