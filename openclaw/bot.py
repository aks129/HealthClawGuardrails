"""
openclaw — Telegram bot for HealthClaw Guardrails.

Provides a conversational interface to the local MCP FHIR guardrail stack.

Commands
--------
/start          Welcome message + command list
/health         Stack health check (Flask + MCP reachability)
/conditions     List Conditions for the configured tenant
/labs           Recent lab results (Observation search)
/curatr         Run Curatr clinical evaluation on current Conditions
/curatr fix     Apply the first fix proposal from the last Curatr evaluation
/approve        Confirm a pending step-up write (sets X-Human-Confirmed)
/token          Display the current step-up token (for debugging)

Environment variables
---------------------
TELEGRAM_BOT_TOKEN   Required. BotFather token.
TENANT_ID            Tenant to query. Default: desktop-demo.
MCP_BASE_URL         MCP HTTP bridge base URL. Default: http://localhost:3001.
FHIR_BASE_URL        Flask FHIR base URL. Default: http://localhost:5000/r6/fhir.
STEP_UP_SECRET       HMAC secret for step-up tokens.
"""

import json
import logging
import os

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(name)s — %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger('openclaw')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
TENANT_ID = os.environ.get('TENANT_ID', 'desktop-demo')
MCP_BASE_URL = os.environ.get('MCP_BASE_URL', 'http://localhost:3001').rstrip('/')
FHIR_BASE_URL = os.environ.get('FHIR_BASE_URL', 'http://localhost:5000/r6/fhir').rstrip('/')
STEP_UP_SECRET = os.environ.get('STEP_UP_SECRET', '')

_RPC_URL = f'{MCP_BASE_URL}/mcp/rpc'

# Per-chat ephemeral state (pending writes, last curatr result)
_chat_state: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# MCP HTTP bridge helpers
# ---------------------------------------------------------------------------

def _rpc(tool: str, **params) -> dict:
    """
    Call an MCP tool via the HTTP bridge (POST /mcp/rpc).

    Uses JSON-RPC 2.0 with method=tools/call.
    Returns the result value on success, raises on HTTP error.
    """
    payload = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': tool,
            'arguments': {'tenant_id': TENANT_ID, **params},
        },
    }
    resp = requests.post(_RPC_URL, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message', str(data['error'])))
    return data.get('result', data)


def _fhir_get(path: str) -> dict:
    """Direct FHIR GET with tenant header (bypasses MCP for quick reads)."""
    resp = requests.get(
        f'{FHIR_BASE_URL}/{path.lstrip("/")}',
        headers={'X-Tenant-ID': TENANT_ID},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get_step_up_token() -> str:
    """Fetch a fresh step-up token from the seed/token endpoint."""
    resp = requests.post(
        f'{FHIR_BASE_URL}/internal/step-up-token',
        json={'tenant_id': TENANT_ID},
        headers={'X-Tenant-ID': TENANT_ID},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('step_up_token', '')


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_condition(entry: dict) -> str:
    res = entry.get('resource', {})
    code = (
        res.get('code', {})
           .get('coding', [{}])[0]
           .get('display', res.get('code', {}).get('text', '?'))
    )
    status = res.get('clinicalStatus', {}).get('coding', [{}])[0].get('code', '?')
    return f'• {code} ({status})'


def _fmt_observation(entry: dict) -> str:
    res = entry.get('resource', {})
    code = (
        res.get('code', {})
           .get('coding', [{}])[0]
           .get('display', res.get('code', {}).get('text', '?'))
    )
    qty = res.get('valueQuantity', {})
    value = f"{qty.get('value', '?')} {qty.get('unit', '')}".strip()
    return f'• {code}: {value}'


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        '*HealthClaw Guardrails Bot*\n\n'
        'Commands:\n'
        '/health — stack health check\n'
        '/conditions — list Conditions\n'
        '/labs — recent lab results\n'
        '/curatr — run Curatr evaluation\n'
        '/curatr\\_fix — apply first Curatr fix proposal\n'
        '/approve — confirm pending write\n'
        '/token — show current step-up token\n\n'
        f'Tenant: `{TENANT_ID}`'
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Checking stack health…')
    lines = []

    # Flask health
    try:
        data = _fhir_get('health')
        mode = data.get('mode', '?')
        lines.append(f'Flask: OK (mode={mode})')
    except Exception as exc:
        lines.append(f'Flask: ERROR — {exc}')

    # MCP reachability
    try:
        resp = requests.get(f'{MCP_BASE_URL}/health', timeout=5)
        lines.append(f'MCP: {"OK" if resp.ok else "HTTP " + str(resp.status_code)}')
    except Exception as exc:
        lines.append(f'MCP: ERROR — {exc}')

    await update.message.reply_text('\n'.join(lines))


async def cmd_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Fetching conditions…')
    try:
        result = _rpc('fhir_search', resource_type='Condition', params={})
        bundle = result.get('bundle', result)
        entries = bundle.get('entry', [])
        if not entries:
            await update.message.reply_text('No conditions found.')
            return
        lines = [_fmt_condition(e) for e in entries[:20]]
        await update.message.reply_text('*Conditions*\n' + '\n'.join(lines), parse_mode='Markdown')
    except Exception as exc:
        logger.error('conditions error: %s', exc)
        await update.message.reply_text(f'Error: {exc}')


async def cmd_labs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Fetching lab results…')
    try:
        result = _rpc(
            'fhir_search',
            resource_type='Observation',
            params={'category': 'laboratory', '_count': '10', '_sort': '-_lastUpdated'},
        )
        bundle = result.get('bundle', result)
        entries = bundle.get('entry', [])
        if not entries:
            await update.message.reply_text('No lab results found.')
            return
        lines = [_fmt_observation(e) for e in entries[:20]]
        await update.message.reply_text('*Lab Results*\n' + '\n'.join(lines), parse_mode='Markdown')
    except Exception as exc:
        logger.error('labs error: %s', exc)
        await update.message.reply_text(f'Error: {exc}')


async def cmd_curatr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == 'fix':
        await _curatr_fix(update, context)
        return

    await update.message.reply_text('Running Curatr evaluation…')
    chat_id = update.effective_chat.id
    try:
        result = _rpc('curatr_evaluate')
        _chat_state.setdefault(chat_id, {})['last_curatr'] = result

        score = result.get('overall_score', result.get('score', '?'))
        issues = result.get('issues', [])
        proposals = result.get('fix_proposals', result.get('proposals', []))

        lines = [f'*Curatr Evaluation* (score: {score})']
        if issues:
            lines.append('\n*Issues:*')
            for iss in issues[:5]:
                lines.append(f'• {iss.get("description", iss)}')
        if proposals:
            lines.append(f'\n{len(proposals)} fix proposal(s) available — use /curatr\\_fix')

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as exc:
        logger.error('curatr error: %s', exc)
        await update.message.reply_text(f'Error: {exc}')


async def _curatr_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = _chat_state.get(chat_id, {})
    last = state.get('last_curatr')

    if not last:
        await update.message.reply_text(
            'No Curatr result in memory. Run /curatr first.'
        )
        return

    proposals = last.get('fix_proposals', last.get('proposals', []))
    if not proposals:
        await update.message.reply_text('No fix proposals in last Curatr result.')
        return

    fix = proposals[0]
    await update.message.reply_text(
        f'Applying fix: {fix.get("description", str(fix))}\n\nConfirm with /approve'
    )
    state['pending_fix'] = fix
    state['pending_token'] = None  # will be set on /approve


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = _chat_state.get(chat_id, {})
    fix = state.get('pending_fix')

    if not fix:
        await update.message.reply_text('No pending fix to approve.')
        return

    await update.message.reply_text('Obtaining step-up token and applying fix…')
    try:
        token = _get_step_up_token()
        result = _rpc(
            'curatr_apply_fix',
            fix=fix,
            step_up_token=token,
            human_confirmed=True,
        )
        state.pop('pending_fix', None)
        state.pop('pending_token', None)

        status = result.get('status', result.get('resourceType', 'ok'))
        await update.message.reply_text(f'Fix applied. Status: `{status}`', parse_mode='Markdown')
    except Exception as exc:
        logger.error('approve error: %s', exc)
        await update.message.reply_text(f'Error applying fix: {exc}')


async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not STEP_UP_SECRET:
        await update.message.reply_text('STEP_UP_SECRET not configured.')
        return
    try:
        token = _get_step_up_token()
        await update.message.reply_text(
            f'Step-up token (valid 5 min):\n`{token}`', parse_mode='Markdown'
        )
    except Exception as exc:
        await update.message.reply_text(f'Error: {exc}')


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        'Unknown command. Try /start for the command list.'
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('health', cmd_health))
    app.add_handler(CommandHandler('conditions', cmd_conditions))
    app.add_handler(CommandHandler('labs', cmd_labs))
    app.add_handler(CommandHandler('curatr', cmd_curatr))
    app.add_handler(CommandHandler('approve', cmd_approve))
    app.add_handler(CommandHandler('token', cmd_token))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info('openclaw bot starting (tenant=%s)', TENANT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
