import { test, expect, APIRequestContext } from '@playwright/test';

/**
 * The example, asserted against the running stack — and filmed while it runs.
 *
 * Every number on screen is a live response. Nothing here is a re-enactment:
 * the assertions and the recording come from the same requests, so a video
 * that shows a pass cannot exist unless the pass happened. That property is
 * the reason this is a test rather than a screen capture with narration.
 *
 * Requires the stack to be up and seeded:
 *   docker compose up -d && ./scripts/seed-aidbox.sh
 */

const PROXY = `http://localhost:${process.env.HEALTHCLAW_PORT || '5000'}`;
const AIDBOX = process.env.AIDBOX_URL || 'http://localhost:8080';
const AIDBOX_AUTH = 'Basic ' + Buffer.from(
  `${process.env.AIDBOX_CLIENT || 'root'}:${process.env.AIDBOX_SECRET || 'qNbQS6sw82'}`
).toString('base64');
const TENANT = process.env.TENANT || 'demo';
const MCP = `http://localhost:${process.env.MCP_PORT || '3001'}`;
const MCP_TOKEN = process.env.MCP_AUTH_TOKEN || '';

/** Values that must never appear on the agent's side of the proxy. */
const IDENTIFIERS = ['Alvarez', 'Maria', 'MRN-88214', '221 Baker St',
                     '555-867-5309', '1974-03-11'];

const OBSERVATION = {
  resourceType: 'Observation',
  status: 'final',
  subject: { reference: 'Patient/pt-demo' },
  effectiveDateTime: '2026-08-11',
  code: { coding: [{ system: 'http://loinc.org', code: '85354-9' }] },
  valueQuantity: { value: 128, unit: 'mmHg' },
};

const SHELL = `
<style>
  :root{
    --ground:#0f1620; --panel:#16202c; --edge:#243244;
    --ink:#e3eaf2; --muted:#8598ad;
    --guard:#e0a33e;      /* the guardrail acting: held, refused, masked */
    --pass:#57c795;       /* an assertion that held */
    --raw:#7fb2e5;        /* the record as the system of record holds it */
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--ground); color:var(--ink);
    font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;
    padding:22px 30px;
  }
  header{display:flex; align-items:baseline; gap:14px; margin-bottom:6px}
  h1{font-size:19px; font-weight:600; margin:0; letter-spacing:-0.01em}
  .sub{color:var(--muted); font-size:13px}
  .flow{
    color:var(--muted); font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
    margin:2px 0 16px; letter-spacing:0.02em;
  }
  .step{
    background:var(--panel); border:1px solid var(--edge); border-radius:9px;
    padding:13px 16px; margin-bottom:11px;
    opacity:0; transform:translateY(6px);
    animation:in .28s ease-out forwards;
  }
  @keyframes in{to{opacity:1; transform:none}}
  .head{display:flex; align-items:center; gap:10px; margin-bottom:9px}
  .n{
    color:var(--muted); font:11px ui-monospace,monospace; letter-spacing:.1em;
    text-transform:uppercase;
  }
  .t{font-weight:600; font-size:14px}
  .chip{
    margin-left:auto; font:11px/1 ui-monospace,monospace; padding:5px 9px;
    border-radius:20px; letter-spacing:.04em;
  }
  .chip.pass{background:rgba(87,199,149,.14); color:var(--pass);
             border:1px solid rgba(87,199,149,.32)}
  .cols{display:grid; grid-template-columns:1fr 1fr; gap:11px}
  .cols.three{grid-template-columns:1fr 1fr 1fr}
  .box{border:1px solid var(--edge); border-radius:7px; padding:9px 11px;
       background:rgba(0,0,0,.18)}
  .lbl{font:10px ui-monospace,monospace; letter-spacing:.09em;
       text-transform:uppercase; color:var(--muted); margin-bottom:6px}
  .box.raw .lbl{color:var(--raw)}
  .box.guard .lbl{color:var(--guard)}
  pre{margin:0; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
      white-space:pre-wrap; word-break:break-word}
  .raw pre{color:var(--raw)}
  .guard pre{color:var(--guard)}
  table{width:100%; border-collapse:collapse; font:12px ui-monospace,monospace}
  td,th{padding:5px 8px; text-align:left; border-bottom:1px solid var(--edge)}
  th{color:var(--muted); font-weight:400; font-size:10px; letter-spacing:.08em;
     text-transform:uppercase}
  tr:last-child td{border-bottom:none}
  .code{color:var(--guard); font-weight:600}
  .code.ok{color:var(--pass)}
  .note{color:var(--muted); font-size:12px; margin-top:8px; line-height:1.5}
  .yes{color:var(--pass)} .no{color:var(--muted)}
</style>
<header>
  <h1>HealthClaw Guardrails in front of Aidbox</h1>
  <span class="sub">live stack &middot; every value below is a real response</span>
</header>
<div class="flow">AI agent &rarr; MCP server &rarr; guardrail proxy &rarr; Aidbox &nbsp;|&nbsp; redact &middot; audit &middot; step-up &middot; human-in-the-loop</div>
<main id="out"></main>
`;

/** Append one finished step to the page. */
async function render(page, html: string) {
  await page.evaluate((h) => {
    const d = document.createElement('div');
    d.className = 'step';
    d.innerHTML = h;
    document.getElementById('out')!.appendChild(d);
    d.scrollIntoView({ block: 'end' });
  }, html);
  // Pacing is for the recording, not the assertions. A step that
  // appears and scrolls away in under a second is a test that also
  // happens to produce an unwatchable file.
  await page.waitForTimeout(1500);
}

const head = (n: string, t: string) =>
  `<div class="head"><span class="n">${n}</span><span class="t">${t}</span>` +
  `<span class="chip pass">PASS</span></div>`;

const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

async function stepUpToken(request: APIRequestContext): Promise<string> {
  const r = await request.post(`${PROXY}/r6/fhir/internal/step-up-token`, {
    headers: { 'X-Tenant-Id': TENANT, 'Content-Type': 'application/json' },
    data: { tenant_id: TENANT },
  });
  expect(r.status(), 'minting a step-up token').toBe(200);
  const token = (await r.json()).token;
  expect(token, 'the mint returned a token').toBeTruthy();
  return token;
}

test('the guardrails hold, in front of a real FHIR server', async ({ page, request }) => {
  await page.setContent(SHELL);
  await page.waitForTimeout(1200);

  // ---- 0. The stack is what we think it is -------------------------------
  // Asserted first because every step below is meaningless if the proxy is
  // serving its own SQLite store instead of Aidbox — and it would still pass
  // most of them.
  const health = await (await request.get(`${PROXY}/r6/fhir/health`)).json();
  expect(health.mode, 'proxy must be in upstream mode, not local').toBe('upstream');
  expect(health.checks.upstream.status, 'proxy must reach Aidbox').toBe('connected');

  const anon = await request.get(`${AIDBOX}/fhir/Patient/pt-demo`);
  expect(anon.status(), 'Aidbox must refuse anonymous callers').not.toBe(200);

  await render(page, head('00', 'The proxy is talking to Aidbox, with its own credential') + `
    <div class="cols">
      <div class="box"><div class="lbl">proxy /health</div><pre>mode      ${health.mode}
upstream  ${health.checks.upstream.status}
version   ${health.version}</pre></div>
      <div class="box"><div class="lbl">Aidbox, no credential</div><pre>GET /fhir/Patient/pt-demo
&rarr; HTTP ${anon.status()}</pre></div>
    </div>
    <div class="note">The agent never holds an Aidbox credential. The proxy does, and
    Aidbox refuses callers who do not &mdash; so &ldquo;the proxy authenticates&rdquo; is a
    fact here, not a diagram.</div>`);

  // ---- 1. The same record, both ways -------------------------------------
  const token = await stepUpToken(request);
  const authed = { 'X-Tenant-Id': TENANT, 'X-Step-Up-Token': token };

  const direct = await (await request.get(`${AIDBOX}/fhir/Patient/pt-demo`,
    { headers: { Authorization: AIDBOX_AUTH } })).json();
  const noToken = await request.get(`${PROXY}/r6/fhir/Patient/pt-demo`,
    { headers: { 'X-Tenant-Id': TENANT } });
  const proxiedRes = await request.get(`${PROXY}/r6/fhir/Patient/pt-demo`,
    { headers: authed });
  const proxied = await proxiedRes.json();

  // Reads are authenticated: a tenant header alone is not a credential.
  expect(noToken.status(), 'a read without a token must be refused').toBe(401);

  // The shape check comes FIRST and is the load-bearing one. A refusal
  // contains none of the identifiers either, so "nothing leaked" is true of
  // an error body — which is exactly how this demo passed while testing
  // nothing at all.
  expect(proxied.resourceType, 'the proxy must return the record itself').toBe('Patient');
  expect(proxied.id).toBe('pt-demo');
  expect(JSON.stringify(direct), 'Aidbox holds the identified record').toContain('Alvarez');
  for (const id of IDENTIFIERS) {
    expect(JSON.stringify(proxied), `identifier survived redaction: ${id}`).not.toContain(id);
  }

  const pick = (o: any) => JSON.stringify(
    { name: o.name, identifier: o.identifier, birthDate: o.birthDate, address: o.address },
    null, 1).replace(/\n\s*/g, ' ').replace(/([,{])\s/g, '$1\n  ');

  await render(page, head('01', 'The same record, with and without governance') + `
    <div class="cols three">
      <div class="box raw"><div class="lbl">Aidbox &middot; system of record</div><pre>${esc(pick(direct))}</pre></div>
      <div class="box"><div class="lbl">proxy &middot; tenant header only</div><pre>HTTP ${noToken.status()}
a tenant claim is not
a credential</pre></div>
      <div class="box guard"><div class="lbl">proxy &middot; authenticated agent</div><pre>${esc(pick(proxied))}</pre></div>
    </div>
    <div class="note">Initials, a masked MRN, the year only, and the street gone &mdash; while
    Aidbox still holds every character. Redaction is a property of the path the agent
    takes, not an edit to the data.</div>`);

  // ---- 2. The read left a record -----------------------------------------
  const audit = await (await request.get(
    `${PROXY}/r6/fhir/AuditEvent?_count=5`, { headers: authed })).json();
  expect(audit.resourceType, 'expected a Bundle of AuditEvents').toBe('Bundle');
  expect(audit.entry?.length, 'the read emitted no AuditEvent').toBeGreaterThan(0);
  for (const id of IDENTIFIERS) {
    expect(JSON.stringify(audit), `PHI in the audit trail: ${id}`).not.toContain(id);
  }

  const first = audit.entry[0].resource;
  await render(page, head('02', 'The read left a record, and the record is safe to hand over') + `
    <div class="cols">
      <div class="box"><div class="lbl">AuditEvent &middot; ${audit.entry.length} of ${audit.total ?? audit.entry.length}</div><pre>action    ${first.action ?? '-'}
recorded  ${(first.recorded ?? '').slice(0, 19)}
type      ${first.type?.code ?? first.type?.coding?.[0]?.code ?? '-'}</pre></div>
      <div class="box guard"><div class="lbl">checked for PHI</div><pre>${IDENTIFIERS.map(i => '&check; ' + i + ' absent').join('\n')}</pre></div>
    </div>
    <div class="note">Every access is written down, and the detail carries no PHI &mdash; so the
    trail you hand a reviewer is safe to hand over.</div>`);

  // ---- 3. Two gates, neither substituting for the other -------------------
  const write = (headers: Record<string, string>) =>
    request.post(`${PROXY}/r6/fhir/Observation`, {
      headers: { 'X-Tenant-Id': TENANT, 'Content-Type': 'application/fhir+json', ...headers },
      data: OBSERVATION,
    });

  const neither = await write({});
  const humanOnly = await write({ 'X-Human-Confirmed': 'true' });
  const tokenOnly = await write({ 'X-Step-Up-Token': token });
  const both = await write({ 'X-Step-Up-Token': token, 'X-Human-Confirmed': 'true' });

  expect(neither.status(), 'no gate satisfied').toBe(428);
  expect(humanOnly.status(), 'a confirmation is not a credential').toBe(401);
  expect(tokenOnly.status(), 'a credential is not a confirmation').toBe(428);
  expect(both.status(), 'both gates satisfied must succeed').toBe(201);

  // The proxy reporting its own 201 says nothing about storage. Ask Aidbox.
  const landed = await (await request.get(
    `${AIDBOX}/fhir/Observation?subject=Patient/pt-demo&code=85354-9`,
    { headers: { Authorization: AIDBOX_AUTH } })).json();
  expect(landed.total, 'the write returned 201 but Aidbox has no such Observation')
    .toBeGreaterThan(0);

  const row = (h: boolean, t: boolean, s: number, why: string) =>
    `<tr><td>${h ? '<span class="yes">&check;</span>' : '<span class="no">&mdash;</span>'}</td>` +
    `<td>${t ? '<span class="yes">&check;</span>' : '<span class="no">&mdash;</span>'}</td>` +
    `<td class="code${s < 400 ? ' ok' : ''}">${s}</td><td style="color:var(--muted)">${why}</td></tr>`;

  await render(page, head('03', 'A write, and two gates that do not substitute for each other') + `
    <table>
      <tr><th>human</th><th>step-up</th><th>status</th><th></th></tr>
      ${row(false, false, neither.status(), 'human confirmation is missing')}
      ${row(true, false, humanOnly.status(), 'a confirmation is not a credential')}
      ${row(false, true, tokenOnly.status(), 'a credential is not a confirmation')}
      ${row(true, true, both.status(), 'and only then')}
    </table>
    <div class="note">Confirmed in Aidbox, not taken from the proxy&rsquo;s own answer:
    <strong>${landed.total}</strong> matching Observation(s) on Patient/pt-demo.
    The agent did useful work. It could not finish alone.</div>`);

  // ---- 4. Grade it -------------------------------------------------------
  const conf = await (await request.get(`${PROXY}/r6/fhir/$conformance`)).json();
  const failed = (conf.properties || []).filter((p: any) => !p.passed).map((p: any) => p.key);

  // Not "the grade is A". Every property except the known proxy-mode gap
  // must hold; the day #498 closes, this same assertion passes at 7/7.
  expect(failed.filter((k: string) => k !== 'error_fidelity'),
    'a property that should hold did not').toEqual([]);

  const props = (conf.properties || []).map((p: any) =>
    `<tr><td>${p.passed ? '<span class="yes">&check;</span>' : '<span style="color:var(--guard)">&mdash;</span>'}</td>` +
    `<td>${p.key.replace(/_/g, ' ')}</td></tr>`).join('');

  await render(page, head('04', 'Graded against the deployment that is actually running') + `
    <div class="cols">
      <div class="box"><div class="lbl">conformance</div><table>${props}</table></div>
      <div class="box guard"><div class="lbl">grade ${conf.grade} &middot; ${conf.score.passed}/${conf.score.total}</div><pre>The one failure is error
fidelity, and in upstream
mode it measures how AIDBOX
answers an unknown search
parameter &mdash; not how the
guardrail does.

Stated, not graded away.
Tracked as #498.</pre></div>
    </div>
    <div class="note">The same harness runs in CI as a merge gate, so a regression shows up
    as a grade change rather than as an incident.</div>`);

  // ---- 5. The tool surface the agent actually connects to ---------------
  // The service this example's diagram opens with had never started: the
  // proxy health check called curl, which is absent from that image, so mcp
  // waited on a `service_healthy` that never arrived. Nothing asked.
  const listBody = { jsonrpc: '2.0', id: 1, method: 'tools/list' };
  const mcpAnon = await request.post(`${MCP}/mcp/rpc`, { data: listBody });
  expect(mcpAnon.status(), 'the MCP server must refuse unauthenticated callers').toBe(401);

  const mcpAuthed = await request.post(`${MCP}/mcp/rpc`, {
    headers: { Authorization: `Bearer ${MCP_TOKEN}` }, data: listBody,
  });
  expect(mcpAuthed.status()).toBe(200);
  const tools = (await mcpAuthed.json()).result?.tools ?? [];
  expect(tools.length, 'the authenticated tool surface must be served').toBeGreaterThan(0);

  await render(page, head('05', 'What the agent actually connects to') + `
    <div class="cols">
      <div class="box"><div class="lbl">tools/list &middot; no credential</div><pre>HTTP ${mcpAnon.status()}

the MCP server refuses to
start without a token at all,
rather than serve an
unauthenticated tool surface</pre></div>
      <div class="box guard"><div class="lbl">tools/list &middot; authenticated &middot; ${tools.length} tools</div><pre>${tools.slice(0, 7).map((t: any) => '&middot; ' + t.name).join('\n')}
&hellip; and ${tools.length - 7} more</pre></div>
    </div>
    <div class="note">Every one of those tools reaches Aidbox only through the guardrail
    proxy above &mdash; so the four properties hold for the agent's whole surface, not
    just for the requests in this demo.</div>`);

  await page.waitForTimeout(3000);
});
