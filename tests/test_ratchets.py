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
#: 16 -> 13: kernel slice 6 migrated the three r6/routes.py write gates —
#: create, update and $share-bundle. Those three also carried the #478 leak:
#: they interpolated the validator's raw reason into the response, including
#: 'Token tenant mismatch', which the kernel withholds.
#: 13 -> 12: slice 5d took the actions `confirm` gate, the one slice 5 left
#: behind. It was blocked on whether a refusal may state its reason; the
#: owner ruled yes (#475), so the three reasons its contract pins survive
#: the kernel.
#: 12 -> 10 (kernel slice 7): $curatr-apply-fix's two-phase gate now goes
#: through require_grant, both phases. r6/routes.py keeps two direct sites
#: ($ingest-context at its flag-conditional gate, bind-telegram).
#: 10 -> 9 (kernel slice 7c): bind-telegram, the body-tenant site, through
#: require_grant with also_body_field. One direct site left in r6/routes.py:
#: $ingest-context, waiting on #648.
_STEP_UP_CALLSITES = 9


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
#: 88 -> 89: the BP trend route (r6/smbp/trend_routes.py). A new READ
#: route must audit — "every FHIR resource access emits an
#: AuditEvent" — and the kernel's audit() is not usable on a read
#: path yet: it never commits, and a GET that commits trips
#: test_no_new_get_route_mutates_the_store. Slice 12 takes read
#: audits as a group; adding one shim call site now is the honest
#: cost of not shipping an unaudited read.
_POST_COMMIT_AUDIT_CALLSITES = 89


def test_post_commit_audit_callsites_only_decrease():
    """MUTATION: add a record_audit_event() call anywhere -> red."""
    sites, _ = _scan(_calls_to('record_audit_event'), skip=('r6/audit.py',))
    assert len(sites) <= _POST_COMMIT_AUDIT_CALLSITES, _report(
        sites, _POST_COMMIT_AUDIT_CALLSITES,
        'New audit calls must use add_audit_event (same transaction).')


#: Packages that mutate the store and emit no audit events at all — an
#: authenticated write with no trail, the worst shape in the codebase for a
#: system whose constitution says every resource access emits an AuditEvent.
#: Playbook B1, B2. The set is now EMPTY, which makes this a tripwire rather
#: than a ratchet (playbook A7's shape): a new silent mutator goes red on
#: arrival instead of being counted.
#:
#: r6/command_center left the set on 2026-08-10 (B1) and r6/agent_runs on
#: 2026-09-04 (B2).
#:
#: TWO THINGS THIS TEST DOES NOT PROVE, and both were true of the version
#: that pinned agent_runs, so read them before trusting a green here:
#:
#: 1. It is a PRESENCE check per package, not per mutation. One audit call
#:    anywhere in a package satisfies it while every other write stays
#:    silent. The per-endpoint pin is
#:    tests/test_agent_run_writes_are_audited.py, which classifies all
#:    fourteen agent-run routes and proves each audited one at the wire;
#:    tests/test_command_center_writes_are_audited.py is B1's equivalent. A
#:    package arriving here needs one of those, not just an import.
#: 2. Until 2026-09-04 it qualified a package as a mutator only if the
#:    package validated STEP-UP. agent_runs gates ten of its fourteen
#:    endpoints on a shared secret instead, so the guard's own subject was
#:    mostly outside its scope — it caught this package by ONE call site,
#:    the step-up check inside `_tenant_authorized`, and would have missed a
#:    package that used no step-up at all (verified by mutation: with that
#:    one call renamed, the old predicate does not flag this package).
#:    The predicate below now also counts a package that
#:    calls db.session.commit(): if it commits, it mutates. Measured before
#:    the widening (2026-09-04): the commit predicate adds r6/fasten to the
#:    mutator set and NOTHING to the silent set — every other committing
#:    package already audits. So the widening is inert today and is a trap
#:    laid for tomorrow, which is what a tripwire is for.
_UNAUDITED_MUTATOR_PACKAGES = set()


def test_no_new_package_mutates_without_auditing():
    """MUTATION: rename the audit CALLS in a mutating package -> red.

    Not the import: this walks the AST for call names, so deleting
    `from r6.access import audit` while leaving `audit(...)` in place stays
    green (verified 2026-09-04). The import is not what is measured.

    A package qualifies as a mutator if it validates step-up (it guards a
    write) or commits a transaction (it performs one). If it does either and
    never calls any audit primitive, its writes are invisible.
    """
    gates, stepup_gates, audits = {}, {}, {}
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
            if name in ('validate_step_up_token', 'require_grant', 'commit'):
                gates.setdefault(package, []).append(f'{rel}:{node.lineno}')
                if name != 'commit':
                    stepup_gates.setdefault(package, []).append(
                        f'{rel}:{node.lineno}')
            elif name in ('record_audit_event', 'add_audit_event', 'audit'):
                audits.setdefault(package, []).append(f'{rel}:{node.lineno}')
    assert scanned > 40, f'the mutator scan only walked {scanned} r6 files'
    #: A predicate that matched nothing would pass this forever, and the
    #: widening above is exactly the edit that could break it silently.
    assert len(gates) > 5, (
        f'the mutator predicate matched only {len(gates)} packages — it is '
        f'broken, and an empty silent set below means nothing')
    #: The floor above does NOT pin the widening: deleting 'commit' from the
    #: tuple drops the mutator set from 8 packages to 7, which still clears
    #: `> 5`, so the 2026-09-04 strengthening could be reverted green
    #: (verified by mutation). This is the pin for it — the commit half has to
    #: still be reaching a package the step-up half does not, which is the
    #: whole reason it was added. If this ever goes empty the widening has
    #: become redundant and may be deleted, but that is then a deliberate
    #: edit here rather than a silent one above.
    commit_only = sorted(set(gates) - set(stepup_gates))
    assert commit_only, (
        "the 'commit' half of the mutator predicate now catches nothing the "
        "step-up half does not — it has been neutered, or every committing "
        "package has since adopted step-up. Do not delete this assertion to "
        "go green; decide which it is.")
    silent = {pkg for pkg in gates if pkg not in audits}
    assert silent <= _UNAUDITED_MUTATOR_PACKAGES, _report(
        sorted(silent - _UNAUDITED_MUTATOR_PACKAGES),
        len(_UNAUDITED_MUTATOR_PACKAGES),
        'A package that mutates the store must audit its writes. Presence '
        'here is not enough: add a per-endpoint pin like '
        'tests/test_agent_run_writes_are_audited.py in the same PR.')


# ---------------------------------------------------------------------------
# C — soft-delete consistency (#422)
# ---------------------------------------------------------------------------

#: Files that query R6Resource without ever mentioning is_deleted. r6/routes.py
#: filters at all 18 of its query sites; the feature modules added since filter
#: at none of theirs. Nothing has detonated because no DELETE route exists and
#: only Permission-revoke sets the flag — it is a loaded gun, not a fire.
#: Playbook F5 introduces one shared live-resource selector and takes this to 0.
#:
#: Re-verified 2026-08-16 while fixing #422, and the gun is even less loaded
#: than this said: `is_deleted = True` is written on exactly ONE line in the
#: repository (r6/routes.py:2824), inside the demo walkthrough, clearing
#: Permission rows. No route accepts DELETE. So no Patient, Observation or
#: Procedure row can carry a tombstone today by any path — which is why the
#: #422 class has never been seen in production and is also why it will land
#: silently the day a delete path ships. Fix the readers before the writer.
#:
#: 12 -> 11: #422 filtered all three queries in r6/caregaps/routes.py — the
#: ambiguity count that decides whether $care-gaps may pick a subject, the
#: demographics a supplied subject resolves to, and the clinical rows that
#: CLOSE a gap.
#: 11 -> 10: #509 filtered r6/actions/rails/form_fill.py. Its own comment
#: named deletion as the case it handled and the query did not, so a
#: tombstoned QuestionnaireResponse was rendered into a form submitted on a
#: patient's behalf.
#: 10 -> 9: council ruling D10 filtered both queries in r6/sdc/routes.py —
#: the Questionnaire/Patient resolution (_load_stored) and the auto-loaded
#: clinical content ($populate's Observation / MedicationRequest /
#: AllergyIntolerance / Condition sweep). A tombstoned row reaching an
#: intake form is the form_fill shape again, one hop upstream.
_FILES_QUERYING_WITHOUT_SOFT_DELETE = 9

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
#: 3930 -> 3931: slice 10a swapped four raw tenant reads 1:1 and added the
#: module's first `from r6.access import ...` line. A ratchet that only
#: shrinks cannot tell decomposition from growth, and the honest move is to
#: record the one line and why rather than to smuggle it in. The following
#: batches remove code from this module; this is the only batch that adds a
#: line to it.
#: 3931 -> 3922: slice 6 replaced three nine-line hand-rolled step-up gates
#: with a two-line require_grant call each. The module shrinks for the first
#: time since the kernel migration began.
#: 3922 -> 3927: the /internal/seed bundle gate. Raised deliberately, and the
#: only kind of raise this file permits — the guard has to sit at that call
#: site, and extracting seed_tenant into its own module is a refactor that
#: should not ride along with a security fix that was exploitable in prod.
#: 3927 -> 3928 (#583): one entry in the tenant-exemption table for the
#: published privacy policy (#574). The table is the guard, so the line
#: has to sit here; nothing else in that change touches this file.
#: 3928 -> 3917 (kernel slice 7): eleven lines of hand-rolled gate removed.
#: 3917 -> 3916 (kernel slice 7c).
_GOD_MODULE_LINES = 3916


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


#: Call sites that read the tenant straight off the header instead of asking
#: the access kernel, across all of r6/. 31 -> 27 -> 9: slice 10a took create,
#: read, update and search; slice 10b took the remaining seventeen in
#: r6/routes.py. 9 -> 5: slice 11a took the four MCP App pages, the first
#: multi-source sites and the first ones NOT protected by the paragraph below
#: — they sit on the exempt /mcp-apps/ prefix, so absence is real there and
#: is caught rather than raised.
#:
#: Every one of the seventeen was checked against the exempt-path list before
#: it moved, by walking each read back to the route decorator that owns it.
#: The first version of that walker matched only single-line decorators, fell
#: back to an earlier unrelated route, and reported three curatr endpoints as
#: sitting on the exempt /demo/ prefix. They are not — they are
#: /<resource_type>/<resource_id>/$curatr-*. The walker was fixed and re-run
#: before anything moved. A migration tool that is wrong in the SAFE
#: direction on Tuesday is wrong in the other direction on Wednesday.
#:
#: This counts the whole package, not just r6/routes.py. The first draft
#: scanned only the god module and pinned 20; the scan then reported 27 and
#: the pin was corrected to the measurement rather than the scan narrowed to
#: the pin. r6/fasten/routes.py and r6/rate_limit.py each hold one, and they
#: are real instances of the same thing.
#:
#: The floor is NOT zero. r6/routes.py:222 is `enforce_tenant_id` itself —
#: the before_request hook that requires and validates the header for the
#: whole blueprint. That read is the enforcement point; it is where the
#: header is supposed to be read.
#:
#: Why this is safe to do in batches, and why the batches are small: every one
#: of these sits behind `enforce_tenant_id`, the before_request hook that
#: already requires the header and format-checks it with the SAME pattern the
#: kernel uses (`[a-zA-Z0-9_-]{1,64}`, fullmatch, unstripped, compared before
#: the swap). The hook also writes the synthesized SHARP tenant back into
#: request.environ, so a downstream read sees it too. A migrated call
#: therefore re-validates a value that cannot fail — inert by construction,
#: which is what a migration slice should be.
#:
#: The exception is any handler on an EXEMPT discovery path, where the hook
#: returns early and the header may legitimately be absent. There the kernel
#: would raise TenantRejected where the old code got None, and that is a
#: behaviour change, not a refactor. Check _is_exempt_discovery_path before
#: moving a call site, per site — that is the whole reason this is 6 PRs and
#: not one sed.
#:
#: Corrected 2026-09-04: this was a single-quote, no-default-arg substring
#: needle (`"request.headers.get('X-Tenant-Id')"` matched against the raw
#: line) on a codebase where most call sites are double-quoted or carry a
#: default — `request.headers.get("X-Tenant-Id")` and
#: `request.headers.get('X-Tenant-Id', 'default')` both evaded it. The pin
#: read 5; an AST-based recount (any quote style, with or without a
#: default, plus the subscript form `request.headers['X-Tenant-Id']`) found
#: 27, including r6/agent_runs/routes.py:98 — the kernel spec's own open
#: slice 11j — and r6/wearables/routes.py:233, a call split across two
#: lines that no single-line text needle could ever have matched regardless
#: of quoting. This was decoration, not a ratchet: a migration touching
#: only double-quoted single-line call sites could have lowered the count
#: while the true number stayed flat.
_RAW_TENANT_READS = 27


def _is_tenant_header_read(node):
    """`request.headers.get('X-Tenant-Id', ...)` or `request.headers['X-Tenant-Id']`.

    Structural match, not source text: quote style and a default argument
    are call-site style choices with no bearing on the property this pin
    exists to hold — a handler reading the tenant header directly instead
    of through the kernel. A needle that requires exact source text is a
    guard that certifies whatever shape happened to exist when the needle
    was written, and nothing else — see the correction above.
    """
    def is_request_headers(expr):
        return (isinstance(expr, ast.Attribute) and expr.attr == 'headers'
                and isinstance(expr.value, ast.Name) and expr.value.id == 'request')

    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == 'get'
                and is_request_headers(func.value) and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 'X-Tenant-Id'):
            return True
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if is_request_headers(node.value):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value == 'X-Tenant-Id':
                return True
    return False


def test_raw_tenant_header_reads_only_decrease():
    """MUTATION: add `request.headers.get("X-Tenant-Id")` to a handler -> red.

    The kernel is meant to be the one tenant reader (spec §1.1). Without a
    pin, the 27 that exist today would quietly become 28 the next time
    someone needed a tenant in a hurry.
    """
    def collect(tree, path):
        return [f'{_rel(path)}:{node.lineno}' for node in ast.walk(tree)
                if _is_tenant_header_read(node)]

    sites, scanned = _scan(collect, skip=('r6/access.py',))
    assert len(sites) <= _RAW_TENANT_READS, _report(
        sites, _RAW_TENANT_READS,
        'Read the tenant through r6.access.tenant_from_request.')


def test_every_ratchet_names_its_playbook_chunk():
    """A pin without a migration plan is a number nobody will ever lower."""
    source = pathlib.Path(__file__).read_text(encoding='utf-8')
    assert 'docs/2026-08-05-healthclaw-2.0-playbook.md' in source
    for pin in ('_STEP_UP_CALLSITES', '_ROUTES_IMPORTERS',
                '_RAW_TENANT_READS',
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
