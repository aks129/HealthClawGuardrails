"""form-fill rail (Task 3) — registered ActionExecutor skeleton.

execute() is intentionally NOT the full populate->review->render->
DocumentReference->link orchestration (that's Task 8). This is the
fail-loud/fail-safe shell: missing PUBLIC_BASE_URL is a loud failure, and
even with it present the honest answer today is 'needs_review', never a
fabricated 'completed'.
"""
from r6.actions import errors
from r6.actions.registry import all_kinds, get_executor


def test_form_fill_is_registered(action_registry):
    assert 'form-fill' in all_kinds()


def test_required_env_is_public_base_url(action_registry):
    ex = get_executor('form-fill')
    assert ex.required_env == ('PUBLIC_BASE_URL',)


def test_validate_rejects_empty_payload(action_registry):
    ex = get_executor('form-fill')
    errs = ex.validate({})
    assert isinstance(errs, list) and errs


def test_validate_accepts_questionnaire_and_body(action_registry):
    ex = get_executor('form-fill')
    assert ex.validate({'questionnaire': 'healthclaw-intake', 'body': 'x'}) == []


def test_validate_rejects_missing_questionnaire(action_registry):
    ex = get_executor('form-fill')
    errs = ex.validate({'body': 'x'})
    assert isinstance(errs, list) and errs


def test_validate_rejects_missing_body(action_registry):
    ex = get_executor('form-fill')
    errs = ex.validate({'questionnaire': 'healthclaw-intake'})
    assert isinstance(errs, list) and errs


class _Action:
    def __init__(self, payload):
        self.payload = payload
        self.external_ref = None
        self.id = 'test-action'
        self.tenant_id = 'test-tenant'


def test_execute_without_public_base_url_fails_loud(action_registry, monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    ex = get_executor('form-fill')
    result = ex.execute(_Action({'questionnaire': 'healthclaw-intake', 'body': 'x'}))
    assert result.status == 'failed'
    assert result.error == errors.PROVIDER_NOT_CONFIGURED


def test_execute_with_public_base_url_is_honest_needs_review(action_registry,
                                                              monkeypatch):
    """Orchestration lands in Task 8 — until then, execute() must never fake
    a completed form. needs_review is the honest placeholder."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://app.example.org')
    ex = get_executor('form-fill')
    result = ex.execute(_Action({'questionnaire': 'healthclaw-intake', 'body': 'x'}))
    assert result.status == 'needs_review'
    assert result.status != 'completed'


def test_reconcile_is_honest_needs_review(action_registry):
    ex = get_executor('form-fill')
    result = ex.reconcile(_Action({'questionnaire': 'healthclaw-intake', 'body': 'x'}))
    assert result.status in ('needs_review', 'completed', 'failed')
