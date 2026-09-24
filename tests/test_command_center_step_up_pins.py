"""Characterization pins for the command centre's two step-up gates (#655).

`_authz_write` (session or X-Step-Up-Token, guarding conversation and task
writes) and the dashboard-link mint (`/api/generate-link`) answer a refusal
in their own JSON shape, not the kernel's OperationOutcome. Moving them onto
r6.access must not change a byte of that answer, so every row here pins the
exact status AND the exact body, captured from the code before the move.

Three rows pin answers the kernel would give differently on its own. They
are kept at the site on purpose; changing them is a behaviour change, and
behaviour changes do not go in migration PRs:

  - a whitespace-only token is "Malformed step-up token", not absent (the
    kernel strips it to nothing and would call it absent, falling through to
    the session on `_authz_write`);
  - a whitespace-PADDED valid token is refused as "Invalid token signature"
    (the kernel strips before validating, #334, and would grant it);
  - a tenant id that fails the kernel's format pattern is not a 400 here: it
    is a token that fails, or passes, its tenant binding, as it always was.

These tests use a PRIVATE tenant: `test-tenant` is in PUBLIC_TENANTS under
the test config, and the mint skips the step-up branch for a public tenant.
"""

import pytest

from models import db
from r6.command_center import access
from r6.command_center.models import AgentTask
from r6.stepup import generate_step_up_token

PRIVATE = 'pin-private-655'
OTHER = 'pin-other-655'
MALFORMED = 'bad tenant!'

CONVERSATIONS = '/command-center/api/conversations'
TASKS = '/command-center/api/tasks'

AUTH_REQUIRED = (
    b'{"error":"authentication required (session cookie or X-Step-Up-Token)"}\n')


def _rejected(reason: str) -> bytes:
    return b'{"error":"step-up token rejected: ' + reason.encode() + b'"}\n'


MALFORMED_TOKEN = _rejected('Malformed step-up token')
BAD_SIGNATURE = _rejected('Invalid token signature')
GENERIC = _rejected('Invalid step-up token')
READ_SCOPED = _rejected('Read-scoped token cannot authorize this operation')


def _token(kind: str) -> str | None:
    """The X-Step-Up-Token value for one row, minted fresh."""
    if kind == 'absent':
        return None
    if kind == 'empty':
        return ''
    if kind == 'whitespace':
        return '   '
    if kind == 'valid':
        return generate_step_up_token(PRIVATE)
    if kind == 'padded':
        return '  ' + generate_step_up_token(PRIVATE) + '  '
    if kind == 'leading-pad':
        return '  ' + generate_step_up_token(PRIVATE)
    if kind == 'padded-junk':
        return '  not-a-real-token  '
    if kind == 'junk':
        return 'not-a-real-token'
    if kind == 'wrong-tenant':
        return generate_step_up_token(OTHER)
    if kind == 'read-scoped':
        return generate_step_up_token(PRIVATE, scope='read')
    if kind == 'malformed-own':
        return generate_step_up_token(MALFORMED)
    raise AssertionError(kind)


def _headers(kind: str, extra: dict | None = None) -> dict:
    headers = dict(extra or {})
    token = _token(kind)
    if token is not None:
        headers['X-Step-Up-Token'] = token
    return headers


def _login(client, tenant: str) -> None:
    client.get('/command-center',
               query_string={'t': access.generate_access_token(tenant)})


# --- _authz_write ----------------------------------------------------------

_AUTHZ_WRITE_REFUSALS = [
    # (row, tenant_id, token kind, extra headers, expected body)
    ('absent token', PRIVATE, 'absent', None, AUTH_REQUIRED),
    ('empty header', PRIVATE, 'empty', None, AUTH_REQUIRED),
    ('whitespace-only token', PRIVATE, 'whitespace', None, MALFORMED_TOKEN),
    ('padded valid token', PRIVATE, 'padded', None, BAD_SIGNATURE),
    ('leading-padded valid token', PRIVATE, 'leading-pad', None,
     BAD_SIGNATURE),
    ('padded junk', PRIVATE, 'padded-junk', None, MALFORMED_TOKEN),
    ('junk token', PRIVATE, 'junk', None, MALFORMED_TOKEN),
    ('wrong-tenant token', PRIVATE, 'wrong-tenant', None, GENERIC),
    ('read-scoped token', PRIVATE, 'read-scoped', None, READ_SCOPED),
    ('bearer is not an alias here', PRIVATE, 'absent',
     {'Authorization': 'Bearer ' + generate_step_up_token(PRIVATE)},
     AUTH_REQUIRED),
    ('malformed tenant, no token', MALFORMED, 'absent', None, AUTH_REQUIRED),
    ('malformed tenant, another tenant\'s token', MALFORMED, 'wrong-tenant',
     None, GENERIC),
    ('non-string tenant, a token', 123, 'wrong-tenant', None, GENERIC),
]


@pytest.mark.parametrize(
    'row, tenant, kind, extra, expected', _AUTHZ_WRITE_REFUSALS,
    ids=[r[0] for r in _AUTHZ_WRITE_REFUSALS])
def test_authz_write_refusal_is_byte_identical(
        client, row, tenant, kind, extra, expected):
    response = client.post(
        CONVERSATIONS, json={'tenant_id': tenant, 'role': 'user', 'text': 'hi'},
        headers=_headers(kind, extra))
    assert (response.status_code, response.get_data()) == (401, expected), row


def test_authz_write_grants_a_valid_token(client):
    response = client.post(
        CONVERSATIONS,
        json={'tenant_id': PRIVATE, 'role': 'user', 'text': 'hi'},
        headers=_headers('valid'))
    assert response.status_code == 201


_AUTHZ_WRITE_WITH_SESSION = [
    # A present token is decided on its own; it never falls back to the
    # session, so a bad one refuses even for a signed-in browser.
    ('session, no token', 'absent', 201, None),
    ('session, empty header', 'empty', 201, None),
    ('session, whitespace-only token', 'whitespace', 401, MALFORMED_TOKEN),
    ('session, padded valid token', 'padded', 401, BAD_SIGNATURE),
    ('session, wrong-tenant token', 'wrong-tenant', 401, GENERIC),
]


@pytest.mark.parametrize(
    'row, kind, status, expected', _AUTHZ_WRITE_WITH_SESSION,
    ids=[r[0] for r in _AUTHZ_WRITE_WITH_SESSION])
def test_authz_write_with_a_session(app, row, kind, status, expected):
    with app.test_client() as client:
        _login(client, PRIVATE)
        response = client.post(
            CONVERSATIONS,
            json={'tenant_id': PRIVATE, 'role': 'user', 'text': 'hi'},
            headers=_headers(kind))
    assert response.status_code == status, row
    if expected is not None:
        assert response.get_data() == expected, row


def test_authz_write_binds_a_malformed_tenant_it_was_given(client):
    """The id is not format-checked here and never was: a token minted for
    exactly that string passes its tenant binding. Pinned so the move keeps
    it; tightening it is a separate, reviewed change."""
    response = client.post(
        TASKS,
        json={'tenant_id': MALFORMED, 'agent_id': 'sally', 'title': 't'},
        headers=_headers('malformed-own'))
    assert response.status_code == 201


def _task(tenant_id: str) -> str:
    task = AgentTask(tenant_id=tenant_id, agent_id='sally', title='t')
    db.session.add(task)
    db.session.commit()
    return task.id


@pytest.mark.parametrize('kind, status, expected', [
    ('valid', 200, None),
    ('wrong-tenant', 401, GENERIC),
    ('whitespace', 401, MALFORMED_TOKEN),
    ('absent', 401, AUTH_REQUIRED),
])
def test_authz_write_on_a_stored_task_uses_the_rows_tenant(
        app, client, kind, status, expected):
    """PATCH authorizes against the task row's tenant, not a request input."""
    with app.app_context():
        task_id = _task(PRIVATE)
    response = client.patch(f'{TASKS}/{task_id}', json={'status': 'completed'},
                            headers=_headers(kind))
    assert response.status_code == status
    if expected is not None:
        assert response.get_data() == expected
