/**
 * fhir-control-panel.js — FHIR Control Panel (Dev Days demo).
 *
 * Vanilla fetch + DOM, no framework. Manual refresh only (no auto-poll).
 * Every data call carries X-Tenant-Id. Each section is fault-tolerant:
 * a failure in one section shows an inline error and never blanks the page.
 *
 * Backend (tenant-scoped, GET, no step-up):
 *   GET /r6/fhir/$inventory          -> Parameters {total, lastUpdated, byType.part[]}
 *   GET /r6/fhir/$profile-adherence  -> Parameters {overallAdherence, byType.part[]}
 *   GET /r6/fhir/<Type>?_count=20&_sort=-_lastUpdated -> searchset Bundle
 */

(function () {
    'use strict';

    const DEFAULT_TENANT = 'desktop-demo';
    const EXPLORER_COUNT = 20;

    const tenantInput = document.getElementById('fcp-tenant-input');
    const refreshBtn = document.getElementById('fcp-refresh');
    const lastRefreshLabel = document.getElementById('fcp-last-refresh');
    const explorerTypeLabel = document.getElementById('fcp-explorer-type');

    function queryTenant() {
        const m = new URLSearchParams(window.location.search).get('tenant');
        return (m && m.trim()) || DEFAULT_TENANT;
    }

    let currentTenant = queryTenant();
    let selectedType = null;
    tenantInput.value = currentTenant;

    // --- helpers ---------------------------------------------------------

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

    async function fetchFHIR(path) {
        const resp = await fetch(`/r6/fhir/${path}`, {
            headers: {
                'Accept': 'application/json',
                'X-Tenant-Id': currentTenant,
            },
        });
        if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
        return resp.json();
    }

    function errorBlock(msg) {
        return `<div class="fcp-error"><i class="fas fa-triangle-exclamation"></i> ${escape(msg)}</div>`;
    }

    // Extract a top-level Parameters value by name (valueInteger/Decimal/String/DateTime).
    function paramValue(params, name) {
        const p = (params || []).find((x) => x.name === name);
        if (!p) return undefined;
        if ('valueInteger' in p) return p.valueInteger;
        if ('valueDecimal' in p) return p.valueDecimal;
        if ('valueString' in p) return p.valueString;
        if ('valueDateTime' in p) return p.valueDateTime;
        return p;
    }

    function paramByType(params) {
        const p = (params || []).find((x) => x.name === 'byType');
        return (p && p.part) || [];
    }

    // Read a named child part's value out of a part[] list.
    function partValue(parts, name) {
        const p = (parts || []).find((x) => x.name === name);
        if (!p) return undefined;
        if ('valueInteger' in p) return p.valueInteger;
        if ('valueDecimal' in p) return p.valueDecimal;
        if ('valueString' in p) return p.valueString;
        if ('valueDateTime' in p) return p.valueDateTime;
        return undefined;
    }

    function fmtDateTime(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return escape(iso);
        return d.toLocaleString();
    }

    function adherenceClass(ratio) {
        if (ratio >= 0.9) return 'good';
        if (ratio >= 0.7) return 'warn';
        return 'bad';
    }

    function pct(ratio) {
        return `${Math.round((ratio || 0) * 100)}%`;
    }

    // --- inventory -------------------------------------------------------

    async function loadInventory() {
        setHTML('fcp-inventory-banner', '<div class="cc-loading">Loading…</div>');
        setHTML('fcp-inventory-grid', '');
        try {
            const data = await fetchFHIR('$inventory');
            const params = data.parameter || [];
            const total = paramValue(params, 'total') || 0;
            const lastUpdated = paramValue(params, 'lastUpdated');
            const byType = paramByType(params);

            if (!byType.length) {
                setHTML('fcp-inventory-banner',
                    '<div class="cc-empty">No data for this tenant yet.</div>');
                return;
            }

            // byType.part[] sorted desc server-side; sort again defensively.
            const tiles = byType
                .map((p) => ({ name: p.name, count: p.valueInteger || 0 }))
                .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

            setHTML('fcp-inventory-banner', `
                <div class="fcp-banner-stat"><span class="fcp-banner-num">${total.toLocaleString()}</span> resources across ${tiles.length} type${tiles.length === 1 ? '' : 's'}</div>
                <div class="fcp-banner-updated">Last updated: ${escape(fmtDateTime(lastUpdated))}</div>
            `);

            const grid = tiles.map((t) => `
                <button type="button" class="fcp-tile${selectedType === t.name ? ' selected' : ''}" data-type="${escape(t.name)}">
                    <span class="fcp-tile-count">${t.count.toLocaleString()}</span>
                    <span class="fcp-tile-name">${escape(t.name)}</span>
                </button>
            `).join('');
            setHTML('fcp-inventory-grid', grid);

            document.querySelectorAll('#fcp-inventory-grid .fcp-tile').forEach((btn) => {
                btn.addEventListener('click', () => selectType(btn.dataset.type));
            });
        } catch (err) {
            setHTML('fcp-inventory-banner', errorBlock(`Inventory failed: ${err.message}`));
        }
    }

    // --- profile adherence ----------------------------------------------

    async function loadAdherence() {
        setHTML('fcp-adherence', '<div class="cc-loading">Loading…</div>');
        try {
            const data = await fetchFHIR('$profile-adherence');
            const params = data.parameter || [];
            const overall = paramValue(params, 'overallAdherence');
            const byType = paramByType(params);

            if (!byType.length) {
                setHTML('fcp-adherence', '<div class="cc-empty">No data for this tenant yet.</div>');
                return;
            }

            const overallRatio = overall || 0;
            const gauge = `
                <div class="fcp-gauge ${adherenceClass(overallRatio)}">
                    <div class="fcp-gauge-num">${pct(overallRatio)}</div>
                    <div class="fcp-gauge-lbl">Overall adherence</div>
                </div>`;

            const rows = byType.map((p) => {
                const parts = p.part || [];
                const sampled = partValue(parts, 'sampled') || 0;
                const conformant = partValue(parts, 'conformant') || 0;
                const adherence = partValue(parts, 'adherence') || 0;
                const topIssues = partValue(parts, 'topIssues') || '';
                const cls = adherenceClass(adherence);
                return `
                    <tr>
                        <td><button type="button" class="fcp-type-link" data-type="${escape(p.name)}">${escape(p.name)}</button></td>
                        <td class="fcp-num">${conformant} / ${sampled}</td>
                        <td class="fcp-num">
                            <span class="fcp-adh-pill ${cls}">${pct(adherence)}</span>
                        </td>
                        <td class="fcp-issues">${topIssues ? escape(topIssues) : '<span class="fcp-none">— none —</span>'}</td>
                    </tr>`;
            }).join('');

            setHTML('fcp-adherence', `
                <div class="fcp-adherence-layout">
                    ${gauge}
                    <div class="fcp-adherence-table-wrap">
                        <table class="fcp-table">
                            <thead>
                                <tr><th>Resource type</th><th>Conformant / sampled</th><th>Adherence</th><th>Top issues</th></tr>
                            </thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                </div>
            `);

            document.querySelectorAll('#fcp-adherence .fcp-type-link').forEach((btn) => {
                btn.addEventListener('click', () => selectType(btn.dataset.type));
            });
        } catch (err) {
            setHTML('fcp-adherence', errorBlock(`Profile adherence failed: ${err.message}`));
        }
    }

    // --- resource explorer ----------------------------------------------

    // Derive a generic, redaction-tolerant summary from a FHIR resource JSON.
    function summarizeCode(res) {
        const c = res.code;
        if (c) {
            if (c.text) return c.text;
            if (Array.isArray(c.coding) && c.coding.length) {
                return c.coding[0].display || c.coding[0].code || '';
            }
        }
        // Patient/Practitioner-style fallback: name
        if (Array.isArray(res.name) && res.name.length) {
            const n = res.name[0];
            if (n.text) return n.text;
            const parts = [(n.given || []).join(' '), n.family].filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        if (res.vaccineCode) {
            const v = res.vaccineCode;
            if (v.text) return v.text;
            if (Array.isArray(v.coding) && v.coding.length) return v.coding[0].display || v.coding[0].code || '';
        }
        if (res.medicationCodeableConcept) {
            const m = res.medicationCodeableConcept;
            if (m.text) return m.text;
            if (Array.isArray(m.coding) && m.coding.length) return m.coding[0].display || m.coding[0].code || '';
        }
        return '';
    }

    function summarizeSubject(res) {
        const ref = res.subject || res.patient;
        if (ref && ref.reference) return ref.reference;
        return '';
    }

    function selectType(type) {
        selectedType = type;
        explorerTypeLabel.textContent = type ? `· ${type}` : '';
        // Reflect selection in the inventory tiles.
        document.querySelectorAll('#fcp-inventory-grid .fcp-tile').forEach((t) => {
            t.classList.toggle('selected', t.dataset.type === type);
        });
        loadExplorer(type);
    }

    async function loadExplorer(type) {
        setHTML('fcp-explorer', '<div class="cc-loading">Loading…</div>');
        try {
            const bundle = await fetchFHIR(
                `${encodeURIComponent(type)}?_count=${EXPLORER_COUNT}&_sort=-_lastUpdated`);
            const entries = bundle.entry || [];

            if (!entries.length) {
                setHTML('fcp-explorer',
                    `<div class="cc-empty">No ${escape(type)} resources for this tenant yet.</div>`);
                return;
            }

            const rows = entries.map((e) => {
                const res = e.resource || {};
                const id = res.id || '';
                const code = summarizeCode(res);
                const status = res.status || res.clinicalStatus?.coding?.[0]?.code || '';
                const subject = summarizeSubject(res);
                const lastUpdated = res.meta && res.meta.lastUpdated;
                const detailUrl = `/r6/fhir/mcp-apps/compiled-truth/${encodeURIComponent(type)}/${encodeURIComponent(id)}`;
                return `
                    <tr class="fcp-res-row" data-url="${escape(detailUrl)}">
                        <td class="fcp-mono">${escape(id)}</td>
                        <td>${code ? escape(code) : '<span class="fcp-none">—</span>'}</td>
                        <td>${status ? `<span class="cc-badge">${escape(status)}</span>` : '<span class="fcp-none">—</span>'}</td>
                        <td class="fcp-mono">${subject ? escape(subject) : '<span class="fcp-none">—</span>'}</td>
                        <td class="fcp-mono">${escape(fmtDateTime(lastUpdated))}</td>
                        <td><a class="fcp-open" href="${escape(detailUrl)}" target="_blank" rel="noopener" title="Open compiled-truth detail"><i class="fas fa-arrow-up-right-from-square"></i></a></td>
                    </tr>`;
            }).join('');

            setHTML('fcp-explorer', `
                <div class="fcp-explorer-meta">
                    Showing ${entries.length} most-recent ${escape(type)} (total ${(bundle.total ?? entries.length).toLocaleString()}). Values PHI-redacted.
                </div>
                <table class="fcp-table fcp-res-table">
                    <thead>
                        <tr><th>ID</th><th>Summary</th><th>Status</th><th>Subject</th><th>Last updated</th><th></th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `);

            // Row click opens detail in a new tab (ignore clicks on the explicit link).
            document.querySelectorAll('#fcp-explorer .fcp-res-row').forEach((row) => {
                row.addEventListener('click', (ev) => {
                    if (ev.target.closest('a')) return;
                    window.open(row.dataset.url, '_blank', 'noopener');
                });
            });
        } catch (err) {
            setHTML('fcp-explorer', errorBlock(`Explorer (${type}) failed: ${err.message}`));
        }
    }

    // --- refresh orchestration ------------------------------------------

    function refreshAll() {
        currentTenant = (tenantInput.value || '').trim() || DEFAULT_TENANT;
        tenantInput.value = currentTenant;
        loadInventory();
        loadAdherence();
        if (selectedType) loadExplorer(selectedType);
        lastRefreshLabel.textContent = new Date().toLocaleTimeString();
    }

    refreshBtn.addEventListener('click', refreshAll);
    tenantInput.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            // Switching tenant invalidates the current selection.
            selectedType = null;
            explorerTypeLabel.textContent = '';
            setHTML('fcp-explorer',
                '<div class="cc-empty">Select a resource type above to explore its resources.</div>');
            refreshAll();
        }
    });

    // initial load
    refreshAll();
})();
