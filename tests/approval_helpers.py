"""Mint an approval credential the way the approve surface does (#659):
bound to the action AND the keyed digest of its payload as it stands."""

from r6.actions.confirmations import ACTION_APPROVAL_AUDIENCE, approval_operation


def bound_operation(app, action_id):
    from r6.actions.models import ProposedAction
    with app.app_context():
        action = ProposedAction.query.get(action_id)
        return approval_operation(action_id, action.payload_json)


def approval_headers(app, auth_headers, action_id):
    from r6.stepup import generate_step_up_token
    headers = dict(auth_headers)
    headers['X-Step-Up-Token'] = generate_step_up_token(
        headers['X-Tenant-Id'], audience=ACTION_APPROVAL_AUDIENCE,
        operation=bound_operation(app, action_id))
    return headers
