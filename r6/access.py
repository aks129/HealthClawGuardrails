"""Access kernel — one tenant reader, one step-up gate, one audit call, one
FHIR exit.

Implements `docs/2026-08-03-access-kernel-spec.md` §1. This module is slice 1
of the migration: it is **adopted by nothing**. No production module imports
it yet (pinned by `tests/test_access_kernel.py`), so it cannot change any
behavior. Adoption happens one guard and one blueprint at a time, per
`docs/2026-08-03-refactor-working-protocol.md`.

Each primitive below enforces exactly ONE property, named in its docstring.
Anything the primitive does not promise stays the caller's job — the point of
the kernel is that a reviewer can tell those apart without reading the
implementation.

§1.0 — collaborators are resolved by MODULE ATTRIBUTE, never by
`from r6.stepup import validate_step_up_token`. 33 tests monkeypatch
`r6.stepup.*` by module path; a direct import binds at import time and those
tests would keep passing while patching an object this module no longer
consults. That is the quietest way the migration can fail.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum

from flask import g, has_app_context, jsonify, request, session

from models import db
from r6 import audit as _audit_mod
from r6 import fhir_proxy as _fhir_proxy_mod
from r6 import health_compliance as _compliance_mod
from r6 import redaction as _redaction_mod
from r6 import stepup as _stepup_mod
from r6.models import AuditEventRecord
from r6.read_auth import TENANT_SESSION_KEY

logger = logging.getLogger(__name__)

__all__ = [
    'TenantSource', 'Tenant', 'TenantRejected', 'tenant_from_request',
    'Scope', 'Grant', 'StepUpDenied', 'require_grant',
    'register_error_handlers',
    'audit', 'AuditAssertionError',
    'install_audit_assertions', 'install_read_audit_assertion',
    'Profile', 'fhir_response', 'unredacted_response', 'outcome_response',
]

# The tenant-id format enforced everywhere. Same character class as
# r6/routes.py:239 (_TENANT_ID_PATTERN) — this module is where it becomes the
# only one.
_TENANT_ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,64}')

_TRUE_VALUES = frozenset({'1', 'true', 'yes'})


# ---------------------------------------------------------------------------
# §1.1 Tenant
# ---------------------------------------------------------------------------

class TenantSource(Enum):
    """Where an endpoint is willing to learn its tenant from.

    Not a ranking and not a security level. Naming the source is what makes
    "this endpoint reads the tenant from the body" a reviewable fact instead
    of a line of code someone has to find.
    """

    SESSION = 'session'   # command-center signed-link cookie (TENANT_SESSION_KEY)
    HEADER = 'header'     # X-Tenant-Id
    QUERY = 'query'       # ?tenant_id= / ?tenant= / ?t=
    BODY = 'body'         # {"tenant_id": ...}
    SHARP = 'sharp'       # synthesized from X-FHIR-Server-URL (routes.py:223-228)
    DEFAULT = 'default'   # a literal fallback the endpoint supplies


@dataclass(frozen=True)
class Tenant:
    """A tenant id that passed format validation, plus where it came from.

    Carrying the source lets an audit row and a log line say which input
    selected the tenant. It does NOT mean the caller is authorized to act on
    that tenant — see require_grant and r6.read_auth.
    """

    id: str
    source: TenantSource


class TenantRejected(Exception):
    """The request did not supply a usable tenant id.

    reason: 'absent' | 'malformed'. The two map to different FHIR
    OperationOutcome codes ('security' vs 'invalid'), both at HTTP 400,
    matching r6/routes.py:230-247 exactly.
    """

    ABSENT = 'absent'
    MALFORMED = 'malformed'

    def __init__(self, reason: str):
        if reason not in (self.ABSENT, self.MALFORMED):
            raise ValueError(
                "TenantRejected reason must be 'absent' or 'malformed'")
        super().__init__(reason)
        self.reason = reason


def _session_tenant() -> str | None:
    return session.get(TENANT_SESSION_KEY)


def _sharp_tenant() -> str | None:
    """Synthesize the SHARP tenant, exactly as r6/routes.py:223-228 does.

    Gated on is_sharp_context_active(), which is the SSRF guard: an
    unvalidated upstream URL must never mint a tenant.
    """
    if not _fhir_proxy_mod.is_sharp_context_active():
        return None
    raw = request.headers.get(_fhir_proxy_mod.SHARP_SERVER_URL_HEADER) or ''
    digest = hashlib.sha256(raw.strip().encode('utf-8')).hexdigest()[:16]
    return f'sharp-{digest}'


def tenant_from_request(
    *,
    sources: tuple[TenantSource, ...] = (TenantSource.HEADER,),
    default: str | None = None,
    query_keys: tuple[str, ...] = ('tenant_id',),
    body_key: str = 'tenant_id',
) -> Tenant:
    """Return the tenant this request names, in the caller's declared order.

    THE ONE PROPERTY: the returned id matched ``[A-Za-z0-9_-]{1,64}`` and
    came from a source this endpoint declared it accepts. Nothing else.

    ``sources`` is an ORDERED tuple, not a set. The first source in the tuple
    that yields a non-empty value wins. A set would force the kernel to
    invent a precedence, and the invented one differs from
    r6/command_center/routes.py:76-82 (session, then query, then header) —
    adopting it would silently change which tenant that page renders when a
    caller sends both. Ordering is per endpoint because today it already is.

    Raises TenantRejected('absent') when no declared source yields a value
    and ``default`` is None. Raises TenantRejected('malformed') when a source
    yields a value that fails the pattern — including a value that came from
    ``default``, so a typo'd literal fails loudly rather than at query time.

    TenantSource.DEFAULT is only consulted when ``default`` is not None, and
    it is always last regardless of its position in ``sources``.
    """
    for source in sources:
        if source is TenantSource.DEFAULT:
            # Always last, regardless of its position in `sources`.
            continue
        value = _read_source(source, query_keys=query_keys, body_key=body_key)
        if value:
            return _validated(value, source)

    if default is not None:
        return _validated(default, TenantSource.DEFAULT)

    raise TenantRejected(TenantRejected.ABSENT)


def _read_source(source: TenantSource, *, query_keys: tuple[str, ...],
                 body_key: str) -> str | None:
    """Return the raw, unvalidated value one declared source offers."""
    if source is TenantSource.SESSION:
        return _session_tenant()
    if source is TenantSource.HEADER:
        return request.headers.get('X-Tenant-Id')
    if source is TenantSource.QUERY:
        for key in query_keys:
            value = request.args.get(key)
            if value:
                return value
        return None
    if source is TenantSource.BODY:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None
        value = body.get(body_key)
        return value if isinstance(value, str) else None
    if source is TenantSource.SHARP:
        return _sharp_tenant()
    raise ValueError(f'unhandled tenant source: {source!r}')


def _validated(value: str, source: TenantSource) -> Tenant:
    # Deliberately NOT stripped. r6/routes.py:239 matches the raw header, so a
    # leading space is malformed there and must stay malformed here; trimming
    # would widen the accepted set during migration.
    if not isinstance(value, str) or not _TENANT_ID_PATTERN.fullmatch(value):
        raise TenantRejected(TenantRejected.MALFORMED)
    return Tenant(id=value, source=source)


# ---------------------------------------------------------------------------
# §1.2 Scope, Grant, require_grant
# ---------------------------------------------------------------------------

class Scope(Enum):
    """What a step-up token must be able to authorize.

    TENANT_BOUND maps to validate_step_up_token(require_scope=None) and
    accepts a read-scoped token. WRITE maps to require_scope='write' and
    rejects one. The names say what the check does, not what the caller
    wishes it did — the exact mistake behind _RESOURCE_ID_PATTERN.
    """

    TENANT_BOUND = 'tenant-bound'   # r6/read_auth.py:88-93 needs this
    WRITE = 'write'                 # every write gate today


#: Scope -> the `require_scope` argument validate_step_up_token expects.
_SCOPE_REQUIREMENT: dict[Scope, str | None] = {
    Scope.TENANT_BOUND: None,
    Scope.WRITE: 'write',
}


@dataclass(frozen=True)
class Grant:
    """Proof that a step-up token authorized THIS tenant for THIS scope.

    Frozen and carrying tenant_id so a handler scopes its query to
    grant.tenant_id rather than re-reading a header. It does not make the
    handler do that — see the spec §3(e).
    """

    tenant_id: str
    scope: Scope
    audience: str | None
    operation: str | None
    nonce_consumed: bool


class StepUpDenied(Exception):
    """A step-up check ran and refused. Rendered by the app errorhandler.

    THE ONE PROPERTY: ``checked is True`` means require_grant decided this,
    on the single line in this module that passes that flag. Any other
    StepUpDenied — raised by a helper, a test double, a copy-paste, or a
    future bug — carries checked=False, and the errorhandler RE-RAISES it so
    it surfaces as a 500.

    Without that flag the errorhandler is a hazard: it would convert an
    unexpected raise from anywhere in the request into a clean 401, which
    reads to a client exactly like a working guard. That is the retro's
    defect shape with an HTTP status on it.

    Subclasses Exception, NOT BaseException: Flask calls handle_user_exception
    from inside `except Exception` in full_dispatch_request, so a
    BaseException subclass would bypass the errorhandler entirely and reach
    the WSGI server.
    """

    def __init__(self, reason: str, *, http_status: int, checked: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.checked = checked


#: Public refusal text. The validator's own message (e.g. 'Token tenant
#: mismatch', 'Step-up token expired') is deliberately NOT propagated to the
#: client — r6/read_auth.py:262 makes the same call, and a per-reason answer
#: is an oracle for a caller probing another tenant's token.
_DENIED_ABSENT = 'Step-up token required'
_DENIED_REJECTED = 'Invalid step-up token'


def _step_up_token(*, also_bearer: bool, also_body_field: str | None) -> str:
    """Return the step-up token this request presents, in the fixed order.

    X-Step-Up-Token, then Authorization: Bearer when the endpoint opted in,
    then the named body field when the endpoint opted in. Each later source
    is opt-in for the same reason TenantSource is: a reader must be able to
    see which inputs an endpoint trusts without reading this helper.
    """
    token = (request.headers.get('X-Step-Up-Token') or '').strip()
    if token:
        return token

    if also_bearer:
        auth = (request.headers.get('Authorization') or '').strip()
        if auth.lower().startswith('bearer '):
            token = auth[7:].strip()
            if token:
                return token

    if also_body_field:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            value = body.get(also_body_field)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ''


def require_grant(
    *,
    scope: Scope,
    tenant: Tenant,
    audience: str | None = None,
    operation: str | None = None,
    consume_nonce: bool = False,
    also_bearer: bool = False,
    also_body_field: str | None = None,
    absent_status: int = 401,
    rejected_status: int = 401,
) -> Grant:
    """Require a step-up token bound to ``tenant`` and return the Grant.

    THE ONE PROPERTY: a Grant exists only if validate_step_up_token returned
    True for this tenant, scope, audience and operation. There is no tuple to
    mis-destructure, no truthy object to test, and no status-code choice at
    the call site.

    Token sources are read in this order. First X-Step-Up-Token. Then
    Authorization: Bearer when ``also_bearer`` (r6/read_auth.py:83-86,
    r6/actions/routes.py:256-260). Then the named body field when
    ``also_body_field`` (r6/routes.py:2097-2098).

    ``absent_status`` / ``rejected_status`` exist because the same failure
    answers 401 at 9 sites and 403 at 3. Normalizing them is the founder's
    call, not this migration's; each migrated site passes the status it
    answers TODAY.

    Raises StepUpDenied with the checked flag set. Never returns None, never
    returns False, never returns a tuple.
    """
    if not isinstance(tenant, Tenant):
        # A raw string here would mean the caller skipped format validation.
        raise TypeError(
            'require_grant needs a Tenant from tenant_from_request, '
            f'not {type(tenant).__name__}')

    token = _step_up_token(also_bearer=also_bearer,
                           also_body_field=also_body_field)
    if not token:
        reason, status = _DENIED_ABSENT, absent_status
    else:
        # Destructure both halves. A truthiness test on the tuple is a silent
        # auth bypass — this module exists so that idiom has one home.
        valid, error = _stepup_mod.validate_step_up_token(
            token,
            tenant.id,
            consume_nonce=consume_nonce,
            require_scope=_SCOPE_REQUIREMENT[scope],
            require_audience=audience,
            require_operation=operation,
        )
        if valid:
            return Grant(
                tenant_id=tenant.id,
                scope=scope,
                audience=audience,
                operation=operation,
                nonce_consumed=consume_nonce,
            )
        logger.info('step-up refused for tenant %s: %s', tenant.id, error)
        reason, status = _DENIED_REJECTED, rejected_status

    # The ONLY place in the repository that sets the checked flag. Pinned by
    # test_the_checked_flag_is_set_in_exactly_one_place.
    raise StepUpDenied(reason, http_status=status, checked=True)


def register_error_handlers(app) -> None:
    """Register the StepUpDenied and TenantRejected renderers app-wide.

    HANDLERS ONLY. This registers no before_request hook and changes no
    blueprint's request pipeline. r6/sdc/delivery.py:8-11 is deliberately off
    r6_blueprint because the HMAC signature in the URL is the credential and
    the route must work with no headers. Extending the hooks would break it.
    An errorhandler is inert until something raises.

    A StepUpDenied whose checked flag is unset is re-raised here, unhandled,
    and becomes a 500.
    """
    app.register_error_handler(StepUpDenied, _render_step_up_denied)
    app.register_error_handler(TenantRejected, _render_tenant_rejected)


def _render_step_up_denied(exc: StepUpDenied):
    if not exc.checked:
        # Not a decision this kernel made. Rendering it as a clean 401 would
        # make an unrelated bug look exactly like a working guard.
        raise exc
    return outcome_response('error', 'security', exc.reason,
                            status=exc.http_status)


def _render_tenant_rejected(exc: TenantRejected):
    if exc.reason == TenantRejected.ABSENT:
        return outcome_response('error', 'security',
                                'X-Tenant-Id header is required', status=400)
    return outcome_response('error', 'invalid',
                            'X-Tenant-Id must match [a-zA-Z0-9_-]{1,64}',
                            status=400)


# ---------------------------------------------------------------------------
# §1.3 Audit
# ---------------------------------------------------------------------------

#: flask.g attribute set when an AuditEventRecord goes through a flush, and
#: CLEARED when the session commits or rolls back that transaction. Set means
#: "an audit row is flushed into a transaction nobody has resolved yet" — the
#: state install_audit_assertions refuses to let a request end in.
#:
#: The spec words this condition as "db.session.new or db.session.dirty is
#: non-empty at teardown". Measured, that condition can never fire: flush()
#: moves the AuditEvent out of session.new into the persistent identity map,
#: so both sets are empty for exactly the request this control exists to
#: catch. Reading session.new from inside after_flush is the same property
#: with a signal that actually observes it. See _install_marker_listeners.
_AUDIT_PENDING = '_hc_access_audit_pending'

#: flask.g attribute recording that audit() ran at least once in this request,
#: whatever happened to the transaction afterwards. This is what
#: install_read_audit_assertion needs — a read that audited AND committed must
#: pass, so it cannot share the pending marker above.
_AUDIT_EMITTED = '_hc_access_audit_emitted'

#: SQLAlchemy session listeners are process-global, so they are installed once
#: per process rather than once per app. Registering per app would stack one
#: listener per Flask app the test suite builds.
_marker_listeners_installed = False


class AuditAssertionError(AssertionError):
    """A request-lifecycle audit assertion failed.

    Testing-mode only — see install_audit_assertions and
    install_read_audit_assertion. Not part of the spec's §1 signature list;
    the two installers need something to raise.
    """


def audit(
    *,
    tenant: Tenant | str,
    event_type: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    agent_id: str | None = None,
    context_id: str | None = None,
    outcome: str = 'success',
    detail: str | None = None,
    outcome_detail_code: str | None = None,
) -> None:
    """Add and flush an AuditEvent inside the CALLER's transaction.

    THE ONE PROPERTY: when this returns, an AuditEvent row is flushed in the
    caller's unit of work, and it will be committed or rolled back with the
    state change it describes. It never commits.

    This is r6/audit.py:46-61 (add_audit_event) becoming the only pattern.
    r6/audit.py:64-113 (record_audit_event) opens a SAVEPOINT and then calls
    db.session.commit() — an ambient commit at 41 call sites, which means a
    caller cannot know whether its own pending work was committed as a side
    effect of auditing.

    ``detail`` stays PHI-free. The kernel does not sanitize it, because a
    sanitizer that silently drops PHI is worse than a reviewer who sees the
    string. Composition of the detail line stays where the facts are.

    Records that an audit was emitted on ``g`` for
    install_read_audit_assertion. The PENDING marker install_audit_assertions
    reads is not set here — it is set by the flush that carries the row, so a
    call that wrote nothing leaves nothing pending (#321).
    """
    tenant_id = tenant.id if isinstance(tenant, Tenant) else tenant
    _install_marker_listeners()
    _audit_mod.add_audit_event(
        event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        agent_id=agent_id,
        context_id=context_id,
        outcome=outcome,
        detail=detail,
        tenant_id=tenant_id,
        outcome_detail_code=outcome_detail_code,
    )
    if has_app_context():
        setattr(g, _AUDIT_EMITTED, True)


def _set_pending_marker(session_, _flush_context, *_args) -> None:
    """An AuditEventRecord is going to the database in an open transaction."""
    if not has_app_context():
        return
    if any(isinstance(obj, AuditEventRecord) for obj in session_.new):
        setattr(g, _AUDIT_PENDING, True)


def _clear_pending_marker(*_args) -> None:
    """The audit row's transaction was resolved — nothing is pending."""
    if has_app_context():
        g.pop(_AUDIT_PENDING, None)


def _clear_on_soft_rollback(_session, previous_transaction=None) -> None:
    """after_soft_rollback also fires for a SAVEPOINT, which resolves nothing.

    A nested rollback leaves the audit row pending in the OUTER transaction, so
    clearing on it is a false negative — and it is the exact path
    record_audit_event takes when the audit write fails (r6/audit.py:105), the
    function slices 12 and 13 migrate away from.

    Do NOT write this as session.in_transaction(): inside after_commit the
    session still reports a transaction, so that variant breaks the commit
    cases. previous_transaction.nested is the discriminator.
    """
    if previous_transaction is not None and previous_transaction.nested:
        return
    _clear_pending_marker()


def _install_marker_listeners() -> None:
    """Attach the session listeners that set and clear the pending marker.

    Set and clear run on the same mechanism on purpose. The marker follows the
    ROW — an AuditEventRecord in a flush — not the call to audit(). Keying it
    to the call makes a faked writer that wrote nothing fail its request, with
    the assertion firing when its property was never violated (#321).

    session.new is read inside after_flush, where SQLAlchemy still holds it in
    its pre-flush state. At teardown it is empty for exactly the request this
    control exists to catch, which is why the spec's original condition could
    never fire.

    Idempotent and once per process: SQLAlchemy session events are global, so
    installing per app would stack one listener per Flask app the suite makes.
    """
    global _marker_listeners_installed
    if _marker_listeners_installed:
        return
    from sqlalchemy import event
    event.listen(db.session, 'after_flush', _set_pending_marker)
    event.listen(db.session, 'after_commit', _clear_pending_marker)
    event.listen(db.session, 'after_soft_rollback', _clear_on_soft_rollback)
    _marker_listeners_installed = True


def _assertions_enabled(app) -> bool:
    if app.config.get('TESTING'):
        return True
    return os.environ.get('HC_ASSERT_AUDIT_COMMITTED', '').strip().lower() \
        in _TRUE_VALUES


def _install_marker_cleanup(app) -> None:
    """Drop both markers at the end of every request.

    flask.g is app-context scoped, and a test that pushes one app context
    around several client calls shares g between them. Without this, request N
    would inherit request N-1's audit markers and the assertions would be
    reading the wrong request. Registered before either assertion so it runs
    LAST (teardown functions run in reverse registration order).
    """
    if getattr(app, '_hc_access_marker_cleanup', False):
        return
    app._hc_access_marker_cleanup = True

    @app.teardown_request
    def _clear_audit_markers(_exc=None):
        g.pop(_AUDIT_PENDING, None)
        g.pop(_AUDIT_EMITTED, None)


def install_audit_assertions(app) -> None:
    """Testing-mode only: fail a request that flushed audit rows and never
    resolved the transaction.

    THE ONE PROPERTY: no request leaves an audit row flushed-but-uncommitted.

    Installed as a teardown_request, active only when app.config['TESTING']
    or HC_ASSERT_AUDIT_COMMITTED is set. It fires when g still carries the
    pending marker at teardown — the signature of a handler that flushed an
    AuditEvent and returned without commit() or rollback(). An outer rollback
    clears the marker and passes, correctly: a refused write should carry no
    audit row. A SAVEPOINT rollback does not clear it, because it resolves
    nothing.

    Without this, moving 41 sites from an ambient commit to a caller-owned
    commit is 41 chances to drop an audit row behind a 201.

    WHAT IT DOES NOT CATCH (see docs/2026-08-03-audit-assertion-ruling.md for
    the full list of seven): a handler that flushes an audit row, rolls the
    transaction back, and still answers 2xx. The marker clears and this passes,
    though the audit row is gone from behind a success. That needs a second
    control, not a change to this one.
    """
    _install_marker_cleanup(app)
    _install_marker_listeners()

    @app.teardown_request
    def _assert_audit_committed(exc=None):
        if exc is not None:
            # The request is already failing. Flask-SQLAlchemy rolls back on
            # its own teardown; masking the real error with ours helps nobody.
            return
        if not _assertions_enabled(app):
            return
        if getattr(g, _AUDIT_PENDING, False):
            raise AuditAssertionError(
                'audit() flushed a row and the request returned without '
                'commit() or rollback() — the AuditEvent is pending in an '
                'unresolved transaction')


#: A path under this prefix whose next segment starts with an uppercase letter
#: is a FHIR resource route. Discovery paths (metadata, health, docs,
#: .well-known) are lowercase by construction, so this separates them without
#: importing r6.routes' exemption list into the kernel.
_FHIR_ROOT = '/r6/fhir/'


def _is_fhir_resource_path(path: str) -> bool:
    if not path.startswith(_FHIR_ROOT):
        return False
    rest = path[len(_FHIR_ROOT):]
    return bool(rest) and rest[0].isupper()


def install_read_audit_assertion(app) -> None:
    """Testing-mode only: fail a FHIR read that returned without auditing.

    THE ONE PROPERTY: a request to a /r6/fhir/ resource route that answered
    200 or 404 flushed at least one AuditEvent.

    This is the ONLY structural answer to S-9, the five unaudited 404 paths
    (r6/routes.py:596-598, 692-694, 1268-1270, 1810-1812, 3106-3110). audit()
    is a function you must call, so the kernel alone cannot make an early
    ``return 404`` audit itself.

    It lands in its own slice and it goes red on those five paths
    immediately. That redness IS the S-9 pin. Do not install it in the same
    PR as the fix — which is why slice 1 defines it and registers it nowhere.
    """
    _install_marker_cleanup(app)

    @app.after_request
    def _assert_read_audited(response):
        if not _assertions_enabled(app):
            return response
        if request.method != 'GET':
            return response
        if response.status_code not in (200, 404):
            return response
        if not _is_fhir_resource_path(request.path):
            return response
        if not getattr(g, _AUDIT_EMITTED, False):
            raise AuditAssertionError(
                f'{request.method} {request.path} answered '
                f'{response.status_code} without emitting an AuditEvent')
        return response


# ---------------------------------------------------------------------------
# §1.4 Response shaping
# ---------------------------------------------------------------------------

class Profile(Enum):
    """Which redaction policy produced this payload.

    There is deliberately NO member meaning "no redaction". An enum member
    for the absence of a property makes the absence look like a policy, and
    a reviewer scanning for the risky case sees a valid-looking constant. The
    opt-out is a different function with a different signature — see
    unredacted_response.
    """

    STANDARD = 'standard'                      # r6/redaction.py:22 + add_disclaimer
    PATIENT_CONTROLLED = 'patient-controlled'  # r6/redaction.py:180
    INTAKE = 'intake'                          # r6/routes.py:3318 _intake_strip


_SSN_SYSTEMS = ('http://hl7.org/fhir/sid/us-ssn',
                'urn:oid:2.16.840.1.113883.4.1')


def _intake_profile(resource):
    """Intake profile: identified for clinic check-in (name/DOB/address/telecom
    preserved) but SSN-class identifiers and clinician free-text never ship.

    Byte-for-byte the behavior of r6/routes.py:_intake_strip, which slice 14
    deletes when $share-bundle adopts fhir_response. Until then the two are
    pinned equal by test_intake_profile_matches_the_shipped_intake_strip, so
    the copy cannot drift while it waits.
    """
    res = resource
    res.pop('note', None)
    res.pop('text', None)
    idents = res.get('identifier')
    if isinstance(idents, list):
        kept = [i for i in idents
                if not (isinstance(i, dict) and i.get('system') in _SSN_SYSTEMS)]
        if kept:
            res['identifier'] = kept
        else:
            res.pop('identifier', None)
    return res


def fhir_response(payload, *, profile: Profile, status: int = 200,
                  resource_type: str | None = None, etag: str | None = None,
                  patient_id: str | None = None):
    """The only exit for a FHIR payload that carries patient data.

    THE ONE PROPERTY: the body was produced by the named redaction profile.

    Profile.STANDARD calls apply_redaction, which strips every upstream
    ``display`` and ``CodeableConcept.text`` and then re-applies labels from
    r6/terminology.py keyed by code (r6/redaction.py:22-38). The kernel does
    not reorder that. It only removes the choice of whether to call it.

    ``patient_id`` is required by Profile.PATIENT_CONTROLLED and rejected by
    the others. The spec's §1.4 signature omits it, but
    apply_patient_controlled_redaction(resource, patient_id) cannot be called
    without it — see the PR body.
    """
    if not isinstance(profile, Profile):
        raise TypeError('fhir_response needs a Profile member')
    if profile is Profile.PATIENT_CONTROLLED and not patient_id:
        raise ValueError(
            'Profile.PATIENT_CONTROLLED needs the canonical patient_id it '
            'injects as the sole identifier')
    if profile is not Profile.PATIENT_CONTROLLED and patient_id:
        raise ValueError(
            f'patient_id is meaningless for {profile.value}; it would read as '
            'though patient-controlled redaction had run')

    if profile is Profile.STANDARD:
        body = _redaction_mod.apply_redaction(payload)
        body = _compliance_mod.add_disclaimer(body, resource_type)
    elif profile is Profile.PATIENT_CONTROLLED:
        body = _redaction_mod.apply_patient_controlled_redaction(
            payload, patient_id)
    else:
        body = _intake_profile(payload)

    response = jsonify(body)
    response.status_code = status
    if etag:
        response.headers['ETag'] = etag
    return response


#: Endpoint names permitted to answer with an unredacted payload. Adding a
#: name here is a deliberate two-file change: this frozenset, and the literal
#: expected set in tests/test_unredacted_exits.py.
_UNREDACTED_EXITS: frozenset[str] = frozenset({
    # populated slice by slice; each entry names its reason in the test
    'r6.subscription_topics',   # SubscriptionTopic is server metadata (routes.py:1671)
    'r6.audit_search',          # AuditEventRecord is PHI-free by construction
})


def unredacted_response(payload, *, endpoint: str, reason: str,
                        status: int = 200):
    """Answer with a payload no redaction profile touched.

    THE ONE PROPERTY: ``endpoint`` appears in _UNREDACTED_EXITS. Raises
    RuntimeError otherwise, at request time, in every environment.

    ``reason`` is a required sentence for the reviewer and is never sent to
    the client. A required argument nobody can leave blank is what turns
    "this path applies no redaction" from an omission into a claim.

    r6/routes.py:719-723 (update_resource returning the stored resource) is
    NOT on the allowlist. It is S-11, a defect, and it migrates to
    fhir_response(profile=Profile.STANDARD) with its own PR and its own pin.
    """
    if endpoint not in _UNREDACTED_EXITS:
        raise RuntimeError(
            f"'{endpoint}' is not an approved unredacted exit; add it to "
            'r6.access._UNREDACTED_EXITS and to tests/test_unredacted_exits.py '
            'or answer through fhir_response()')
    if not reason or not reason.strip():
        raise ValueError('unredacted_response requires a written reason')

    response = jsonify(payload)
    response.status_code = status
    return response


def outcome_response(severity: str, code: str, diagnostics: str, *,
                     status: int):
    """Build a FHIR OperationOutcome. This is r6/routes.py:3705-3716 with a
    status attached.

    Separate from fhir_response because an OperationOutcome carries text this
    system authored, never stored patient data. Running it through a
    redaction profile would be a no-op that teaches a reader the wrong thing
    about what fhir_response protects.

    ``diagnostics`` must not contain a token, an id from another tenant, or a
    driver exception string.
    """
    response = jsonify({
        'resourceType': 'OperationOutcome',
        'issue': [
            {
                'severity': severity,
                'code': code,
                'diagnostics': diagnostics,
            }
        ],
    })
    response.status_code = status
    return response
