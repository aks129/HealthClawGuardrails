/**
 * R6 FHIR Agent Dashboard — Interactive Showcase
 *
 * Demonstrates MCP tool-loop, FHIR R6 CRUD, tenant isolation,
 * de-identification, audit trail, human-in-the-loop, and OAuth flows.
 */

const API = '/r6/fhir';
const TENANT = 'demo-tenant';

// --------------- API Client ---------------

async function r6Fetch(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...(opts.headers || {}) };
  const res = await fetch(`${API}${path}`, { ...opts, headers });
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  return { status: res.status, headers: Object.fromEntries(res.headers.entries()), body };
}

// --------------- JSON Syntax Highlighting ---------------

function highlightJSON(obj, indent = 2) {
  const raw = typeof obj === 'string' ? obj : JSON.stringify(obj, null, indent);
  return raw
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"([^"]+)":/g, '<span class="jk">"$1"</span>:')
    .replace(/: "(.*?)"/g, ': <span class="js">"$1"</span>')
    .replace(/: (-?\d+\.?\d*)/g, ': <span class="jn">$1</span>')
    .replace(/: (true|false)/g, ': <span class="jb">$1</span>')
    .replace(/: (null)/g, ': <span class="jl">$1</span>');
}

function showResult(id, data, status) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = highlightJSON(typeof data === 'object' ? data : { raw: data });
  if (status) {
    const badge = status < 300 ? 'success' : status < 500 ? 'warning' : 'danger';
    el.insertAdjacentHTML('beforebegin',
      `<div class="r6-tag r6-tag-${badge === 'success' ? 'read' : badge === 'warning' ? 'write' : 'security'}" style="margin-bottom:0.5rem">HTTP ${status}</div>`);
  }
}

function clearResult(id) {
  const el = document.getElementById(id);
  if (el) { el.innerHTML = ''; el.previousElementSibling?.classList.contains('r6-tag') && el.previousElementSibling.remove(); }
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `r6-status-msg ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function setLoading(btnId, loading) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    btn._origHTML = btn.innerHTML;
    btn.innerHTML = '<span class="r6-spinner"></span> Running...';
  } else {
    btn.disabled = false;
    if (btn._origHTML) btn.innerHTML = btn._origHTML;
  }
}

// --------------- 1. System Health ---------------

async function loadSystemHealth() {
  try {
    const res = await r6Fetch('/metadata');
    if (res.status === 200) {
      const cs = res.body;
      document.getElementById('stat-version').textContent = cs.software?.version || '—';
      document.getElementById('stat-fhir').textContent = cs.fhirVersion || '—';
      const types = (cs.rest?.[0]?.resource || []).map(r => r.type);
      document.getElementById('stat-resources').textContent = types.length;
      const ops = (cs.rest?.[0]?.operation || []).map(o => o.name);
      document.getElementById('stat-operations').textContent = ops.length;
      document.getElementById('stat-status').innerHTML = '<span style="color:var(--r6-success)">Online</span>';
    }
    // Check upstream connection status via health endpoint
    const healthRes = await fetch('/r6/fhir/health');
    if (healthRes.ok) {
      const health = await healthRes.json();
      const modeEl = document.getElementById('stat-mode');
      if (modeEl) {
        if (health.mode === 'upstream') {
          const upstream = health.checks?.upstream || {};
          if (upstream.status === 'connected') {
            modeEl.innerHTML = '<span style="color:var(--r6-success)">Upstream</span>';
            modeEl.title = upstream.upstream_url + ' (' + (upstream.software || '') + ')';
          } else {
            modeEl.innerHTML = '<span style="color:var(--r6-danger)">Upstream (down)</span>';
          }
        } else {
          modeEl.innerHTML = '<span style="color:var(--r6-info, #60a5fa)">Local</span>';
        }
      }
    }
  } catch (e) {
    document.getElementById('stat-status').innerHTML = '<span style="color:var(--r6-danger)">Offline</span>';
  }
}

// --------------- 2. Patient Explorer ---------------

async function loadSamplePatient() {
  setLoading('btn-load-patient', true);
  const patient = {
    resourceType: 'Patient', id: 'demo-pt-1',
    name: [{ family: 'Martinez', given: ['Elena', 'Sofia'] }],
    gender: 'female', birthDate: '1985-07-22',
    identifier: [{ system: 'http://hospital.example/mrn', value: 'MRN98765432' }],
    address: [{ line: ['742 Evergreen Terrace'], city: 'Portland', state: 'OR', postalCode: '97201', country: 'US' }],
    telecom: [{ system: 'phone', value: '503-555-0142', use: 'home' }]
  };

  // Get a step-up token first
  const tokenRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tokenRes.body?.token;

  const res = await r6Fetch('/Patient', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify(patient)
  });

  if (res.status === 201 || res.status === 200) {
    toast('Patient created successfully', 'success');
    // Now read it back (redacted)
    const readRes = await r6Fetch(`/Patient/demo-pt-1`);
    showResult('patient-result', readRes.body);
    document.getElementById('patient-etag').textContent = readRes.headers['etag'] || '—';
  } else {
    showResult('patient-result', res.body);
  }
  setLoading('btn-load-patient', false);
}

async function searchPatients() {
  setLoading('btn-search-patients', true);
  const res = await r6Fetch('/Patient?_count=10');
  showResult('patient-result', res.body);
  setLoading('btn-search-patients', false);
}

async function searchPatientCount() {
  setLoading('btn-patient-count', true);
  const res = await r6Fetch('/Patient?_summary=count');
  showResult('patient-result', res.body);
  setLoading('btn-patient-count', false);
}

// --------------- 3. Agent Tool Loop ---------------

const TOOL_DEFS = [
  { name: 'context.get', desc: 'Retrieve bounded context envelope', tier: 'read' },
  { name: 'fhir.read', desc: 'Read a FHIR resource (redacted)', tier: 'read' },
  { name: 'fhir.search', desc: 'Search FHIR resources', tier: 'read' },
  { name: 'fhir.validate', desc: 'Validate a resource against R6', tier: 'read' },
  { name: 'fhir.propose_write', desc: 'Validate + preview a write (no commit)', tier: 'write' },
  { name: 'fhir.commit_write', desc: 'Commit a write (requires step-up)', tier: 'write' },
  // Additional tools
  { name: 'fhir.stats', desc: '$stats — numeric observation statistics (std FHIR)', tier: 'read', phase: 2 },
  { name: 'fhir.lastn', desc: '$lastn — most recent observations (std FHIR)', tier: 'read', phase: 2 },
  { name: 'fhir.permission_evaluate', desc: 'Permission $evaluate — R6 access control', tier: 'read', phase: 2 },
  { name: 'fhir.subscription_topics', desc: 'SubscriptionTopic $list — R6 pub/sub discovery', tier: 'read', phase: 2 },
];

function renderToolCards() {
  const container = document.getElementById('tool-cards');
  if (!container) return;
  container.innerHTML = TOOL_DEFS.map(t => `
    <div class="tool-card" data-tool="${t.name}" onclick="selectTool('${t.name}')">
      <div class="d-flex align-items-center gap-2 flex-wrap">
        <span class="tool-name">${t.name}</span>
        <span class="r6-tag r6-tag-${t.tier === 'read' ? 'read' : 'write'}">${t.tier}</span>
        ${t.phase === 2 ? '<span class="r6-tag r6-tag-clinical" style="font-size:0.6rem">R6</span>' : ''}
      </div>
      <div class="tool-desc">${t.desc}</div>
    </div>
  `).join('');
}

function selectTool(name) {
  document.querySelectorAll('.tool-card').forEach(c => c.classList.toggle('active', c.dataset.tool === name));
  document.getElementById('selected-tool').textContent = name;
  // Pre-fill example input
  const examples = {
    'fhir.read': { resource_type: 'Patient', resource_id: 'demo-pt-1' },
    'fhir.search': { resource_type: 'Observation', code: '2339-0', status: 'final', _count: 5 },
    'fhir.validate': { resource: { resourceType: 'Observation', status: 'final', code: { coding: [{ system: 'http://loinc.org', code: '2339-0' }] } } },
    'context.get': { context_id: '(ingest a bundle first)' },
    'fhir.propose_write': { resource: { resourceType: 'Observation', id: 'new-obs', status: 'preliminary', code: { coding: [{ system: 'http://loinc.org', code: '8867-4' }] } }, operation: 'create' },
    'fhir.commit_write': { resource: { resourceType: 'Patient', id: 'demo-pt-1', name: [{ family: 'Martinez-Updated' }], gender: 'female', birthDate: '1985-07-22' }, operation: 'update' },
    // Phase 2 tools
    'fhir.stats': { code: '2339-0' },
    'fhir.lastn': { code: '2339-0', max: 3 },
    'fhir.permission_evaluate': { subject: 'Practitioner/dr-1', action: 'read', resource: 'Patient/demo-pt-1' },
    'fhir.subscription_topics': {},
  };
  document.getElementById('tool-input').value = JSON.stringify(examples[name] || {}, null, 2);
}

async function executeSelectedTool() {
  const toolName = document.getElementById('selected-tool').textContent;
  if (!toolName || toolName === '—') { toast('Select a tool first', 'error'); return; }

  setLoading('btn-exec-tool', true);
  clearResult('tool-result');

  let input;
  try {
    input = JSON.parse(document.getElementById('tool-input').value);
  } catch { toast('Invalid JSON input', 'error'); setLoading('btn-exec-tool', false); return; }

  let res;
  try {
    switch (toolName) {
      case 'fhir.read':
        res = await r6Fetch(`/${input.resource_type}/${input.resource_id}`);
        break;
      case 'fhir.search': {
        const params = new URLSearchParams();
        if (input.patient) params.set('patient', input.patient);
        if (input.code) params.set('code', input.code);
        if (input.status) params.set('status', input.status);
        if (input._lastUpdated) params.set('_lastUpdated', input._lastUpdated);
        if (input._count) params.set('_count', input._count);
        if (input._sort) params.set('_sort', input._sort);
        if (input._summary) params.set('_summary', input._summary);
        res = await r6Fetch(`/${input.resource_type}?${params}`);
        break;
      }
      case 'fhir.validate':
        res = await r6Fetch(`/${input.resource.resourceType}/$validate`, {
          method: 'POST', body: JSON.stringify(input.resource)
        });
        break;
      case 'context.get':
        res = await r6Fetch(`/context/${input.context_id}`);
        break;
      case 'fhir.propose_write':
        // Propose = validate only
        res = await r6Fetch(`/${input.resource.resourceType}/$validate`, {
          method: 'POST', body: JSON.stringify(input.resource)
        });
        res.body = { operation: input.operation, validation: res.body, requires_step_up: true, message: 'Validated. Provide X-Step-Up-Token to commit.' };
        break;
      case 'fhir.commit_write': {
        const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
        const tok = tkRes.body?.token;
        const method = input.operation === 'create' ? 'POST' : 'PUT';
        const url = input.operation === 'create' ? `/${input.resource.resourceType}` : `/${input.resource.resourceType}/${input.resource.id}`;
        res = await r6Fetch(url, {
          method,
          headers: { 'X-Step-Up-Token': tok || '', 'X-Human-Confirmed': 'true' },
          body: JSON.stringify(input.resource)
        });
        break;
      }
      // Phase 2: R6-specific tool execution
      case 'fhir.stats': {
        const sp = new URLSearchParams();
        if (input.code) sp.set('code', input.code);
        if (input.patient) sp.set('patient', input.patient);
        res = await r6Fetch(`/Observation/$stats?${sp}`);
        break;
      }
      case 'fhir.lastn': {
        const lp = new URLSearchParams();
        if (input.code) lp.set('code', input.code);
        if (input.patient) lp.set('patient', input.patient);
        if (input.max) lp.set('max', input.max);
        res = await r6Fetch(`/Observation/$lastn?${lp}`);
        break;
      }
      case 'fhir.permission_evaluate':
        res = await r6Fetch('/Permission/$evaluate', {
          method: 'POST', body: JSON.stringify(input)
        });
        break;
      case 'fhir.subscription_topics':
        res = await r6Fetch('/SubscriptionTopic/$list');
        break;
      default:
        res = { status: 400, body: { error: 'Unknown tool' } };
    }
  } catch (e) {
    res = { status: 500, body: { error: e.message } };
  }

  showResult('tool-result', res.body, res.status);
  setLoading('btn-exec-tool', false);
  refreshAuditFeed();
}

// --------------- 4. Context Builder ---------------

async function ingestDemoBundle() {
  setLoading('btn-ingest', true);
  clearResult('context-result');

  const bundle = {
    resourceType: 'Bundle', type: 'collection',
    entry: [
      { resource: { resourceType: 'Patient', id: 'ctx-pt-1', name: [{ family: 'Nguyen', given: ['Tran'] }], gender: 'male', birthDate: '1972-11-03', identifier: [{ value: 'MRN555111' }] } },
      { resource: { resourceType: 'Observation', id: 'ctx-obs-1', status: 'final', code: { coding: [{ system: 'http://loinc.org', code: '2339-0', display: 'Glucose' }] }, subject: { reference: 'Patient/ctx-pt-1' }, valueQuantity: { value: 110, unit: 'mg/dL' } } },
      { resource: { resourceType: 'Observation', id: 'ctx-obs-2', status: 'final', code: { coding: [{ system: 'http://loinc.org', code: '8867-4', display: 'Heart rate' }] }, subject: { reference: 'Patient/ctx-pt-1' }, valueQuantity: { value: 72, unit: '/min' } } },
    ]
  };

  const res = await r6Fetch('/Bundle/$ingest-context', { method: 'POST', body: JSON.stringify(bundle) });
  showResult('context-result', res.body, res.status);

  if (res.status === 201 && res.body.context_id) {
    window._lastContextId = res.body.context_id;
    document.getElementById('last-context-id').textContent = res.body.context_id;
    toast(`Context created: ${res.body.resource_count} resources`, 'success');
  }
  setLoading('btn-ingest', false);
  refreshAuditFeed();
}

async function retrieveContext() {
  const cid = window._lastContextId || document.getElementById('context-id-input')?.value;
  if (!cid) { toast('Ingest a bundle first to get a context ID', 'error'); return; }
  setLoading('btn-get-context', true);
  const res = await r6Fetch(`/context/${cid}`);
  showResult('context-result', res.body, res.status);
  setLoading('btn-get-context', false);
}

// --------------- 5. De-identification ---------------

async function runDeidentify() {
  setLoading('btn-deidentify', true);

  // Ensure patient exists
  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;
  await r6Fetch('/Patient', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify({
      resourceType: 'Patient', id: 'deid-pt-1',
      name: [{ family: 'Washington', given: ['George'] }],
      birthDate: '1978-04-12', gender: 'male',
      identifier: [{ system: 'http://example.org/ssn', value: '123-45-6789' }],
      address: [{ line: ['1600 Pennsylvania Ave'], city: 'Washington', state: 'DC', postalCode: '20500' }],
      telecom: [{ system: 'phone', value: '202-456-1111' }]
    })
  });

  // Read raw (redacted but with structure)
  const rawRes = await r6Fetch('/Patient/deid-pt-1');
  document.getElementById('deid-raw').innerHTML = highlightJSON(rawRes.body);

  // De-identify
  const deidRes = await r6Fetch('/Patient/deid-pt-1/$deidentify');
  document.getElementById('deid-safe').innerHTML = highlightJSON(deidRes.body);

  setLoading('btn-deidentify', false);
  refreshAuditFeed();
}

// --------------- 6. Human-in-the-Loop Demo ---------------

async function demoHumanInLoop() {
  setLoading('btn-hitl-demo', true);
  clearResult('hitl-result');

  const obs = {
    resourceType: 'Observation', id: 'hitl-obs-1', status: 'final',
    code: { coding: [{ system: 'http://loinc.org', code: '2339-0' }] },
    valueQuantity: { value: 200, unit: 'mg/dL' }
  };

  // Step 1: Try without human confirmation → expect 428
  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const res1 = await r6Fetch('/Observation', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify(obs)
  });

  const step1 = { step: '1 — Without X-Human-Confirmed', status: res1.status, body: res1.body };

  // Step 2: With human confirmation → expect 201
  const tkRes2 = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token2 = tkRes2.body?.token;
  obs.id = 'hitl-obs-2';
  const res2 = await r6Fetch('/Observation', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token2 || '', 'X-Human-Confirmed': 'true' },
    body: JSON.stringify(obs)
  });

  const step2 = { step: '2 — With X-Human-Confirmed: true', status: res2.status, body: res2.body };

  showResult('hitl-result', { walkthrough: [step1, step2] });
  toast('Human-in-the-loop demo complete', 'success');
  setLoading('btn-hitl-demo', false);
  refreshAuditFeed();
}

// --------------- 7. OAuth / Security ---------------

async function demoOAuthFlow() {
  setLoading('btn-oauth-demo', true);
  clearResult('oauth-result');

  const steps = [];

  // Step 1: Register client
  const regRes = await r6Fetch('/oauth/register', {
    method: 'POST',
    body: JSON.stringify({ client_name: 'Demo Agent', redirect_uris: ['http://localhost:5000/callback'], scope: 'fhir.read context.read' })
  });
  steps.push({ step: '1 — Dynamic Client Registration', status: regRes.status, client_id: regRes.body?.client_id });

  if (regRes.status !== 201) {
    showResult('oauth-result', { steps });
    setLoading('btn-oauth-demo', false);
    return;
  }

  const clientId = regRes.body.client_id;

  // Step 2: PKCE challenge
  const verifier = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('');
  const encoder = new TextEncoder();
  const hash = await crypto.subtle.digest('SHA-256', encoder.encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');

  const authRes = await r6Fetch(`/oauth/authorize?client_id=${clientId}&redirect_uri=http://localhost:5000/callback&scope=fhir.read&code_challenge=${challenge}&code_challenge_method=S256&state=demo-state`);
  steps.push({ step: '2 — Authorization (PKCE S256)', status: authRes.status, code: authRes.body?.code ? '***' + authRes.body.code.slice(-8) : null });

  if (!authRes.body?.code) {
    showResult('oauth-result', { steps });
    setLoading('btn-oauth-demo', false);
    return;
  }

  // Step 3: Token exchange
  const tokenRes = await r6Fetch('/oauth/token', {
    method: 'POST',
    body: JSON.stringify({ grant_type: 'authorization_code', code: authRes.body.code, code_verifier: verifier, client_id: clientId })
  });
  steps.push({ step: '3 — Token Exchange', status: tokenRes.status, token_type: tokenRes.body?.token_type, scope: tokenRes.body?.scope, expires_in: tokenRes.body?.expires_in });

  // Step 4: Revoke
  if (tokenRes.body?.access_token) {
    const revRes = await r6Fetch('/oauth/revoke', {
      method: 'POST',
      body: JSON.stringify({ token: tokenRes.body.access_token })
    });
    steps.push({ step: '4 — Token Revocation', status: revRes.status });
  }

  showResult('oauth-result', { walkthrough: steps });
  toast('OAuth 2.1 + PKCE flow complete', 'success');
  setLoading('btn-oauth-demo', false);
}

// --------------- 8. Audit Trail ---------------

async function refreshAuditFeed() {
  try {
    const res = await r6Fetch('/AuditEvent?_count=20');
    if (res.status !== 200) return;

    const entries = res.body.entry || [];
    const feed = document.getElementById('audit-feed');
    if (!feed) return;

    if (entries.length === 0) {
      feed.innerHTML = '<div class="text-muted" style="padding:1rem;font-size:0.85rem">No audit events yet. Interact with the panels above to generate events.</div>';
      return;
    }

    feed.innerHTML = entries.map(e => {
      const r = e.resource;
      const type = r.type?.display || r.action || '?';
      const entity = r.entity?.[0]?.what?.reference || '';
      const time = r.recorded ? new Date(r.recorded).toLocaleTimeString() : '';
      const ok = r.outcome?.code?.code === '0';
      return `
        <div class="audit-entry">
          <span class="audit-dot ${type}"></span>
          <div>
            <span class="audit-text"><strong>${type}</strong> ${entity}</span>
            <div class="audit-time">${time} — ${ok ? 'success' : 'failure'}</div>
          </div>
        </div>`;
    }).join('');

    document.getElementById('stat-audits').textContent = res.body.total || entries.length;
  } catch { /* silent */ }
}

async function exportAuditNDJSON() {
  setLoading('btn-export-audit', true);
  try {
    const res = await fetch(`${API}/AuditEvent/$export`, { headers: { 'X-Tenant-Id': TENANT } });
    const text = await res.text();
    const blob = new Blob([text], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'audit-trail.ndjson'; a.click();
    URL.revokeObjectURL(url);
    toast('Audit trail exported', 'success');
  } catch (e) {
    toast('Export failed: ' + e.message, 'error');
  }
  setLoading('btn-export-audit', false);
}

// --------------- 9. Validate Demo ---------------

async function validateDemo() {
  setLoading('btn-validate', true);
  clearResult('validate-result');
  const input = document.getElementById('validate-input').value;
  let resource;
  try { resource = JSON.parse(input); } catch { toast('Invalid JSON', 'error'); setLoading('btn-validate', false); return; }
  const rt = resource.resourceType;
  if (!rt) { toast('resourceType is required', 'error'); setLoading('btn-validate', false); return; }
  const res = await r6Fetch(`/${rt}/$validate`, { method: 'POST', body: JSON.stringify(resource) });
  showResult('validate-result', res.body, res.status);
  setLoading('btn-validate', false);
}

// --------------- Phase 2: Permission (R6 Access Control) ---------------

async function createDemoPermission() {
  setLoading('btn-create-permission', true);
  clearResult('permission-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const permission = {
    resourceType: 'Permission',
    id: 'demo-permission-1',
    status: 'active',
    combining: 'deny-overrides',
    asserter: { reference: 'Organization/hospital-1' },
    justification: {
      basis: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v3-ActReason', code: 'TREAT', display: 'Treatment' }] }]
    },
    rule: [
      {
        type: 'permit',
        activity: [{
          action: [{ coding: [{ system: 'http://hl7.org/fhir/permission-action', code: 'read' }] }],
          purpose: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v3-ActReason', code: 'TREAT' }] }]
        }]
      },
      {
        type: 'deny',
        activity: [{
          action: [{ coding: [{ system: 'http://hl7.org/fhir/permission-action', code: 'delete' }] }]
        }]
      }
    ]
  };

  const res = await r6Fetch('/Permission', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify(permission)
  });

  showResult('permission-result', res.body, res.status);
  if (res.status === 201) toast('R6 Permission created (deny-overrides)', 'success');
  setLoading('btn-create-permission', false);
  refreshAuditFeed();
}

async function evaluatePermission() {
  setLoading('btn-eval-permission', true);
  clearResult('permission-result');

  const evalReq = {
    subject: 'Practitioner/dr-jones',
    action: 'read',
    resource: 'Patient/demo-pt-1'
  };

  const res = await r6Fetch('/Permission/$evaluate', {
    method: 'POST', body: JSON.stringify(evalReq)
  });

  showResult('permission-result', res.body, res.status);
  const decision = res.body?.parameter?.find(p => p.name === 'decision')?.valueCode;
  toast(`Permission decision: ${decision || 'unknown'}`, decision === 'permit' ? 'success' : 'error');
  setLoading('btn-eval-permission', false);
  refreshAuditFeed();
}

// --------------- Phase 2: Observation $stats / $lastn ---------------

async function seedObservations() {
  setLoading('btn-seed-obs', true);
  clearResult('stats-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const glucoseValues = [95, 110, 120, 88, 105, 130, 98, 115];
  const results = [];

  for (let i = 0; i < glucoseValues.length; i++) {
    const obs = {
      resourceType: 'Observation', id: `stats-obs-${i}`,
      status: 'final',
      code: { coding: [{ system: 'http://loinc.org', code: '2339-0', display: 'Glucose [Mass/volume] in Blood' }] },
      subject: { reference: 'Patient/demo-pt-1' },
      valueQuantity: { value: glucoseValues[i], unit: 'mg/dL', system: 'http://unitsofmeasure.org', code: 'mg/dL' }
    };

    const res = await r6Fetch('/Observation', {
      method: 'POST',
      headers: { 'X-Step-Up-Token': token || '', 'X-Human-Confirmed': 'true' },
      body: JSON.stringify(obs)
    });
    results.push({ id: obs.id, value: glucoseValues[i], status: res.status });
  }

  showResult('stats-result', { seeded: results.length, observations: results });
  toast(`Seeded ${results.length} Glucose observations`, 'success');
  setLoading('btn-seed-obs', false);
  refreshAuditFeed();
}

async function runObsStats() {
  setLoading('btn-run-stats', true);
  clearResult('stats-result');

  const res = await r6Fetch('/Observation/$stats?code=2339-0');
  showResult('stats-result', res.body, res.status);

  const count = res.body?.parameter?.find(p => p.name === 'count')?.valueInteger;
  const mean = res.body?.parameter?.find(p => p.name === 'mean')?.valueDecimal;
  if (count) toast(`$stats: ${count} observations, mean=${mean}`, 'success');
  setLoading('btn-run-stats', false);
}

async function runObsLastN() {
  setLoading('btn-run-lastn', true);
  clearResult('stats-result');

  const res = await r6Fetch('/Observation/$lastn?code=2339-0&max=3');
  showResult('stats-result', res.body, res.status);
  toast(`$lastn: ${res.body?.total || 0} observations returned`, 'success');
  setLoading('btn-run-lastn', false);
}

// --------------- Phase 2: SubscriptionTopic ---------------

async function createDemoTopic() {
  setLoading('btn-create-topic', true);
  clearResult('subscription-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const topic = {
    resourceType: 'SubscriptionTopic',
    id: 'encounter-admit',
    url: 'http://example.org/fhir/SubscriptionTopic/encounter-admit',
    status: 'active',
    title: 'Encounter Admission Events',
    description: 'Triggers when an Encounter status changes to in-progress (admission)',
    resourceTrigger: [{
      description: 'Encounter admission trigger',
      resource: 'Encounter',
      supportedInteraction: ['create', 'update'],
      queryCriteria: {
        current: 'status=in-progress',
        resultForCreate: 'test-passes',
        resultForDelete: 'test-fails'
      }
    }],
    canFilterBy: [
      { description: 'Filter by patient', resource: 'Encounter', filterParameter: 'patient' },
      { description: 'Filter by class', resource: 'Encounter', filterParameter: 'class' }
    ],
    notificationShape: [{
      resource: 'Encounter',
      include: ['Encounter:patient', 'Encounter:practitioner']
    }]
  };

  const res = await r6Fetch('/SubscriptionTopic', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify(topic)
  });

  showResult('subscription-result', res.body, res.status);
  if (res.status === 201) toast('SubscriptionTopic created: encounter-admit', 'success');
  setLoading('btn-create-topic', false);
  refreshAuditFeed();
}

async function listTopics() {
  setLoading('btn-list-topics', true);
  clearResult('subscription-result');

  const res = await r6Fetch('/SubscriptionTopic/$list');
  showResult('subscription-result', res.body, res.status);
  toast(`Found ${res.body?.total || 0} SubscriptionTopics`, 'success');
  setLoading('btn-list-topics', false);
}

async function createDemoSubscription() {
  setLoading('btn-create-subscription', true);
  clearResult('subscription-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const subscription = {
    resourceType: 'Subscription',
    id: 'sub-encounter-admit',
    status: 'requested',
    topic: 'http://example.org/fhir/SubscriptionTopic/encounter-admit',
    reason: 'Monitor patient admissions for care coordination',
    channelType: { system: 'http://terminology.hl7.org/CodeSystem/subscription-channel-type', code: 'rest-hook' },
    endpoint: 'https://agent.example.org/webhooks/admission',
    heartbeatPeriod: 60,
    content: 'id-only',
    maxCount: 10,
    filterBy: [{
      resourceType: 'Encounter',
      filterParameter: 'patient',
      value: 'Patient/demo-pt-1'
    }]
  };

  const res = await r6Fetch('/Subscription', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '' },
    body: JSON.stringify(subscription)
  });

  showResult('subscription-result', res.body, res.status);
  if (res.status === 201) toast('Subscription created for encounter-admit', 'success');
  setLoading('btn-create-subscription', false);
  refreshAuditFeed();
}

// --------------- Phase 2: NutritionIntake + DeviceAlert ---------------

async function createNutritionIntake() {
  setLoading('btn-create-nutrition', true);
  clearResult('r6resources-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const intake = {
    resourceType: 'NutritionIntake',
    id: 'demo-nutrition-1',
    status: 'completed',
    subject: { reference: 'Patient/demo-pt-1' },
    consumedItem: [{
      type: { coding: [{ system: 'http://snomed.info/sct', code: '226059008', display: 'Breakfast cereal' }] },
      nutritionProduct: { concept: { coding: [{ system: 'http://snomed.info/sct', code: '226029003', display: 'Corn flakes' }] } },
      amount: { value: 1, unit: 'serving', system: 'http://unitsofmeasure.org', code: '{serving}' }
    }],
    reportedBoolean: true
  };

  const res = await r6Fetch('/NutritionIntake', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '', 'X-Human-Confirmed': 'true' },
    body: JSON.stringify(intake)
  });

  showResult('r6resources-result', res.body, res.status);
  if (res.status === 201) toast('NutritionIntake recorded', 'success');
  setLoading('btn-create-nutrition', false);
  refreshAuditFeed();
}

async function createDeviceAlert() {
  setLoading('btn-create-alert', true);
  clearResult('r6resources-result');

  const tkRes = await r6Fetch('/internal/step-up-token', { method: 'POST', body: JSON.stringify({ tenant_id: TENANT }) });
  const token = tkRes.body?.token;

  const alert = {
    resourceType: 'DeviceAlert',
    id: 'demo-alert-1',
    status: 'active',
    condition: {
      coding: [{
        system: 'urn:iso:std:iso:11073:10101',
        code: 'MDC_EVT_HI_GT_LIM',
        display: 'High limit alarm'
      }]
    },
    device: { reference: 'Device/infusion-pump-1' },
    subject: { reference: 'Patient/demo-pt-1' },
    derivedFrom: [{ reference: 'Observation/stats-obs-6' }]
  };

  const res = await r6Fetch('/DeviceAlert', {
    method: 'POST',
    headers: { 'X-Step-Up-Token': token || '', 'X-Human-Confirmed': 'true' },
    body: JSON.stringify(alert)
  });

  showResult('r6resources-result', res.body, res.status);
  if (res.status === 201) toast('DeviceAlert created (high limit alarm)', 'success');
  setLoading('btn-create-alert', false);
  refreshAuditFeed();
}

// --------------- Agent Guardrail Demo Loop ---------------

async function runDemoLoop() {
  setLoading('btn-demo-loop', true);
  const stepsEl = document.getElementById('demo-steps');
  const detailEl = document.getElementById('demo-step-detail');
  stepsEl.style.display = 'block';
  detailEl.style.display = 'block';
  detailEl.innerHTML = '';

  // Reset all steps
  document.querySelectorAll('.demo-step').forEach(s => {
    s.className = 'demo-step';
    s.querySelector('.demo-step-indicator').innerHTML = '';
  });

  // Scroll into view
  document.getElementById('demo-loop-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });

  try {
    // Fire the backend orchestration
    const res = await r6Fetch('/demo/agent-loop', { method: 'POST' });

    if (res.status !== 200) {
      detailEl.innerHTML = highlightJSON(res.body);
      setLoading('btn-demo-loop', false);
      return;
    }

    const data = res.body;
    const steps = data.steps || [];

    // Animate through each step with delays
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      const stepEl = document.querySelector(`.demo-step[data-step="${step.step}"]`);
      if (!stepEl) continue;

      // Mark active
      stepEl.className = 'demo-step active';
      stepEl.querySelector('.demo-step-indicator').innerHTML = '<span class="r6-spinner" style="width:14px;height:14px;border-width:2px"></span>';

      // Show step detail
      const stepDetail = {
        step: step.step,
        title: step.title,
        guardrail: step.guardrail,
        action: step.action,
        detail: step.detail,
      };
      detailEl.innerHTML = highlightJSON(stepDetail);

      await sleep(800);

      // Show full result
      detailEl.innerHTML = highlightJSON(step);

      // Mark completed with appropriate state
      const stateClass = step.status === 'denied' ? 'denied'
        : step.status === 'permitted' ? 'permitted'
        : 'completed';
      stepEl.className = `demo-step ${stateClass}`;

      const icon = step.status === 'denied' ? '<i class="fas fa-times" style="font-size:0.7rem"></i>'
        : step.status === 'permitted' ? '<i class="fas fa-check" style="font-size:0.7rem"></i>'
        : step.status === 'awaiting_confirmation' ? '<i class="fas fa-hand-paper" style="font-size:0.7rem"></i>'
        : '<i class="fas fa-check" style="font-size:0.7rem"></i>';
      stepEl.querySelector('.demo-step-indicator').innerHTML = icon;

      // Toast per step
      const toastType = step.status === 'denied' ? 'error'
        : step.status === 'permitted' || step.status === 'committed' ? 'success'
        : 'info';
      toast(`Step ${step.step}: ${step.title}`, toastType);

      refreshAuditFeed();

      if (i < steps.length - 1) await sleep(1200);
    }

    // Final summary
    await sleep(600);
    detailEl.innerHTML = highlightJSON({
      demo_complete: true,
      guardrails_demonstrated: data.guardrails_demonstrated,
      total_steps: steps.length,
      audit_events_generated: steps.length + ' operations recorded in immutable audit trail',
      message: 'Every step — from read to write — passed through tenant isolation, validation, access control, step-up auth, and human-in-the-loop enforcement.',
    });
    toast('Guardrail demo complete — all 6 patterns demonstrated', 'success');

  } catch (e) {
    detailEl.innerHTML = highlightJSON({ error: e.message });
    toast('Demo failed: ' + e.message, 'error');
  }

  setLoading('btn-demo-loop', false);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// --------------- Curatr: Patient Data Quality ---------------

let _curatrConditionId = null;

async function curatrCreateCondition() {
  clearResult('curatr-result');
  setLoading('btn-curatr-create', true);
  toast('Creating Condition with retired ICD-9 code…', 'info');

  // Get a step-up token first
  const tokenRes = await r6Fetch('/internal/step-up-token', { method: 'POST' });
  if (!tokenRes.body || !tokenRes.body.token) {
    showResult('curatr-result', { error: 'Could not obtain step-up token', detail: tokenRes.body }, tokenRes.status);
    setLoading('btn-curatr-create', false);
    return;
  }
  const stepUpToken = tokenRes.body.token;

  const condition = {
    resourceType: 'Condition',
    clinicalStatus: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/condition-clinical', code: 'active' }] },
    code: {
      coding: [{
        system: 'http://hl7.org/fhir/sid/icd-9-cm',
        code: '250.00',
        display: 'Diabetes mellitus without mention of complication',
      }],
      text: 'Diabetes mellitus',
    },
    subject: { reference: 'Patient/demo-patient' },
  };

  const res = await r6Fetch('/Condition', {
    method: 'POST',
    body: JSON.stringify(condition),
    headers: { 'X-Step-Up-Token': stepUpToken, 'X-Human-Confirmed': 'true' },
  });

  if (res.status === 201) {
    _curatrConditionId = res.body.id;
    showResult('curatr-result', {
      status: 'Condition created',
      id: _curatrConditionId,
      note: 'Uses retired ICD-9-CM code 250.00 — ready for Curatr evaluation',
      condition: res.body,
    }, res.status);
    toast('ICD-9 Condition created — run $curatr-evaluate to check quality', 'success');
  } else {
    showResult('curatr-result', res.body, res.status);
    toast('Create failed: ' + res.status, 'error');
  }
  setLoading('btn-curatr-create', false);
}

async function curatrEvaluate() {
  if (!_curatrConditionId) {
    toast('Create a Condition first', 'error');
    return;
  }
  clearResult('curatr-result');
  setLoading('btn-curatr-evaluate', true);
  toast('Evaluating against live terminology services…', 'info');

  const res = await r6Fetch(`/Condition/${_curatrConditionId}/$curatr-evaluate`);

  if (res.status === 200) {
    const issues = res.body.issues || [];
    const quality = res.body.overall_quality || 'unknown';
    const qualityColor = quality === 'critical' ? '#ef4444' : quality === 'needs-review' ? '#f59e0b' : '#22c55e';

    showResult('curatr-result', {
      overall_quality: quality,
      summary: res.body.summary,
      issue_count: issues.length,
      checked_at: res.body.checked_at,
      issues: issues.map(i => ({
        severity: i.severity,
        title: i.title,
        plain_language: i.plain_language,
        impact: i.impact,
        suggestion: i.suggestion,
        field_path: i.field_path,
      })),
      _note: 'Terminology services: NLM Clinical Tables (ICD-10-CM), tx.fhir.org (SNOMED/LOINC), RXNAV (RxNorm)',
    }, res.status);

    const msg = issues.length > 0
      ? `Found ${issues.length} issue(s) — quality: ${quality}. Run "Apply Fix" to correct.`
      : 'No issues found';
    toast(msg, issues.length > 0 ? 'warning' : 'success');
  } else {
    showResult('curatr-result', res.body, res.status);
    toast('Evaluate failed: ' + res.status, 'error');
  }
  setLoading('btn-curatr-evaluate', false);
}

async function curatrApplyFix() {
  if (!_curatrConditionId) {
    toast('Create and evaluate a Condition first', 'error');
    return;
  }
  clearResult('curatr-result');
  setLoading('btn-curatr-fix', true);
  toast('Applying patient-approved ICD-10-CM fix…', 'info');

  const tokenRes = await r6Fetch('/internal/step-up-token', { method: 'POST' });
  if (!tokenRes.body || !tokenRes.body.token) {
    showResult('curatr-result', { error: 'Could not obtain step-up token' }, tokenRes.status);
    setLoading('btn-curatr-fix', false);
    return;
  }
  const stepUpToken = tokenRes.body.token;

  const fixes = [
    { field_path: 'Condition.code.coding[0].system', new_value: 'http://hl7.org/fhir/sid/icd-10-cm' },
    { field_path: 'Condition.code.coding[0].code', new_value: 'E11.9' },
    { field_path: 'Condition.code.coding[0].display', new_value: 'Type 2 diabetes mellitus without complications' },
  ];

  const res = await r6Fetch(`/Condition/${_curatrConditionId}/$curatr-apply-fix`, {
    method: 'POST',
    body: JSON.stringify({
      fixes,
      patient_intent: 'Updating retired ICD-9 code 250.00 to ICD-10-CM E11.9 — approved by patient via Curatr',
    }),
    headers: { 'X-Step-Up-Token': stepUpToken, 'X-Human-Confirmed': 'true' },
  });

  if (res.status === 200) {
    showResult('curatr-result', {
      status: 'Fix applied',
      issues_fixed: res.body.issues_fixed,
      change_summary: res.body.change_summary,
      provenance_id: res.body.provenance?.id,
      provenance_recorded: res.body.provenance?.recorded,
      provenance_agent: res.body.provenance?.agent?.[0]?.who?.display,
      updated_code: res.body.updated_resource?.code,
      _patient_rights: [
        'Change initiated and approved by the patient',
        'Original ICD-9 data preserved in audit trail',
        'Provenance record documents patient intent verbatim',
        'Patient can request provider correct the source EHR',
      ],
    }, res.status);
    toast(`${res.body.issues_fixed} fix(es) applied — Provenance ${res.body.provenance?.id?.slice(0, 8)}… created`, 'success');
  } else {
    showResult('curatr-result', res.body, res.status);
    toast('Fix failed: ' + res.status, 'error');
  }
  setLoading('btn-curatr-fix', false);
}

// --------------- Walkthrough Mode ---------------

const WALKTHROUGH_STEPS = [
  { title: 'Load Sample Patient', action: loadSamplePatient, panel: 'patient-panel' },
  { title: 'Ingest Bundle + Build Context', action: ingestDemoBundle, panel: 'context-panel' },
  { title: 'Execute Agent Tool (fhir.read)', action: async () => { selectTool('fhir.read'); await executeSelectedTool(); }, panel: 'tools-panel' },
  { title: 'De-identify Patient (Safe Harbor)', action: runDeidentify, panel: 'deid-panel' },
  { title: 'Human-in-the-Loop Enforcement', action: demoHumanInLoop, panel: 'hitl-panel' },
  { title: 'Full OAuth 2.1 + PKCE Flow', action: demoOAuthFlow, panel: 'oauth-panel' },
  // Phase 2 steps
  { title: 'R6 Permission + $evaluate', action: async () => { await createDemoPermission(); await evaluatePermission(); }, panel: 'permission-panel' },
  { title: 'Seed Observations + $stats', action: async () => { await seedObservations(); await runObsStats(); }, panel: 'stats-panel' },
  { title: 'SubscriptionTopic + Subscribe', action: async () => { await createDemoTopic(); await createDemoSubscription(); }, panel: 'subscription-panel' },
  { title: 'NutritionIntake + DeviceAlert', action: async () => { await createNutritionIntake(); await createDeviceAlert(); }, panel: 'r6resources-panel' },
  { title: 'Curatr: Data Quality Evaluation + Fix', action: async () => { await curatrCreateCondition(); await sleep(800); await curatrEvaluate(); }, panel: 'curatr-panel' },
];

let walkthroughIdx = -1;

function startWalkthrough() {
  walkthroughIdx = 0;
  runWalkthroughStep();
}

async function runWalkthroughStep() {
  if (walkthroughIdx >= WALKTHROUGH_STEPS.length) {
    document.getElementById('walkthrough-bar').style.display = 'none';
    toast('Walkthrough complete!', 'success');
    walkthroughIdx = -1;
    return;
  }
  const step = WALKTHROUGH_STEPS[walkthroughIdx];
  const bar = document.getElementById('walkthrough-bar');
  bar.style.display = 'flex';
  bar.querySelector('.step-counter').textContent = `${walkthroughIdx + 1}/${WALKTHROUGH_STEPS.length}`;
  bar.querySelector('.step-text').textContent = step.title;

  // Scroll panel into view
  const panel = document.getElementById(step.panel);
  if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'center' });

  await step.action();
  walkthroughIdx++;
  // Auto-advance after a brief pause
  setTimeout(runWalkthroughStep, 1200);
}

// --------------- Init ---------------

document.addEventListener('DOMContentLoaded', () => {
  loadSystemHealth();
  renderToolCards();
  refreshAuditFeed();
  // Poll audit feed every 10s
  setInterval(refreshAuditFeed, 10000);
});
