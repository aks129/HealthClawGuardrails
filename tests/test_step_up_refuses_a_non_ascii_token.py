"""A non-ASCII byte in a step-up token is a wrong token, not a crash (#557).

`hmac.compare_digest` raises TypeError on a `str` that is not ASCII.
`validate_step_up_token` passed it the caller's signature half raw, nothing
caught it — `r6/access._evaluate` does not catch validator exceptions, by
design — so every endpoint that reached the gate answered an anonymous caller
with a 500 where ASCII garbage got a 401. It failed closed, so this was noise
and information disclosure rather than a bypass: a 500 told a prober their
token was something other than merely wrong.

THE PROPERTY: a token carrying a non-ASCII byte is answered EXACTLY as ASCII
garbage is — same status, same body. Not "answered 401": /wearables/sync-now
answers 403, and pinning one status here would encode a normalization nobody
has decided (`r6/access.require_grant`'s absent_status/rejected_status exist
for exactly that reason). Sameness is the property; the status each row
already answers is carried alongside it so a row cannot pass by refusing
earlier, for some other reason, in both requests.

Every surface reaching require_grant gets a row, and
test_every_require_grant_site_has_a_row fails when a new one is added without
one — enumerating routes by hand covers the ten that exist today and none of
the ones written next month.

MUTATION: revert r6/stepup.py to `hmac.compare_digest(sig, expected_sig)` ->
every row raises TypeError out of the test client (TESTING propagates; in
production it is the 500 this issue reports).
"""

import ast
import json
import pathlib

import pytest

from r6.stepup import generate_step_up_token, validate_step_up_token

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The same production tree tests/test_constant_time.py and
#: tests/test_ratchets.py walk. Kernel adoption is confined to r6/ and main.py
#: today; the pin should not depend on that staying true.
_PRODUCTION_DIRS = ('r6', 'careagents', 'adapters', 'api', 'services',
                    'scripts', 'openclaw', 'hermes', 'migrations')
_PRODUCTION_ROOT_FILES = ('main.py', 'app.py', 'models.py')
_SCAN_FLOOR = 100


def _production_python_files():
    for name in _PRODUCTION_ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path
    for folder in _PRODUCTION_DIRS:
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' not in path.parts:
                yield path

#: Appended to a valid token to make it wrong. The pair differs ONLY in
#: whether the added byte is ASCII, so anything but sameness in the two
#: answers is caused by the encoding and nothing else.
_ASCII_SUFFIX = 'x'
_NON_ASCII_SUFFIX = 'é'

#: `r6/routes.py:check_human_confirmation` is a blueprint before_request,
#: so a clinical write on /r6/fhir answers 428 before the handler runs and
#: never reaches its step-up gate. Carrying the header is what puts these
#: rows in front of the gate they are here to measure — the same thing
#: tests/test_rx_transfer.py does to seed. Nothing here builds a write path
#: on the header (#214): every request in this file is refused at step-up.
_HUMAN_CONFIRMED = {'X-Human-Confirmed': 'true'}


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------

class Row:
    """One request that reaches a require_grant gate.

    `site` names the gate, so the AST pin below can check the rows against
    the source rather than against a number somebody remembered to bump.
    """

    def __init__(self, id, site, method, path, status, body=None,
                 headers=None, seed=None):
        self.id = id
        self.site = site
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.seed = seed


def _seed_medication_request(client, tenant_id, token):
    """rx-transfer/propose only becomes a write once a draft exists — with no
    transferable medication it answers 422 before it ever reaches its gate,
    and the row would then pass while measuring nothing."""
    med = {'resourceType': 'MedicationRequest', 'status': 'active',
           'intent': 'order',
           'medicationCodeableConcept': {'text': 'Atorvastatin 20 mg tablet'},
           'subject': {'reference': 'Patient/non-ascii-probe'}}
    resp = client.post('/r6/fhir/MedicationRequest',
                       headers={'X-Tenant-Id': tenant_id,
                                'X-Step-Up-Token': token,
                                **_HUMAN_CONFIRMED,
                                'Content-Type': 'application/fhir+json'},
                       data=json.dumps(med))
    assert resp.status_code == 201, resp.get_data(as_text=True)


ROWS = [
    Row('fhir-create', 'r6/routes.py:create_resource',
        'POST', '/r6/fhir/Observation', 401,
        body={'resourceType': 'Observation'}, headers=_HUMAN_CONFIRMED),
    Row('fhir-update', 'r6/routes.py:update_resource',
        'PUT', '/r6/fhir/Observation/no-such-observation', 401,
        body={'resourceType': 'Observation'}, headers=_HUMAN_CONFIRMED),
    Row('fhir-share-bundle', 'r6/routes.py:share_bundle',
        'POST', '/r6/fhir/$share-bundle', 401, body={}),
    Row('actions-rx-transfer-propose',
        'r6/actions/routes.py:propose_rx_transfer',
        'POST', '/r6/actions/rx-transfer/propose', 401,
        body={'to_pharmacy': {'name': 'Walgreens Main St',
                              'phone': '+15551230000'}},
        seed=_seed_medication_request),
    Row('actions-commit', 'r6/actions/routes.py:commit_action',
        'POST', '/r6/actions/no-such-action/commit', 401, body={}),
    Row('actions-confirm', 'r6/actions/routes.py:confirm_action',
        'POST', '/r6/actions/no-such-action/confirm', 401, body={}),
    Row('actions-review-get', 'r6/actions/review.py:_require_step_up',
        'GET', '/r6/actions/no-such-action/review', 401),
    Row('actions-review-post', 'r6/actions/review.py:_require_step_up',
        'POST', '/r6/actions/no-such-action/review', 401, body={}),
    Row('smbp-reading', 'r6/smbp/routes.py:reading',
        'POST', '/r6/smbp/reading', 401, body={'systolic': 120,
                                               'diastolic': 80}),
    # The minority dialect: this gate answers 403, which is why the rows
    # assert sameness rather than 401.
    Row('wearables-sync-now', 'r6/wearables/routes.py:sync_now',
        'POST', '/wearables/sync-now', 403, body={}),
    # Kernel slice 7. The 403 dialect again; phase one of the two-phase gate
    # answers before the body is read, so the id need not exist.
    Row('curatr-apply-fix', 'r6/routes.py:curatr_apply_fix',
        'POST', '/r6/fhir/Condition/no-such-condition/$curatr-apply-fix', 403,
        body={'fixes': [{'field_path': 'Condition.code.coding[0].code',
                         'new_value': 'E11.9'}]},
        headers=_HUMAN_CONFIRMED),
]


def _call(client, row, tenant_id, token):
    kwargs = {'headers': {'X-Tenant-Id': tenant_id,
                          'X-Step-Up-Token': token,
                          'Content-Type': 'application/json',
                          **row.headers}}
    if row.body is not None:
        kwargs['data'] = json.dumps(row.body)
    return client.open(row.path, method=row.method, **kwargs)


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r.id)
def test_a_non_ascii_token_is_refused_exactly_as_ascii_garbage_is(
        client, tenant_id, step_up_token, row):
    """MUTATION: revert the compare in r6/stepup.py -> TypeError, not 401.

    Both requests carry a VALID token with one extra character, so the only
    difference between them is whether that character is ASCII.
    """
    if row.seed is not None:
        row.seed(client, tenant_id, step_up_token)

    ascii_resp = _call(client, row, tenant_id,
                       step_up_token + _ASCII_SUFFIX)
    assert ascii_resp.status_code == row.status, (
        f'{row.id}: ASCII garbage answered {ascii_resp.status_code}, not the '
        f'pinned {row.status} — this row is not reaching its step-up gate, '
        f'so it would measure nothing: {ascii_resp.get_data(as_text=True)}')

    non_ascii_resp = _call(client, row, tenant_id,
                           step_up_token + _NON_ASCII_SUFFIX)
    assert non_ascii_resp.status_code == ascii_resp.status_code, (
        f'{row.id}: a non-ASCII byte in the token answered '
        f'{non_ascii_resp.status_code} where ASCII garbage answered '
        f'{ascii_resp.status_code}')
    assert non_ascii_resp.get_data() == ascii_resp.get_data(), (
        f'{row.id}: the refusal body distinguishes a non-ASCII token from an '
        f'ordinary wrong one — {non_ascii_resp.get_data(as_text=True)!r} vs '
        f'{ascii_resp.get_data(as_text=True)!r}')


def test_the_refusal_does_not_name_the_encoding(client, tenant_id,
                                                step_up_token):
    """One refusal path, not two.

    The alternative fix — screen the token before the comparison — would have
    answered 'Malformed step-up token' here and 'Invalid token signature' for
    ASCII garbage, telling a caller their credential was rejected for how it
    was spelled. That is a distinction with no authorization meaning behind
    it, and the second refusal path this issue asked not to create.

    MUTATION: add an is-ascii pre-check returning 'Malformed step-up token'
    -> red.
    """
    resp = client.post('/r6/fhir/Observation',
                       headers={'X-Tenant-Id': tenant_id,
                                'X-Step-Up-Token': step_up_token + 'é',
                                **_HUMAN_CONFIRMED,
                                'Content-Type': 'application/json'},
                       data=json.dumps({'resourceType': 'Observation'}))
    assert resp.status_code == 401
    assert 'Invalid token signature' in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The validator itself — the shapes no route can currently deliver
# ---------------------------------------------------------------------------

class TestTheValidatorIsTotal:
    """`validate_step_up_token` is a public function, and `require_grant`
    reads tokens from a JSON body as well as from headers. A header cannot
    carry a lone surrogate (Werkzeug decodes latin-1), but `json.loads` of
    `'"\\ud800"'` produces one — so the body source can, and `also_body_field`
    exists for endpoints to opt into. No live route uses it today; these
    cases keep the validator total for the one that will.
    """

    @pytest.mark.parametrize('token', [
        'é.abc',                       # non-ASCII payload half
        'abc.é',                       # non-ASCII signature half
        '€.€',                         # outside latin-1 in both halves
        '\ud800.abc',                  # lone surrogate: strict UTF-8 refuses
        'abc.\ud800',
        '\U0001f600.\U0001f600',
    ], ids=['payload-latin1', 'sig-latin1', 'euro', 'payload-surrogate',
            'sig-surrogate', 'astral'])
    def test_it_refuses_rather_than_raises(self, app, tenant_id, token):
        with app.app_context():
            valid, error = validate_step_up_token(token, tenant_id)
        assert valid is False
        assert error == 'Invalid token signature', (
            'a token that cannot be spelled leaves through the same door as '
            f'any other one that does not verify, not a new one: {error!r}')

    def test_a_valid_token_still_validates(self, app, tenant_id):
        """The comparison changed; what it decides did not.

        MUTATION: make `equal` return False unconditionally -> red.
        """
        with app.app_context():
            valid, error = validate_step_up_token(
                generate_step_up_token(tenant_id), tenant_id)
        assert (valid, error) == (True, None)


# ---------------------------------------------------------------------------
# The rows are the surfaces
# ---------------------------------------------------------------------------

def _require_grant_sites():
    """Every function in the production tree that calls require_grant."""
    sites = set()
    scanned = 0
    for path in _production_python_files():
        scanned += 1
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                # Both spellings: the bare name every site uses today, and
                # `access.require_grant(...)`, which a new module reaching
                # for the kernel could just as easily write.
                func = inner.func
                named = ((isinstance(func, ast.Name)
                          and func.id == 'require_grant')
                         or (isinstance(func, ast.Attribute)
                             and func.attr == 'require_grant'))
                if named:
                    sites.add(f'{rel}:{node.name}')
    return sites, scanned


def test_every_require_grant_site_has_a_row():
    """MUTATION: add a require_grant call in a new handler -> red.

    #557 was reported through one route. It was never one route's bug: the
    crash is in the validator every gate shares, so the fix is only pinned if
    the pin follows the gates rather than the route that happened to be
    measured.
    """
    sites, scanned = _require_grant_sites()
    assert scanned >= _SCAN_FLOOR, (
        f'walked only {scanned} production modules; the scan is broken and '
        'this test is reporting a green it did not measure')
    covered = {row.site for row in ROWS}
    assert sites == covered, (
        f'require_grant sites without a row: {sorted(sites - covered)}\n'
        f'rows naming a site that no longer exists: {sorted(covered - sites)}\n'
        'Every surface behind the step-up gate gets a row, so a new one '
        'cannot be added without saying how it answers a token it cannot '
        'read.')
