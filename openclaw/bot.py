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

# Command Center persistence — if Flask is reachable, every chat turn is
# logged to /command-center/api/conversations so the dashboard can show
# recent Telegram activity per agent.
CC_API_BASE = os.environ.get(
    'COMMAND_CENTER_API',
    FHIR_BASE_URL.replace('/r6/fhir', '') + '/command-center/api',
).rstrip('/')

_RPC_URL = f'{MCP_BASE_URL}/mcp/rpc'

# Per-chat ephemeral state (pending writes, last curatr result)
_chat_state: dict[int, dict] = {}

# Telegram command → command-center agent id. Determines which agent
# persona each bot interaction is attributed to in the dashboard.
COMMAND_TO_AGENT = {
    'start': 'health-advisor',
    'health': 'health-advisor',
    'conditions': 'health-advisor',
    'labs': 'health-advisor',
    'dashboard': 'health-advisor',
    'curatr': 'record-curator',
    'curatr_fix': 'record-curator',
    'approve': 'record-curator',
    'token': 'record-curator',
}

# Public base URL where the dashboard is reachable. Override for production
# (e.g., https://healthclaw.io).
DASHBOARD_BASE_URL = os.environ.get('DASHBOARD_BASE_URL', 'https://healthclaw.io').rstrip('/')


def _persist_turn(update: Update, agent_id: str, role: str, text: str,
                  metadata: dict | None = None) -> None:
    """
    POST a conversation turn to the command center API. Silent-on-failure —
    the bot must keep working even if the dashboard API is down.
    """
    try:
        msg = update.effective_message if update else None
        user = update.effective_user if update else None
        chat_id = str(msg.chat_id) if msg else None

        payload = {
            'tenant_id': TENANT_ID,
            'agent_id': agent_id,
            'channel': 'telegram',
            'session_id': chat_id,
            'user_id': str(user.id) if user else None,
            'role': role,
            'text': text,
        }
        if metadata:
            payload['metadata'] = metadata
        requests.post(
            f'{CC_API_BASE}/conversations',
            json=payload,
            timeout=2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug('Command center persistence failed: %s', exc)


async def _log_incoming(update: Update, command: str) -> str:
    """Persist the inbound user command and return the chosen agent_id."""
    agent_id = COMMAND_TO_AGENT.get(command, 'health-advisor')
    if update and update.effective_message:
        _persist_turn(
            update,
            agent_id,
            'user',
            f'/{command} {update.effective_message.text or ""}'.strip(),
        )
    return agent_id


async def _reply(update: Update, text: str, agent_id: str,
                 parse_mode: str | None = None) -> None:
    """Send a Telegram reply and log the assistant turn to the dashboard."""
    if update and update.effective_message:
        if parse_mode:
            await update.effective_message.reply_text(text, parse_mode=parse_mode)
        else:
            await update.effective_message.reply_text(text)
    _persist_turn(update, agent_id, 'assistant', text[:1000])


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
    agent_id = await _log_incoming(update, 'start')
    text = (
        '*HealthClaw Guardrails Bot*\n\n'
        'Commands:\n'
        '/dashboard — open the command center (signed 24h link)\n'
        '/health — stack health check\n'
        '/conditions — list Conditions\n'
        '/labs — recent lab results\n'
        '/curatr — run Curatr evaluation\n'
        '/curatr\\_fix — apply first Curatr fix proposal\n'
        '/approve — confirm pending write\n'
        '/token — show current step-up token\n\n'
        f'Tenant: `{TENANT_ID}`'
    )
    await _reply(update, text, agent_id, parse_mode='Markdown')


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'health')
    await _reply(update, 'Checking stack health…', agent_id)
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

    await _reply(update, '\n'.join(lines), agent_id)


async def cmd_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'conditions')
    await _reply(update, 'Fetching conditions…', agent_id)
    try:
        result = _rpc('fhir_search', resource_type='Condition', params={})
        bundle = result.get('bundle', result)
        entries = bundle.get('entry', [])
        if not entries:
            await _reply(update, 'No conditions found.', agent_id)
            return
        lines = [_fmt_condition(e) for e in entries[:20]]
        await _reply(update, '*Conditions*\n' + '\n'.join(lines), agent_id, parse_mode='Markdown')
    except Exception as exc:
        logger.error('conditions error: %s', exc)
        await _reply(update, f'Error: {exc}', agent_id)


async def cmd_labs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'labs')
    await _reply(update, 'Fetching lab results…', agent_id)
    try:
        result = _rpc(
            'fhir_search',
            resource_type='Observation',
            params={'category': 'laboratory', '_count': '10', '_sort': '-_lastUpdated'},
        )
        bundle = result.get('bundle', result)
        entries = bundle.get('entry', [])
        if not entries:
            await _reply(update, 'No lab results found.', agent_id)
            return
        lines = [_fmt_observation(e) for e in entries[:20]]
        await _reply(update, '*Lab Results*\n' + '\n'.join(lines), agent_id, parse_mode='Markdown')
    except Exception as exc:
        logger.error('labs error: %s', exc)
        await _reply(update, f'Error: {exc}', agent_id)


async def cmd_curatr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == 'fix':
        await _curatr_fix(update, context)
        return

    agent_id = await _log_incoming(update, 'curatr')
    await _reply(update, 'Running Curatr evaluation…', agent_id)
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

        await _reply(update, '\n'.join(lines), agent_id, parse_mode='Markdown')
    except Exception as exc:
        logger.error('curatr error: %s', exc)
        await _reply(update, f'Error: {exc}', agent_id)


async def _curatr_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'curatr_fix')
    chat_id = update.effective_chat.id
    state = _chat_state.get(chat_id, {})
    last = state.get('last_curatr')

    if not last:
        await _reply(update, 'No Curatr result in memory. Run /curatr first.', agent_id)
        return

    proposals = last.get('fix_proposals', last.get('proposals', []))
    if not proposals:
        await _reply(update, 'No fix proposals in last Curatr result.', agent_id)
        return

    fix = proposals[0]
    description = fix.get('description', str(fix))
    await _reply(
        update,
        f'Applying fix: {description}\n\nConfirm with /approve',
        agent_id,
    )
    state['pending_fix'] = fix
    state['pending_token'] = None  # will be set on /approve

    # Create a pending task in the command center so the dashboard surfaces it
    try:
        requests.post(
            f'{CC_API_BASE}/tasks',
            json={
                'tenant_id': TENANT_ID,
                'agent_id': agent_id,
                'title': f'Approve curatr fix: {description[:120]}',
                'description': json.dumps(fix)[:1000],
                'priority': 'high',
                'source': 'telegram',
                'resource_ref': fix.get('resource_ref'),
            },
            timeout=2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug('Could not create task: %s', exc)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'approve')
    chat_id = update.effective_chat.id
    state = _chat_state.get(chat_id, {})
    fix = state.get('pending_fix')

    if not fix:
        await _reply(update, 'No pending fix to approve.', agent_id)
        return

    await _reply(update, 'Obtaining step-up token and applying fix…', agent_id)
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
        await _reply(update, f'Fix applied. Status: `{status}`', agent_id, parse_mode='Markdown')
    except Exception as exc:
        logger.error('approve error: %s', exc)
        await _reply(update, f'Error applying fix: {exc}', agent_id)


async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'token')
    if not STEP_UP_SECRET:
        await _reply(update, 'STEP_UP_SECRET not configured.', agent_id)
        return
    try:
        token = _get_step_up_token()
        await _reply(
            update,
            f'Step-up token (valid 5 min):\n`{token}`',
            agent_id,
            parse_mode='Markdown',
        )
    except Exception as exc:
        await _reply(update, f'Error: {exc}', agent_id)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a signed, time-limited dashboard URL to the user."""
    agent_id = await _log_incoming(update, 'dashboard')

    # Need a step-up token so the API trusts the mint request
    if not STEP_UP_SECRET:
        await _reply(
            update,
            'STEP_UP_SECRET not configured — cannot mint dashboard link.',
            agent_id,
        )
        return

    try:
        step_up = _get_step_up_token()
        resp = requests.post(
            f'{CC_API_BASE}/generate-link',
            json={
                'tenant_id': TENANT_ID,
                'agent_id': agent_id,
                'base_url': DASHBOARD_BASE_URL,
            },
            headers={'X-Step-Up-Token': step_up},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        url = data.get('url')
        hours = data.get('expires_in_hours', 24)

        text = (
            '🔐 *Your dashboard link*\n\n'
            f'{url}\n\n'
            f'Valid for {hours} hours · Tenant: `{TENANT_ID}`\n'
            '_Do not share — anyone with this link can view your command center._'
        )
        await _reply(update, text, agent_id, parse_mode='Markdown')
    except Exception as exc:
        logger.error('dashboard link error: %s', exc)
        await _reply(update, f'Error generating link: {exc}', agent_id)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = 'health-advisor'
    if update and update.effective_message:
        _persist_turn(update, agent_id, 'user', update.effective_message.text or '')
    await _reply(
        update,
        'Unknown command. Try /start for the command list.',
        agent_id,
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
    app.add_handler(CommandHandler('dashboard', cmd_dashboard))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info('openclaw bot starting (tenant=%s)', TENANT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
