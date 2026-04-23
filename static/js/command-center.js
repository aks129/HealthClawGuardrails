/**
 * command-center.js — dashboard polling + rendering.
 *
 * No framework; just fetch + DOM. Polls every POLL_MS.
 */

(function () {
    'use strict';

    const POLL_MS = 5000;
    const hero = document.getElementById('cc-hero');
    const tenantInput = document.getElementById('cc-tenant-input');
    const tenantApply = document.getElementById('cc-tenant-apply');
    const refreshNowBtn = document.getElementById('cc-refresh-now');
    const pollIntervalLabel = document.getElementById('cc-poll-interval');
    const lastRefreshLabel = document.getElementById('cc-last-refresh');

    pollIntervalLabel.textContent = Math.round(POLL_MS / 1000);

    let currentTenant = hero.dataset.tenant || 'desktop-demo';
    let pollTimer = null;

    // --- helpers ---------------------------------------------------------

    function api(path) {
        return `/command-center/api/${path}?tenant=${encodeURIComponent(currentTenant)}`;
    }

    async function fetchJSON(url) {
        const resp = await fetch(url, { headers: { 'Accept': 'application/json' } });
        if (!resp.ok) throw new Error(`${resp.status} ${url}`);
        return resp.json();
    }

    function relativeTime(iso) {
        if (!iso) return '—';
        const then = new Date(iso);
        const diff = Math.round((Date.now() - then.getTime()) / 1000);
        if (diff < 5) return 'just now';
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
        return `${Math.round(diff / 86400)}d ago`;
    }

    function escape(s) {
        if (s === null || s === undefined) return '';
        return String(s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function setHTML(id, html) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
    }

    function setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    // --- renderers -------------------------------------------------------

    function renderOverview(data) {
        setText('cc-hero-summary', data.last_activity
            ? `${relativeTime(data.last_activity)} · ${data.activity_24h} action${data.activity_24h === 1 ? '' : 's'} in last 24h`
            : 'Waiting for activity…');
        setText('cc-stat-records', data.record_count?.toLocaleString() ?? '—');
        setText('cc-stat-flags', data.flag_count ?? '—');
        setText('cc-stat-tasks', data.pending_task_count ?? '—');
        setText('cc-stat-activity', data.activity_24h ?? '—');
    }

    function renderReadiness(data) {
        if (!data.stages || !data.stages.length) {
            setHTML('cc-pipeline', '<div class="cc-empty">No readiness data</div>');
            return;
        }
        setText('cc-hero-summary', data.summary || '');

        const icons = {
            'stack-live': 'fa-server',
            'data-connected': 'fa-plug',
            'records-ingested': 'fa-database',
            'quality-curated': 'fa-check-double',
            'insights-running': 'fa-chart-line',
        };
        const html = data.stages.map((s) => `
            <div class="cc-stage state-${escape(s.state)}">
                <div class="cc-stage-state">${escape(s.state)}</div>
                <div class="cc-stage-label"><i class="fas ${icons[s.id] || 'fa-circle'}"></i> ${escape(s.label)}</div>
                <div class="cc-stage-detail">${escape(s.detail)}</div>
            </div>
        `).join('');
        setHTML('cc-pipeline', html);
    }

    function renderSystem(data) {
        const rows = [
            { name: 'Flask API', up: data.flask?.up, detail: `Mode: ${data.flask?.mode ?? 'unknown'}` },
            { name: 'MCP Server', up: data.mcp_server?.up, detail: data.mcp_server?.url || data.mcp_server?.error || '' },
            {
                name: 'OpenClaw Gateway',
                up: data.openclaw_gateway?.reachable,
                unknown: !data.openclaw_gateway?.configured && !data.openclaw_gateway?.reachable,
                detail: data.openclaw_gateway?.reachable
                    ? `${data.openclaw_gateway.url}${data.openclaw_gateway.version ? ' · v' + data.openclaw_gateway.version : ''}`
                    : (data.openclaw_gateway?.configured
                        ? (data.openclaw_gateway.error || 'unreachable')
                        : 'Set OPENCLAW_GATEWAY_URL to connect'),
            },
            {
                name: 'Redis',
                up: data.redis?.up,
                unknown: !data.redis?.configured,
                detail: data.redis?.configured ? (data.redis?.url || data.redis?.error || '') : 'Optional',
            },
        ];
        const html = rows.map((r) => {
            const cls = r.up ? 'up' : (r.unknown ? 'unknown' : 'down');
            return `
                <div class="cc-system-card">
                    <div class="cc-status-dot ${cls}"></div>
                    <div class="cc-system-card-body">
                        <div class="cc-system-card-name">${escape(r.name)}</div>
                        <div class="cc-system-card-detail" title="${escape(r.detail)}">${escape(r.detail)}</div>
                    </div>
                </div>
            `;
        }).join('');
        setHTML('cc-system', html);
    }

    function renderAgents(list) {
        if (!list || !list.length) {
            setHTML('cc-agents', '<div class="cc-empty">No agents registered</div>');
            return;
        }
        const html = list.map((a) => {
            const lastMsgHtml = a.last_conversation
                ? `<div class="cc-agent-last-msg">“${escape((a.last_conversation.text || '').slice(0, 140))}” · ${relativeTime(a.last_conversation.created_at)}</div>`
                : '';
            const roleHtml = a.role
                ? `<div class="cc-agent-role">${escape(a.role)}</div>`
                : '';
            const tgHtml = a.telegram
                ? `<a class="cc-agent-telegram" href="https://t.me/${escape(a.telegram.replace(/^@/, ''))}" target="_blank" rel="noopener"><i class="fab fa-telegram"></i> ${escape(a.telegram)}</a>`
                : '<span class="cc-agent-telegram cc-agent-telegram-off">no Telegram bot yet</span>';
            return `
                <div class="cc-agent-card state-${escape(a.state)}" style="--cc-agent-color:${escape(a.color)}">
                    <div class="cc-agent-head">
                        <span class="cc-agent-emoji">${escape(a.emoji)}</span>
                        <div>
                            <div class="cc-agent-name">${escape(a.name)}</div>
                            ${roleHtml}
                        </div>
                    </div>
                    <div class="cc-agent-desc">${escape(a.description)}</div>
                    <div class="cc-agent-meta">
                        <span><strong>${a.recent_activity_count}</strong> actions/7d</span>
                        <span><strong>${a.conversation_count}</strong> chats</span>
                        <span><strong>${a.pending_tasks}</strong> tasks</span>
                    </div>
                    ${tgHtml}
                    ${lastMsgHtml}
                </div>
            `;
        }).join('');
        setHTML('cc-agents', html);
    }

    function renderActions(list) {
        if (!list || !list.length) {
            setHTML('cc-actions', '<div class="cc-empty">No recent actions</div>');
            return;
        }
        const icons = { read: 'fa-eye', create: 'fa-plus', update: 'fa-pen', delete: 'fa-trash', validate: 'fa-check' };
        const html = list.map((a) => `
            <div class="cc-row">
                <i class="cc-row-icon fas ${icons[a.event_type] || 'fa-bolt'}"></i>
                <div class="cc-row-body">
                    <div class="cc-row-title">
                        ${escape(a.event_type)}
                        ${a.resource_type ? `<span class="cc-badge">${escape(a.resource_type)}</span>` : ''}
                        ${a.outcome === 'failure' ? '<span class="cc-badge failure">failed</span>' : ''}
                        ${a.agent_emoji ? `<span title="${escape(a.agent_name)}">${escape(a.agent_emoji)}</span>` : ''}
                    </div>
                    <div class="cc-row-sub">${relativeTime(a.recorded)} · ${escape((a.detail || '').slice(0, 120))}</div>
                </div>
            </div>
        `).join('');
        setHTML('cc-actions', html);
    }

    function renderConversations(list) {
        if (!list || !list.length) {
            setHTML('cc-conversations', '<div class="cc-empty">No conversations yet — connect Telegram or chat via MCP</div>');
            return;
        }
        const icons = { user: 'fa-user', assistant: 'fa-robot', system: 'fa-gear' };
        const html = list.map((m) => `
            <div class="cc-row role-${escape(m.role)}">
                <i class="cc-row-icon fas ${icons[m.role] || 'fa-comment'}"></i>
                <div class="cc-row-body">
                    <div class="cc-row-title">
                        ${m.agent_emoji ? escape(m.agent_emoji) + ' ' : ''}<span class="cc-badge">${escape(m.channel)}</span>
                        ${m.agent_name ? `<span style="color:var(--cc-text-dim)">${escape(m.agent_name)}</span>` : ''}
                    </div>
                    <div class="cc-row-sub" style="margin-top:0.3rem;color:var(--cc-text)">${escape(m.text)}</div>
                    <div class="cc-row-sub">${relativeTime(m.created_at)}${m.truncated ? ' · truncated' : ''}</div>
                </div>
            </div>
        `).join('');
        setHTML('cc-conversations', html);
    }

    function renderTasks(list) {
        if (!list || !list.length) {
            setHTML('cc-tasks', '<div class="cc-empty">No pending tasks</div>');
            return;
        }
        const html = list.map((t) => `
            <div class="cc-row">
                <i class="cc-row-icon fas fa-square-check"></i>
                <div class="cc-row-body">
                    <div class="cc-row-title">
                        ${t.agent_emoji ? escape(t.agent_emoji) + ' ' : ''}${escape(t.title)}
                        <span class="cc-badge priority-${escape(t.priority)}">${escape(t.priority)}</span>
                        <span class="cc-badge">${escape(t.status)}</span>
                    </div>
                    <div class="cc-row-sub">${escape(t.agent_name || t.agent_id)} · ${relativeTime(t.created_at)}${t.resource_ref ? ' · ' + escape(t.resource_ref) : ''}</div>
                    ${t.description ? `<div class="cc-row-sub" style="color:var(--cc-text)">${escape(t.description.slice(0, 200))}</div>` : ''}
                </div>
            </div>
        `).join('');
        setHTML('cc-tasks', html);
    }

    function renderInsights(list) {
        if (!list || !list.length) {
            setHTML('cc-insights', '<div class="cc-empty">No flagged insights — run curatr_evaluate to surface issues</div>');
            return;
        }
        const html = list.map((i) => `
            <div class="cc-row">
                <i class="cc-row-icon fas fa-triangle-exclamation"></i>
                <div class="cc-row-body">
                    <div class="cc-row-title">
                        ${escape(i.title)}
                        <span class="cc-badge severity-${escape(i.severity)}">${escape(i.severity)}</span>
                    </div>
                    <div class="cc-row-sub">${escape(i.resource_ref || '')}</div>
                    ${i.description ? `<div class="cc-row-sub" style="color:var(--cc-text)">${escape(i.description)}</div>` : ''}
                </div>
            </div>
        `).join('');
        setHTML('cc-insights', html);
    }

    function renderSources(list) {
        if (!list || !list.length) {
            setHTML('cc-sources', '<div class="cc-empty">No sources configured</div>');
            return;
        }
        const html = list.map((s) => `
            <div class="cc-source-card ${s.connected ? 'connected' : ''}">
                <div class="cc-source-head">
                    <div class="cc-status-dot ${s.connected ? 'up' : 'unknown'}"></div>
                    <span class="cc-source-name">${escape(s.name)}</span>
                </div>
                <div class="cc-source-desc">${escape(s.description)}</div>
                <div class="cc-source-detail">${escape(s.detail)}</div>
                ${s.last_activity ? `<div class="cc-row-sub" style="margin-top:0.4rem">Last: ${relativeTime(s.last_activity)}</div>` : ''}
            </div>
        `).join('');
        setHTML('cc-sources', html);
    }

    function renderSessions(data) {
        const gw = (data && data.gateway) || {};
        const sessions = (data && data.sessions) || [];
        if (!gw.configured) {
            setHTML('cc-sessions',
                `<div class="cc-empty">OpenClaw Gateway not configured — set <code>OPENCLAW_GATEWAY_URL</code> on the Flask service to pull live sessions from your Mac mini</div>`);
            return;
        }
        if (!gw.reachable) {
            setHTML('cc-sessions',
                `<div class="cc-empty">Gateway unreachable at <code>${escape(gw.url || '?')}</code> — check tunnel / VPN / Tailscale</div>`);
            return;
        }
        if (!sessions.length) {
            setHTML('cc-sessions', '<div class="cc-empty">Gateway up but no active sessions</div>');
            return;
        }
        const html = sessions.map((s) => `
            <div class="cc-row">
                <i class="cc-row-icon fas fa-circle-nodes"></i>
                <div class="cc-row-body">
                    <div class="cc-row-title">
                        ${s.agent ? escape(s.agent) + ' · ' : ''}<span class="cc-badge">${escape(s.channel || 'unknown')}</span>
                        ${s.peer ? `<span style="color:var(--cc-text-dim)">${escape(s.peer)}</span>` : ''}
                    </div>
                    <div class="cc-row-sub">
                        ${s.started ? 'started ' + relativeTime(s.started) : ''}
                        ${s.last_activity ? ' · last ' + relativeTime(s.last_activity) : ''}
                        ${s.message_count != null ? ' · ' + s.message_count + ' msgs' : ''}
                    </div>
                </div>
            </div>
        `).join('');
        setHTML('cc-sessions', html);
    }

    function renderSkills(list) {
        if (!list || !list.length) {
            setHTML('cc-skills', '<div class="cc-empty">No skills found</div>');
            return;
        }
        const html = list.map((s) => `
            <div class="cc-skill-card state-${escape(s.state)}">
                <div class="cc-skill-name">${escape(s.name)}</div>
                <div class="cc-skill-desc">${escape(s.description || 'No description')}</div>
                <div class="cc-skill-meta">${s.recent_activity_count} action${s.recent_activity_count === 1 ? '' : 's'} this week${s.last_activity ? ' · ' + relativeTime(s.last_activity) : ''}</div>
            </div>
        `).join('');
        setHTML('cc-skills', html);
    }

    // --- polling --------------------------------------------------------

    async function refreshAll() {
        const endpoints = [
            ['overview', renderOverview],
            ['readiness', renderReadiness],
            ['system', renderSystem],
            ['agents', renderAgents],
            ['openclaw/sessions', renderSessions],
            ['actions', renderActions],
            ['conversations', renderConversations],
            ['tasks', renderTasks],
            ['insights', renderInsights],
            ['sources', renderSources],
            ['skills', renderSkills],
        ];
        const results = await Promise.allSettled(endpoints.map(([p]) => fetchJSON(api(p))));
        results.forEach((r, idx) => {
            const [path, render] = endpoints[idx];
            if (r.status === 'fulfilled') {
                try { render(r.value); }
                catch (e) { console.error('render failed', path, e); }
            } else {
                console.error('fetch failed', path, r.reason);
            }
        });
        lastRefreshLabel.textContent = new Date().toLocaleTimeString();
    }

    function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        refreshAll();
        pollTimer = setInterval(refreshAll, POLL_MS);
    }

    tenantApply?.addEventListener('click', () => {
        const v = (tenantInput.value || '').trim();
        if (v) {
            currentTenant = v;
            hero.dataset.tenant = v;
            refreshAll();
        }
    });
    tenantInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') tenantApply.click();
    });
    refreshNowBtn?.addEventListener('click', refreshAll);

    startPolling();
})();
