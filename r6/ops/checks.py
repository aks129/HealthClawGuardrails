"""Config preflight checks (spec W0: "preflight green = stage precondition").

Contract: every check returns {name, ok, detail, fatal}. `fatal` marks the
checks that gate the overall verdict — GET /r6/ops/preflight reports
ok = all fatal checks ok. Non-fatal reds are surfaced (a dark rail fails
loud at execution time; preflight just makes it visible before then).

The checks are GLOBAL (process/env truth), even though the endpoint is
tenant-authenticated like every other write-shaped surface.
"""
import os
from datetime import timedelta

from sqlalchemy import text

from models import db
from r6.actions.events import ActionEvent
from r6.actions.models import _utcnow
from r6.actions.registry import all_kinds, get_executor

# The docker-compose fallback value (docker-compose.yml). Running with it in
# any real environment means anyone who has read the repo can forge tokens.
COMPOSE_DEFAULT_STEP_UP_SECRET = 'dev-step-up-secret-change-in-production'
MIN_SECRET_LENGTH = 16

# reaper_heartbeat goes red when the newest reaper transition is older than
# this (the external cron ticks every 5 minutes; 15 = three missed ticks).
HEARTBEAT_STALE = timedelta(minutes=15)


def _result(name, ok, detail, fatal):
    return {'name': name, 'ok': bool(ok), 'detail': detail, 'fatal': fatal}


def _is_production():
    return os.environ.get('FLASK_ENV') == 'production'


def check_step_up_secret():
    """STEP_UP_SECRET signs every step-up token — the write/execute gate."""
    secret = os.environ.get('STEP_UP_SECRET', '')
    if not secret:
        return _result(
            'step_up_secret', False,
            'STEP_UP_SECRET is not set — no step-up token can be minted or '
            'validated; every commit/confirm rejects.', True)
    if secret == COMPOSE_DEFAULT_STEP_UP_SECRET:
        return _result(
            'step_up_secret', False,
            'STEP_UP_SECRET is the docker-compose default — anyone who has '
            'read the repo can forge step-up tokens. Set a real secret.',
            True)
    if len(secret) < MIN_SECRET_LENGTH:
        return _result(
            'step_up_secret', False,
            'STEP_UP_SECRET is shorter than %d characters — too weak for an '
            'HMAC signing key.' % MIN_SECRET_LENGTH, True)
    return _result('step_up_secret', True,
                   'set (%d chars, not the compose default)' % len(secret),
                   True)


def check_fasten_webhook_secret():
    """r6/fasten/verify.py fails CLOSED without it — every provider webhook
    is silently rejected and records never arrive."""
    if os.environ.get('FASTEN_WEBHOOK_SECRET', '').strip():
        return _result('fasten_webhook_secret', True, 'set', True)
    return _result(
        'fasten_webhook_secret', False,
        'FASTEN_WEBHOOK_SECRET is not set — Fasten webhooks silently 401 '
        '(fail-closed) and patient record deliveries never arrive.', True)


def check_internal_mint_secret():
    """Non-public token mints fail closed without it (r6/routes.py)."""
    fatal = _is_production()
    if os.environ.get('INTERNAL_TOKEN_MINT_SECRET'):
        return _result('internal_mint_secret', True, 'set', fatal)
    return _result(
        'internal_mint_secret', False,
        'INTERNAL_TOKEN_MINT_SECRET is not set — non-public token mints '
        'fail closed%s.' % (' (fatal in production)' if fatal
                            else ' (warning outside production)'), fatal)


def check_actions_webhook():
    """Provider callbacks (Bland/Twilio) verify a shared secret riding in a
    URL built from PUBLIC_BASE_URL; unset means callbacks fail closed and
    executing actions never resolve."""
    missing = [var for var in ('ACTIONS_WEBHOOK_SECRET', 'PUBLIC_BASE_URL')
               if not os.environ.get(var)]
    if not missing:
        return _result('actions_webhook', True, 'set', True)
    return _result(
        'actions_webhook', False,
        'missing: %s — provider callbacks fail closed, so executing actions '
        'never resolve.' % ', '.join(missing), True)


def check_executor_env():
    """One check per registered rail, from the executor's own required_env.
    NOT fatal: a dark rail fails loud at execution; preflight reports it."""
    results = []
    for kind in all_kinds():
        executor = get_executor(kind)
        missing = [var for var in executor.required_env
                   if not os.environ.get(var)]
        if missing:
            results.append(_result(
                'rail:%s' % kind, False,
                'missing: %s — rail is dark; executions fail loud at '
                'confirm.' % ', '.join(missing), False))
        else:
            results.append(_result(
                'rail:%s' % kind, True,
                'all required env present (%s)'
                % ', '.join(executor.required_env), False))
    return results


def check_database():
    """Dialect + a trivial SELECT 1. sqlite in production is wrong (data
    evaporates on redeploy), so it reds the check there."""
    try:
        dialect = db.engine.dialect.name
        db.session.execute(text('SELECT 1'))
    except Exception as exc:  # noqa: BLE001 — report, don't crash preflight
        return _result('database', False,
                       'database check failed: %s' % exc, True)
    if _is_production() and dialect == 'sqlite':
        return _result(
            'database', False,
            'dialect=sqlite — wrong for production (ephemeral filesystem; '
            'expected postgresql).', True)
    return _result('database', True, 'dialect=%s; SELECT 1 ok' % dialect,
                   True)


def check_telegram_admin():
    """Alert path for poller/ops failures (poller-hardening PR)."""
    if os.environ.get('TELEGRAM_ADMIN_CHAT_ID'):
        return _result('telegram_admin', True, 'set', False)
    return _result(
        'telegram_admin', False,
        'TELEGRAM_ADMIN_CHAT_ID is not set — ops alerts have nowhere to '
        'go.', False)


def check_reaper_heartbeat():
    """Newest ActionEvent with actor='reaper'. Informational until the
    external cron is wired — never fatal."""
    event = (ActionEvent.query.filter_by(actor='reaper')
             .order_by(ActionEvent.created_at.desc()).first())
    if event is None:
        return _result('reaper_heartbeat', False, 'never run', False)
    age = _utcnow() - event.created_at
    detail = 'last reaper transition %ds ago' % int(age.total_seconds())
    return _result('reaper_heartbeat', age <= HEARTBEAT_STALE, detail, False)


# Registry: each entry returns one result dict or a list of them.
CHECKS = (
    check_step_up_secret,
    check_fasten_webhook_secret,
    check_internal_mint_secret,
    check_actions_webhook,
    check_executor_env,
    check_database,
    check_telegram_admin,
    check_reaper_heartbeat,
)


def run_all():
    """Run every registered check, flattened into one list."""
    results = []
    for check in CHECKS:
        outcome = check()
        results.extend(outcome if isinstance(outcome, list) else [outcome])
    return results
