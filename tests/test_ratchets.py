"""Architecture ratchets — the counts that define HealthClaw 2.0.

Every guardrail guarantee in this codebase is currently a *convention*
repeated at N call sites rather than a *structure* enforced at one: audit is
called from 88 places, the tenant header is read raw in dozens, step-up is
validated directly at 23. That is the generator behind the week of
"a guardrail produced nothing and the caller read it as an answer" defects
(`docs/2026-08-02-retro.md`), and driving those counts to zero is what
`docs/2026-08-05-healthclaw-2.0-playbook.md` means by 2.0.

A ratchet is a pin on a count that may only go DOWN. Each migration PR
lowers its pin in the same commit that lowers the count — the §6 rule from
`docs/agent-task-guide.md` applied to architecture instead of behavior. A PR
that adds a new call site goes red, which is the point: the old path stops
being a thing you can quietly reach for.

These tests assert nothing about behavior. They are the architecture spec,
made executable so that backsliding is a red build rather than a review
comment somebody has to notice.

WHEN YOU MIGRATE: lower the number, keep the comment honest about what is
left. Never raise a pin to make a build pass — that is the one edit that
turns this file into decoration.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_PRODUCTION_DIRS = ('r6', 'careagents', 'adapters', 'api', 'services',
                    'scripts', 'openclaw', 'hermes', 'migrations')
_PRODUCTION_ROOT_FILES = ('main.py', 'app.py', 'models.py')

#: A scan that walks nothing passes every ratchet forever — the false green
#: that is this repo's own defect shape pointed at its own test suite. Every
#: counter asserts this floor, so a broken path list fails loudly instead of
#: reporting perfect compliance. 172 production modules exist today.
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
            if '__pycache__' in path.parts:
                continue
            yield path


def _rel(path):
    return str(path.relative_to(REPO_ROOT))


def _scan(collect, *, skip=()):
    """Walk production modules, collecting `collect(tree, path) -> [str]`.

    Returns (sites, scanned). Counting is AST-based on purpose: grep counts
    the word in a docstring that *describes* the old primitive and reports a
    migration as incomplete when it is done, or counts a commented-out line
    and hides one that is not.
    """
    sites, scanned = [], 0
    for path in _production_python_files():
        rel = _rel(path)
        if rel in skip:
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        sites.extend(collect(tree, path))
    assert scanned > _SCAN_FLOOR, (
        f'the ratchet scan only walked {scanned} files — the path list is '
        f'broken, and every count below is meaningless until it is fixed')
    return sites, scanned


def _called_name(node):
    """The bare name of a call target: f(), obj.f(), a.b.f() -> 'f'."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_to(name):
    def collect(tree, path):
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == name:
                found.append(f'{_rel(path)}:{node.lineno}')
        return found
    return collect


def _report(sites, pin, what):
    """A failure message that names the sites, so the fix is obvious."""
    listing = '\n  '.join(sites[:40])
    more = '' if len(sites) <= 40 else f'\n  ... and {len(sites) - 40} more'
    return (f'{what}\n'
            f'  pinned at {pin}, found {len(sites)}\n  {listing}{more}')


# ---------------------------------------------------------------------------
# A — the kernel becomes the only path
# ---------------------------------------------------------------------------

#: Direct step-up validation. `r6/access.require_grant` is the replacement:
#: it raises rather than returning a (bool, str) tuple that a truthiness test
#: silently misreads as authorized (#307). r6/access.py itself is excluded —
#: it is the one module that is *supposed* to call this.
#: Playbook chunks A2, A3, A6.
#:
#: 20 -> 19: kernel slice 4 migrated `/wearables/sync-now`. That slice's job
#: was to prove the MINORITY 403 dialect survives migration without being
#: normalized, so require_grant carries absent_status=403 and
#: rejected_status=403 and the wire behaviour is byte-identical.
#: 19 -> 16: kernel slice 5 migrated three of the four actions gates —
#: rx-transfer propose (also_bearer), commit, and review's _require_step_up.
#: The fourth, `confirm`, is deliberately NOT migrated: its wire contract
#: includes the REASON a token was refused, and the kernel's uniform
#: OperationOutcome does not carry it. See the comment at that call site.
_STEP_UP_CALLSITES = 16


def test_direct_step_up_validation_only_decreases():
    """MUTATION: add a validate_step_up_token() call anywhere -> red."""
    sites, _ = _scan(_calls_to('validate_step_up_token'),
                     skip=('r6/access.py',))
    assert len(sites) <= _STEP_UP_CALLSITES, _report(
        sites, _STEP_UP_CALLSITES,
        'Direct step-up validation should route through r6.access.require_grant.')


#: Modules that reach into r6/routes.py for a symbol. Every one of these is a
#: utility that leaked out of a 3,900-line module because there was nowhere
#: else to put it.
#:
#: A1 moved the two that formed import cycles — the env predicates now live in
#: r6/runtime_config, the body guard in r6/body_guard — and
#: tests/test_import_acyclicity.py holds that line directly. What is left is
#: main.py importing the blueprint, which is the point, and three imports of
#: `authenticate_tenant_read`.
#:
#: That one is deliberately still here. It needs an OperationOutcome builder,
#: which is `r6.access.outcome_response` — so moving it is kernel slice work
#: (A2-A5) rather than a cut-and-paste, and doing it here would either
#: duplicate the builder or adopt the kernel outside its own slice. Ratchet
#: reaches 1, not 0.
_ROUTES_IMPORTERS = 4


def test_imports_out_of_the_god_module_only_decrease():
    """MUTATION: add `from r6.routes import x` anywhere -> red."""
    def collect(tree, path):
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'r6.routes':
                found.append(f'{_rel(path)}:{node.lineno}')
        return found
    sites, _ = _scan(collect)
    assert len(sites) <= _ROUTES_IMPORTERS, _report(
        sites, _ROUTES_IMPORTERS,
        'Nothing but main.py should import from r6/routes.py.')


# ---------------------------------------------------------------------------
# B — audit correctness
# ---------------------------------------------------------------------------

#: `record_audit_event` commits the audit row in its own SAVEPOINT *after*
#: the caller has already committed the data, and its db.session.commit()
#: also commits whatever else the caller had pending. A failed audit
#: therefore 500s the request with the resource already persisted and
#: unaudited: fail-closed on the response, fail-open on the data.
#:
#: `add_audit_event` flushes inside the caller's transaction and never
#: commits — same transaction, genuinely fail-closed. Playbook B3-B9 moves
#: these one blueprint per PR; B9 deletes the old primitive when this is 0.
_POST_COMMIT_AUDIT_CALLSITES = 88


def test_post_commit_audit_callsites_only_decrease():
    """MUTATION: add a record_audit_event() call anywhere -> red."""
    sites, _ = _scan(_calls_to('record_audit_event'), skip=('r6/audit.py',))
    assert len(sites) <= _POST_COMMIT_AUDIT_CALLSITES, _report(
        sites, _POST_COMMIT_AUDIT_CALLSITES,
        'New audit calls must use add_audit_event (same transaction).')


#: The two step-up-gated mutators that emit no audit events at all. They
#: perform authenticated writes with no trail — the worst shape in the
#: codebase for a system whose constitution says every FHIR resource access
#: emits an AuditEvent. Playbook B1, B2. This ratchet counts blueprints
#: that mutate without auditing; it goes to 0 and then becomes a tripwire.
#: r6/command_center left this set on 2026-08-10 (playbook B1): its three
#: step-up-gated writes now call add_audit_event inside their own
#: transaction. r6/agent_runs stays, and stays deliberately — claim,
#: heartbeat and transition fire on a timer, so auditing them would bury
#: real access records under queue chatter. B2 is a decision about WHICH of
#: its twelve endpoints deserve a trail, not a sweep.
_UNAUDITED_MUTATOR_PACKAGES = {'r6/agent_runs'}


def test_no_new_package_mutates_without_auditing():
    """MUTATION: delete the audit import from a gated blueprint -> red.

    A package qualifies as a mutator if it validates step-up (it guards a
    write). If it does that and never calls either audit primitive, the
    write is authenticated and invisible.
    """
    gates, audits = {}, {}
    scanned = 0
    for path in _production_python_files():
        rel = _rel(path)
        if not rel.startswith('r6/') or rel == 'r6/access.py':
            continue
        package = '/'.join(rel.split('/')[:2]) if '/' in rel[3:] else 'r6'
        scanned += 1
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name == 'validate_step_up_token' or name == 'require_grant':
                gates.setdefault(package, []).append(f'{rel}:{node.lineno}')
            elif name in ('record_audit_event', 'add_audit_event', 'audit'):
                audits.setdefault(package, []).append(f'{rel}:{node.lineno}')
    assert scanned > 40, f'the mutator scan only walked {scanned} r6 files'
    silent = {pkg for pkg in gates if pkg not in audits}
    assert silent <= _UNAUDITED_MUTATOR_PACKAGES, _report(
        sorted(silent - _UNAUDITED_MUTATOR_PACKAGES),
        len(_UNAUDITED_MUTATOR_PACKAGES),
        'A package that gates writes with step-up must audit them.')


# ---------------------------------------------------------------------------
# C — soft-delete consistency (#422)
# ---------------------------------------------------------------------------

#: Files that query R6Resource without ever mentioning is_deleted. r6/routes.py
#: filters at all 18 of its query sites; the feature modules added since filter
#: at none of theirs. Nothing has detonated because no DELETE route exists and
#: only Permission-revoke sets the flag — it is a loaded gun, not a fire.
#: Playbook F5 introduces one shared live-resource selector and takes this to 0.
_FILES_QUERYING_WITHOUT_SOFT_DELETE = 12

#: r6/purge.py hard-deletes a tenant's rows. It must NOT filter is_deleted —
#: a purge that skipped soft-deleted rows would leave exactly the records the
#: caller asked to destroy. Exempt on purpose, so nobody "fixes" it.
_SOFT_DELETE_EXEMPT = ('r6/purge.py',)


def test_soft_delete_blind_query_files_only_decrease():
    """MUTATION: strip is_deleted from a query in r6/routes.py -> red."""
    def collect(tree, path):
        source = path.read_text(encoding='utf-8')
        if 'R6Resource' not in source or 'query' not in source:
            return []
        queries = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and n.attr == 'query']
        if not queries:
            return []
        if 'is_deleted' in source:
            return []
        return [f'{_rel(path)}:{queries[0].lineno}']
    sites, _ = _scan(collect, skip=_SOFT_DELETE_EXEMPT)
    assert len(sites) <= _FILES_QUERYING_WITHOUT_SOFT_DELETE, _report(
        sites, _FILES_QUERYING_WITHOUT_SOFT_DELETE,
        'A resource query that ignores is_deleted reads deleted rows.')


#: r6/routes.py, in lines. The 08-02 audit's Workstream B is to decompose it
#: into a package; that work is sequenced after the access kernel and has not
#: started. In the meantime the file has been going the wrong way — 3,762
#: lines at the audit, 3,905 at the 08-05 pattern review, 3,930 today, against
#: a plan whose whole purpose is to shrink it.
#:
#: Nobody decided to grow it. Each PR added twenty lines to the only module
#: where the thing they needed already lived, which is exactly how it reached
#: 3,900 in the first place. A number that can only go down makes that visible
#: in the PR that does it rather than in the next audit.
#:
#: This is a CEILING, not a target. Lower it when Workstream B lands a module;
#: raising it needs a reason in the diff.
#: Playbook chunk B (docs/2026-08-05-healthclaw-2.0-playbook.md).
_GOD_MODULE_LINES = 3930


def test_the_god_module_only_shrinks():
    """MUTATION: add a line to r6/routes.py -> red.

    The one ratchet that fires on ordinary feature work, deliberately. If a
    change genuinely belongs in routes.py the pin moves up in the same PR
    with the reason; the point is that it is a decision rather than a drift.
    """
    lines = len((REPO_ROOT / 'r6/routes.py').read_text(encoding='utf-8').splitlines())
    assert lines <= _GOD_MODULE_LINES, (
        f'r6/routes.py is {lines} lines, pinned at {_GOD_MODULE_LINES}.\n'
        f'  Workstream B exists to shrink this file. If the change truly '
        f'belongs here, raise the pin in this PR and say why; if it belongs '
        f'in a new module, that is the refactor starting.')


# ---------------------------------------------------------------------------
# The ratchets themselves
# ---------------------------------------------------------------------------

def test_every_ratchet_names_its_playbook_chunk():
    """A pin without a migration plan is a number nobody will ever lower."""
    source = pathlib.Path(__file__).read_text(encoding='utf-8')
    assert 'docs/2026-08-05-healthclaw-2.0-playbook.md' in source
    for pin in ('_STEP_UP_CALLSITES', '_ROUTES_IMPORTERS',
                '_POST_COMMIT_AUDIT_CALLSITES',
                '_FILES_QUERYING_WITHOUT_SOFT_DELETE',
                '_GOD_MODULE_LINES'):
        assert f'{pin} = ' in source, f'{pin} lost its pin'


@pytest.mark.parametrize('pin,value', [
    ('step-up callsites', _STEP_UP_CALLSITES),
    ('routes.py importers', _ROUTES_IMPORTERS),
    ('post-commit audit callsites', _POST_COMMIT_AUDIT_CALLSITES),
    ('soft-delete-blind files', _FILES_QUERYING_WITHOUT_SOFT_DELETE),
    ('god-module lines', _GOD_MODULE_LINES),
])
def test_no_ratchet_is_already_at_zero(pin, value):
    """When one reaches 0, delete its ratchet and add the grep guard instead.

    A ratchet pinned at 0 is doing a tripwire's job with a counter's
    machinery. The playbook's chunk A7 is exactly this transition for the
    tenant-header read: ratchet until 0, then forbid outright.
    """
    assert value > 0, (
        f'{pin} is at 0 — replace the ratchet with a hard guard (playbook A7)')
