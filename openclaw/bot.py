"""
openclaw — Telegram bot for HealthClaw Guardrails.

Provides a conversational interface to the local MCP FHIR guardrail stack.

Commands
--------
/start          Welcome + bind this chat to TENANT_ID (idempotent)
/connect        Get the Fasten TEFCA verification URL for this tenant
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    # Maps /command → persona id in agents.yaml.
    # sally (pcp-advisor) owns the "what's up with my health?" questions.
    # mary (pharmacy) handles meds. dom does fitness/vitals. joe runs plumbing.
    'start': 'sally',
    'connect': 'sally',       # data-onboarding flow lives with PCP advisor
    'health': 'joe',          # stack health check — service optimizer
    'conditions': 'sally',
    'labs': 'sally',
    'dashboard': 'sally',
    'curatr': 'joe',
    'curatr_fix': 'joe',
    'approve': 'joe',
    'token': 'joe',
    'hbo_connect': 'sally',   # Health Bank One OAuth consent
    'hbo_pull': 'joe',        # HBO MCP pull + ingest
}

# Public base URL where the dashboard is reachable. Override for production
# (e.g., https://healthclaw.io).
DASHBOARD_BASE_URL = os.environ.get('DASHBOARD_BASE_URL', 'https://healthclaw.io').rstrip('/')

# Health Bank One — optional config. If HBO_AUTHORIZATION_ENDPOINT is unset,
# /hbo-connect tells the user to configure it.
HBO_AUTHORIZATION_ENDPOINT = os.environ.get('HBO_AUTHORIZATION_ENDPOINT', '').strip()
HBO_CLIENT_ID = os.environ.get('HBO_CLIENT_ID', '').strip()
HBO_REDIRECT_URI = os.environ.get(
    'HBO_REDIRECT_URI', f'{DASHBOARD_BASE_URL}/hbo/callback').strip()
HBO_SCOPES = os.environ.get('HBO_SCOPES', 'openid offline_access').strip()
HBO_MCP_URL = os.environ.get(
    'HBO_MCP_URL', 'https://mcp.app.healthbankone.com/mcp').strip()


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
    """Fetch a fresh step-up token from the mint endpoint.

    The endpoint returns the token under `token` (older callers expected
    `step_up_token`). When INTERNAL_TOKEN_MINT_SECRET is set, minting for a
    non-public tenant (this bot's TENANT_ID) requires X-Internal-Secret.
    """
    headers = {'X-Tenant-ID': TENANT_ID}
    mint_secret = os.environ.get('INTERNAL_TOKEN_MINT_SECRET')
    if mint_secret:
        headers['X-Internal-Secret'] = mint_secret
    resp = requests.post(
        f'{FHIR_BASE_URL}/internal/step-up-token',
        json={'tenant_id': TENANT_ID},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get('token') or data.get('step_up_token', '')


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

def _bind_chat_to_tenant(chat_id: int, username: str | None) -> tuple[bool, str]:
    """
    Tell Flask to bind this Telegram chat to TENANT_ID so the Fasten ingest
    webhook can push back. Idempotent; returns (ok, detail) — detail is
    safe to surface in chat ("bound", "already bound", or an error class).
    """
    if not STEP_UP_SECRET:
        return False, 'STEP_UP_SECRET not configured'
    try:
        step_up = _get_step_up_token()
        resp = requests.post(
            f'{FHIR_BASE_URL}/internal/bind-telegram',
            json={
                'tenant_id': TENANT_ID,
                'chat_id': chat_id,
                'username': username,
                'step_up_token': step_up,
            },
            timeout=5,
        )
        if resp.status_code == 201:
            return True, 'bound'
        return False, f'http {resp.status_code}'
    except requests.RequestException as exc:
        return False, f'network: {exc.__class__.__name__}'


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent_id = await _log_incoming(update, 'start')

    chat_id = update.effective_chat.id if update.effective_chat else None
    username = (update.effective_user.username if update.effective_user else None) or None
    bind_ok, bind_detail = (False, 'no chat_id')
    if chat_id is not None:
        bind_ok, bind_detail = _bind_chat_to_tenant(chat_id, username)
        logger.info('bind chat=%s tenant=%s ok=%s detail=%s',
                    chat_id, TENANT_ID, bind_ok, bind_detail)

    bind_line = (
        f'✅ Chat bound to tenant `{TENANT_ID}` — you will get a ping when records arrive.'
        if bind_ok else
        f'⚠️ Could not bind chat (`{bind_detail}`). You can still use commands; notifications are off.'
    )

    # One-time risk acknowledgment for the chat-app channel. Telegram is a
    # consumer channel, not BAA-covered transport; this is patient-directed
    # access to one's own records. See templates/privacy.html "Messaging
    # Platforms" for the full posture.
    # TODO(nophi): wire a real /nophi toggle that flips this chat into
    # summary-only mode (persist per chat_id, gate read formatters on it).
    # Disclosure line is shipped now; the toggle is not yet implemented.
    risk_line = (
        '⚠️ Heads up: chat apps aren’t encrypted medical channels. '
        'You’re accessing your own records here; by continuing you accept '
        'that for your own data. Reply /nophi to keep responses summary-only.'
    )

    text = (
        '*HealthClaw Guardrails Bot*\n\n'
        f'{risk_line}\n\n'
        f'{bind_line}\n\n'
        'Commands:\n'
        '/connect — pull your records (Fasten + TEFCA)\n'
        '/hbo\\_connect — authorize Health Bank One (OAuth)\n'
        '/hbo\\_pull — pull + ingest HBO verified records\n'
        '/dashboard — open the command center (signed 24h link)\n'
        '/summary — high-level review of your record\n'
        '/conditions — list Conditions\n'
        '/labs — recent lab results\n'
        '/curatr — run Curatr data-quality evaluation\n'
        '/curatr\\_fix — apply first Curatr fix proposal\n'
        '/approve — confirm pending write\n'
        '/health — stack health check\n'
        '/token — show current step-up token\n\n'
        f'Tenant: `{TENANT_ID}`'
    )
    await _reply(update, text, agent_id, parse_mode='Markdown')


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a menu of available health data connection options."""
    agent_id = await _log_incoming(update, 'connect')

    keyboard = [
        [InlineKeyboardButton(
            'Fasten TEFCA — hospitals, labs, EHRs (nationwide)',
            callback_data='connect:fasten',
        )],
        [InlineKeyboardButton(
            'Health Bank One — verified records + insurance',
            callback_data='connect:hbo',
        )],
        [InlineKeyboardButton(
            'Flexpa — 200+ payers/insurers (CMS-9115)',
            callback_data='connect:flexpa',
        )],
        [InlineKeyboardButton(
            'Epic / patient portals (Health Skillz)',
            callback_data='connect:epic',
        )],
        [InlineKeyboardButton(
            'MEDENT — small-practice EHR (SMART on FHIR)',
            callback_data='connect:medent',
        )],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        '*Connect your health records*\n\n'
        'Choose a source to connect. You can connect multiple — '
        'all records flow into the same tenant and are deduplicated.'
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)


async def _connect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses from /connect menu."""
    query = update.callback_query
    await query.answer()
    choice = query.data  # e.g. 'connect:fasten'

    connect_url = f'{DASHBOARD_BASE_URL}/connect/{TENANT_ID}'

    if choice == 'connect:fasten':
        text = (
            '*Fasten TEFCA*\n\n'
            'Verify once with CLEAR or ID.me — records stream from every QHIN '
            '(hospitals, labs, EHRs). Usually 5–45 min.\n\n'
            f'{connect_url}'
        )
    elif choice == 'connect:hbo':
        text = (
            '*Health Bank One*\n\n'
            'Identity-verified records + insurance context.\n\n'
            'Run /hbo\\_connect to start the OAuth flow.'
        )
    elif choice == 'connect:flexpa':
        text = (
            '*Flexpa — 200+ payers*\n\n'
            'Connects claims, EOBs, coverage from major US insurers (CMS-9115).\n\n'
            'Run /flexpa\\_connect to get the authorization link.'
        )
    elif choice == 'connect:epic':
        text = (
            '*Epic / patient portals*\n\n'
            'Connects MyChart and most major patient portals via Health Skillz.\n\n'
            'Run /epic\\_connect to get the authorization link.'
        )
    elif choice == 'connect:medent':
        text = (
            '*MEDENT*\n\n'
            'Small-practice EHR with SMART on FHIR (v23.5+).\n\n'
            'Run /medent\\_connect to authorize via your patient portal.'
        )
    else:
        text = 'Unknown option.'

    await query.edit_message_text(text, parse_mode='Markdown')


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


async def cmd_hbo_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the HBO OAuth authorization URL so the user can grant access."""
    agent_id = await _log_incoming(update, 'hbo_connect')

    if not HBO_AUTHORIZATION_ENDPOINT:
        await _reply(
            update,
            '⚠️ *Health Bank One not configured*\n\n'
            'Ask your admin to set:\n'
            '  `HBO_AUTHORIZATION_ENDPOINT`\n'
            '  `HBO_CLIENT_ID`\n'
            '  `HBO_SCOPES`\n\n'
            'Or run `python scripts/healthbankone_oauth.py authorize` locally.',
            agent_id,
            parse_mode='Markdown',
        )
        return

    import urllib.parse as _up
    import secrets as _sec
    import hashlib as _hl
    import base64 as _b64

    # Build PKCE pair inline (avoids a subprocess just to get the URL)
    verifier_bytes = _sec.token_bytes(48)
    verifier = _b64.urlsafe_b64encode(verifier_bytes).rstrip(b'=').decode('ascii')
    digest = _hl.sha256(verifier.encode('ascii')).digest()
    challenge = _b64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    state = _sec.token_urlsafe(24)

    # Stash verifier in chat state so /hbo-callback (future) can finish exchange
    chat_id = update.effective_chat.id
    _chat_state.setdefault(chat_id, {})['hbo_pkce'] = {
        'verifier': verifier, 'state': state}

    params = {
        'response_type': 'code',
        'client_id': HBO_CLIENT_ID,
        'redirect_uri': HBO_REDIRECT_URI,
        'scope': HBO_SCOPES,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    auth_url = f'{HBO_AUTHORIZATION_ENDPOINT}?{_up.urlencode(params)}'

    text = (
        '🔑 *Health Bank One authorization*\n\n'
        'Click the link below, log in with Health Bank One, '
        'and grant HealthClaw access to your verified records.\n\n'
        f'{auth_url}\n\n'
        '_After you authorize, run /hbo\\_pull to fetch your records._'
    )
    await _reply(update, text, agent_id, parse_mode='Markdown')


async def cmd_hbo_pull(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull + redact + ingest Health Bank One records into this tenant."""
    agent_id = await _log_incoming(update, 'hbo_pull')

    await _reply(
        update,
        '⏳ Starting HBO pull for tenant `' + TENANT_ID + '`…\n'
        '_This runs in the background. I\'ll message you when records land._',
        agent_id,
        parse_mode='Markdown',
    )

    import subprocess
    import sys as _sys
    import threading as _thr

    def _run_pull():
        try:
            result = subprocess.run(
                [_sys.executable,
                 'scripts/export_healthbankone_mcp.py',
                 '--tenant-id', TENANT_ID,
                 '--discover',
                 '--pretty'],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                # Best-effort: parse the last line of stdout for summary
                last_line = (result.stdout.strip().splitlines() or ['done'])[-1]
                requests.post(
                    f'{FHIR_BASE_URL.replace("/r6/fhir", "")}/r6/telegram_push/send',
                    json={'tenant_id': TENANT_ID,
                          'message': f'✅ *HBO pull complete*\n{last_line}'},
                    timeout=5,
                )
            else:
                err = (result.stderr.strip().splitlines() or ['unknown error'])[-1]
                requests.post(
                    f'{FHIR_BASE_URL.replace("/r6/fhir", "")}/r6/telegram_push/send',
                    json={'tenant_id': TENANT_ID,
                          'message': f'❌ HBO pull failed: {err}'},
                    timeout=5,
                )
        except Exception as exc:
            logger.error('hbo_pull background error: %s', exc)

    _thr.Thread(target=_run_pull, daemon=True).start()


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
    app.add_handler(CommandHandler('connect', cmd_connect))
    app.add_handler(CallbackQueryHandler(_connect_callback, pattern='^connect:'))
    app.add_handler(CommandHandler('health', cmd_health))
    app.add_handler(CommandHandler('conditions', cmd_conditions))
    app.add_handler(CommandHandler('labs', cmd_labs))
    app.add_handler(CommandHandler('curatr', cmd_curatr))
    app.add_handler(CommandHandler('approve', cmd_approve))
    app.add_handler(CommandHandler('token', cmd_token))
    app.add_handler(CommandHandler('dashboard', cmd_dashboard))
    app.add_handler(CommandHandler('hbo_connect', cmd_hbo_connect))
    app.add_handler(CommandHandler('hbo_pull', cmd_hbo_pull))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info('openclaw bot starting (tenant=%s)', TENANT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
