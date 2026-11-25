"""Mint an approval credential the way the approve surface does (#659):
bound to the action AND the keyed digest of its payload as it stands."""

from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE, approval_operation


def bound_operation(app, action_id):
    from r6.actions.models import ProposedAction
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        return approval_operation(action_id, action.payload_json)


def propose_headers(headers):
    """The same headers with a tenant-bound write token: propose needs a
    grant since #1001. Any token already present is replaced."""
    from r6.stepup import generate_step_up_token
    headers = dict(headers)
    headers['X-Step-Up-Token'] = generate_step_up_token(
        headers['X-Tenant-Id'])
    return headers


def approval_headers(app, auth_headers, action_id):
    from r6.stepup import generate_step_up_token
    headers = dict(auth_headers)
    headers['X-Step-Up-Token'] = generate_step_up_token(
        headers['X-Tenant-Id'], audience=ACTION_APPROVAL_AUDIENCE,
        operation=bound_operation(app, action_id))
    return headers


def sms_rail_env(monkeypatch):
    """Configure the one rail that reaches a provider, for tests that drive
    an approval through to the provider request."""
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'AC-test')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'test-token')
    monkeypatch.setenv('TWILIO_FROM_NUMBER', '+15555550100')


def page_digest(client, headers, action_id):
    """The draft digest from the review page as rendered, the way the
    person's browser carries it into the submit (#1174). Read off the HTML,
    never computed here, so a test using it measures the page."""
    import re
    page = client.get('/r6/actions/%s/review' % action_id,
                      headers=propose_headers(headers))
    match = re.search(r'name="draft_digest" value="([0-9a-f]{64})"',
                      page.get_data(as_text=True))
    if page.status_code != 200 or match is None:
        raise RuntimeError('no draft digest on the review page (%s)'
                           % page.status_code)
    return match.group(1)


def reviewed(client, headers, action_id, decisions, strict=True):
    """The decisions plus the digest of the page they were made on.

    strict=False leaves the decisions as they are when no page renders (an
    unknown or closed action), for tests whose submit is refused before the
    digest is read."""
    if not isinstance(decisions, dict) or 'draft_digest' in decisions:
        return decisions
    try:
        digest = page_digest(client, headers, action_id)
    except RuntimeError:
        if strict:
            raise
        return decisions
    return dict(decisions, draft_digest=digest)
