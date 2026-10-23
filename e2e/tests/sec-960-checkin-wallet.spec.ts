/**
 * Security exploit tests for the SMART Health Check-in web wallet (#960,
 * FHI-54) and its synthetic fixture (#959, FHI-51).
 *
 * A hostile check-in page is played by an origin the browser treats as
 * separate from the wallet: http://127.0.0.1:5394 (and :5396), served by
 * context.route, so no extra server and no internet. It uses the real
 * Verifier library (@smart-health-checkin/client 0.4.4 checkin.js, the
 * release asset, sha256 pinned below) to open the wallet and decrypt what
 * comes back, so what these tests read is exactly what any website that
 * opens the wallet would read.
 *
 * Tests assert the secure behaviour. A red test is a finding.
 *
 * The wallet's records relay (POST /checkin/wallet/records) is answered by
 * route with the engine's checkin export of the synthetic fixture
 * (e2e/fixtures/checkin-export-test-patient.json): the CareAgents under
 * test has no engine behind it. Each body the wallet sends it is kept in RELAY_ASKED.
 *
 * Run against a flag-on CareAgents server:
 *   CARE_E2E_PORT=<port> SEC_CARE_LOG=<that server's stderr log> \
 *     npx playwright test sec-960 --config <a config with no webServer>
 */

import { test, expect, Page, BrowserContext } from '@playwright/test';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { codeFromLog, uniqueEmail } from './careagents-fixtures';

const LOG = process.env.SEC_CARE_LOG || CARE_LOG;
const WALLET = `${CARE_BASE_URL}/checkin/wallet`;
const EVIL = 'http://127.0.0.1:5394';
const ELSEWHERE = 'http://127.0.0.1:5396';
const THIRD_PARTY = 'http://127.0.0.1:5395';

const CHECKIN_JS = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'smart-health-checkin-client', '0.4.4', 'checkin.js'));
const CHECKIN_JS_SHA256 =
  'ad0ad34e28b19c7a5bae6305b1be4a9b98b56f83b4309665054c33b425d4f33c';

const FIXTURE = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..',
  'careagents', 'checkin', 'us_core_test_patient.json'), 'utf8'));
// What the relay answers with: the engine's own checkin export of the test
// patient, upstream words dropped and our labels written (pinned to the
// engine by tests/test_checkin_relay.py).
const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));

const CANARY = 'SEC960CANARY';
const US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/';
const item = (id: string, profile: string) => ({
  id, title: `${CANARY} ${id}`,
  content: { kind: 'selection.fhir', profiles: [US_CORE + profile] },
  accept: ['application/fhir+json'],
});
const REQUEST = {
  purpose: `${CANARY} purpose: I am your doctor`,
  items: [
    item('patient', 'us-core-patient'),
    item('allergies', 'us-core-allergyintolerance'),
    item('coverage', 'us-core-coverage'),
  ],
};

const EVIL_HTML = `<!doctype html><meta charset="utf-8"><title>evil</title>
<button id="go">check in</button>
<button id="open">open only</button>
<iframe id="other" src="http://127.0.0.1:5395/frame.html"></iframe>
<script type="module">
import { runCheckin, webWallet } from '/checkin.js';
window.__msgs = [];
window.addEventListener('message', (e) => window.__msgs.push({
  origin: e.origin, type: e.data && e.data.type, outcome: e.data && e.data.outcome }));
// Opens the wallet and never answers its ready: a quiet opener.
document.getElementById('open').onclick = () => {
  window.__w = window.open(${JSON.stringify(WALLET)}, 'smart-checkin-wallet');
};
document.getElementById('go').onclick = () => {
  const wallet = webWallet({ id: 'sec', name: 'sec', walletUrl: ${JSON.stringify(WALLET)} });
  window.__done = runCheckin(window.__req, { wallet }).then((r) => {
    window.__result = { status: r.status, json: r.response ? r.response.json : null,
      error: r.error || null };
  }, (e) => { window.__result = { status: 'threw', error: String(e) }; });
};
</script>`;
// A third-party frame inside the check-in page (an ad, a widget): another
// origin, not the opener. (A frame on the opener's *own* origin can re-point
// the wallet's window.opener in Chromium with window.open('', name); it is the
// same principal as the page, shows the same origin and seals to it, so that
// is not tested as a bypass.)
const FRAME_HTML = `<!doctype html><meta charset="utf-8"><title>frame</title>
<script>
window.inject = () => window.open('', 'smart-checkin-wallet').postMessage({
  type: 'digital-credentials/web-wallet/request', requestId: 'from-frame',
  credentialRequestOptions: {} }, '*');
</script>`;
const ELSEWHERE_HTML = `<!doctype html><meta charset="utf-8"><title>elsewhere</title>
<script>
window.__msgs = [];
window.addEventListener('message', (e) => window.__msgs.push({
  origin: e.origin, type: e.data && e.data.type, outcome: e.data && e.data.outcome }));
</script>`;

const RELAY_ASKED: unknown[] = [];

async function serveAttacker(context: BrowserContext) {
  RELAY_ASKED.length = 0;
  expect(crypto.createHash('sha256').update(CHECKIN_JS).digest('hex'))
    .toBe(CHECKIN_JS_SHA256);
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    const html = (body: string) => route.fulfill(
      { status: 200, contentType: 'text/html', body });
    if (url.origin === EVIL) {
      if (url.pathname === '/checkin.js') {
        return route.fulfill({ status: 200, contentType: 'text/javascript',
          body: CHECKIN_JS });
      }
      return html(EVIL_HTML);
    }
    if (url.origin === ELSEWHERE) return html(ELSEWHERE_HTML);
    if (url.origin === THIRD_PARTY) return html(FRAME_HTML);
    if (url.hostname === 'localhost' && url.pathname === '/checkin/wallet/records') {
      RELAY_ASKED.push(route.request().postDataJSON());
      return route.fulfill({ status: 200, contentType: 'application/fhir+json',
        headers: { 'Cache-Control': 'no-store' }, body: JSON.stringify(EXPORT) });
    }
    if (url.hostname === 'localhost') return route.continue();
    return route.abort();
  });
}

/** Open the wallet from the hostile page and sign in inside the wallet tab. */
async function openAndSignIn(evil: Page): Promise<Page> {
  await evil.goto(`${EVIL}/`);
  await evil.evaluate((req) => { (window as any).__req = req; }, REQUEST);
  const [wallet] = await Promise.all([
    evil.waitForEvent('popup'),
    evil.locator('#go').click(),
  ]);
  await expect(wallet.locator('#wallet-signin')).toBeVisible();
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, LOG));
  await wallet.locator('#signin-verify').click();
  return wallet;
}

// Its own sign-in code budget. The server allows 30 code requests per client
// address per 10 minutes (app.py CODE_REQUESTS_PER_IP), and this file runs
// last against a server the whole suite has signed in to from 127.0.0.1. With
// no edge in front, the limiter keys on X-Real-IP (TEST-NET-1, RFC 5737).
test.use({ extraHTTPHeaders: { 'X-Real-IP': '192.0.2.60' } });

const result = (evil: Page) => expect.poll(
  () => evil.evaluate(() => (window as any).__result), { timeout: 15_000 });

test.describe('sec #960: hostile opener', () => {
  test.beforeEach(async ({ context }) => { await serveAttacker(context); });

  test('names the real origin, defaults to decline, and a bare Send shares nothing',
    async ({ page }) => {
      const wallet = await openAndSignIn(page);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await expect(wallet.locator('#wallet-origin')).toHaveText(EVIL);
      await expect(wallet.locator('#wallet-purpose'))
        // Isolated so a bidi control in it cannot reorder our words (#1030 F2).
        .toHaveText(`The website says why: ⁨${REQUEST.purpose}⁩`);
      const radios = wallet.locator('#wallet-items input[type=radio]:checked');
      await expect(radios).toHaveCount(3);
      for (const v of await radios.evaluateAll((els) => els.map(
        (e) => (e as HTMLInputElement).value))) expect(v).toBe('decline');
      await wallet.locator('#wallet-send').click();
      await result(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.map((s: any) => s.status))
        .toEqual(['declined', 'declined', 'declined']);
    });

  test('a third-party frame in the check-in page cannot drive the wallet',
    async ({ page }) => {
      await page.goto(`${EVIL}/`);
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'), page.locator('#open').click()]);
      const email = uniqueEmail();
      await wallet.locator('#signin-email').fill(email);
      await wallet.locator('#signin-email-btn').click();
      await wallet.locator('#signin-code-input').fill(await codeFromLog(email, LOG));
      await wallet.locator('#signin-verify').click();
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      const frame = page.frames().find((f) => f.url().startsWith(THIRD_PARTY))!;
      await frame.evaluate(() => (window as any).inject());
      await wallet.waitForTimeout(750);
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      // Control: the same message from the opener itself is taken.
      await page.evaluate(() => (window as any).__w.postMessage({
        type: 'digital-credentials/web-wallet/request', requestId: 'opener',
        credentialRequestOptions: {} }, '*'));
      await expect(wallet.locator('#wallet-done-title'))
        .toHaveText("Couldn't read the request");
    });

  test('a second request from the opener is ignored once one is in review',
    async ({ page }) => {
      const wallet = await openAndSignIn(page);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await page.evaluate(() => window.open('', 'smart-checkin-wallet')!.postMessage({
        type: 'digital-credentials/web-wallet/request', requestId: 'second',
        credentialRequestOptions: {} }, '*'));
      await wallet.waitForTimeout(500);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      expect((await page.evaluate(() => (window as any).__msgs))
        .filter((m: any) => m.type === 'digital-credentials/web-wallet/response'))
        .toEqual([]);
    });

  test('if the opener navigates to another origin, the answer does not follow',
    async ({ page }) => {
      const wallet = await openAndSignIn(page);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await page.goto(`${ELSEWHERE}/`);
      await wallet.locator('#wallet-items input[value=share]').first().check();
      await wallet.locator('#wallet-send').click();
      await wallet.waitForTimeout(1000);
      expect(await page.evaluate(() => (window as any).__msgs)).toEqual([]);
    });

  test('the request and the answer never reach the CareAgents server log',
    async ({ page }) => {
      const before = fs.readFileSync(LOG, 'utf8').length;
      const wallet = await openAndSignIn(page);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await wallet.locator('#wallet-items input[value=share]').nth(0).check();
      await wallet.locator('#wallet-send').click();
      await result(page).toMatchObject({ status: 'completed' });
      const after = fs.readFileSync(LOG, 'utf8').slice(before);
      expect(after).not.toContain(CANARY);
      expect(after).not.toContain('Testpatient');
      expect(after).not.toContain('FAKE-');
      expect(after).not.toMatch(/"POST \/checkin/);
      // The relay was asked for the requested kinds of record only: none of
      // the website's words and not its origin.
      expect(RELAY_ASKED).toEqual([{ resource_types:
        ['AllergyIntolerance', 'Coverage', 'Patient'] }]);
      expect(JSON.stringify(RELAY_ASKED)).not.toContain(CANARY);
      expect(JSON.stringify(RELAY_ASKED)).not.toContain(EVIL);
    });

  // Finding (#959/#960) was: HTEST lived only on the fixture's outer Bundle,
  // so the wallet tagged each Bundle it sent while it answered from the
  // fixture alone. It answers from the account's own records now (#960's
  // note: the real-record export drops the wallet's tag), so it adds none.
  // Open, engine lane: the engine's export rebuilds `meta` and drops a
  // record's own HTEST tag too, so uploaded synthetic records reach a
  // website unmarked. This stub serves the raw fixture, whose records keep
  // theirs, so it cannot speak to that and does not assert it.
  test('the wallet adds no test tag of its own', async ({ page }) => {
    const wallet = await openAndSignIn(page);
    await expect(wallet.locator('#wallet-review')).toBeVisible();
    for (const r of await wallet.locator('#wallet-items input[value=share]').all()) {
      await r.check();
    }
    await wallet.locator('#wallet-send').click();
    await result(page).toMatchObject({ status: 'completed' });
    const r = await page.evaluate(() => (window as any).__result);
    expect(r.json.artifacts.length).toBe(3);
    const isTest = (meta: any) => ((meta && meta.tag) || []).some(
      (t: any) => t.code === 'HTEST');
    for (const a of r.json.artifacts) {
      expect(isTest(a.value.meta), `artifact ${a.id} adds no tag`).toBe(false);
    }
  });
});

// The fixture's own HTEST tagging: tests/test_sec_960_checkin_fixture.py.

test.describe('sec #960: "no known allergy" is never sent', () => {
  async function allergyStatus(page: Page, code: unknown) {
    const nka = structuredClone(FIXTURE);
    nka.entry.find((e: any) => e.resource.resourceType === 'AllergyIntolerance')
      .resource.code = code;
    return page.evaluate(async ({ bundle }) => {
      const m = await import('/static/checkin_answer.js');
      const request = { type: 'smart-health-checkin-request', version: '1', id: 'x',
        items: [{ id: 'allergies', title: 'a', accept: ['application/fhir+json'],
          content: { kind: 'selection.fhir', profiles: [
            'http://hl7.org/fhir/us/core/StructureDefinition/us-core-allergyintolerance'] } }] };
      const matches = m.matchItems(request, [], m.prepareRecords(bundle));
      return m.buildResponse(request, matches, { allergies: true })
        .requestStatus[0].status;
    }, { bundle: nka });
  }

  test.beforeEach(async ({ page }) => { await page.goto(`${CARE_BASE_URL}/`); });

  test('SNOMED by OID is filtered', async ({ page }) => {
    expect(await allergyStatus(page, { coding: [{
      system: 'urn:oid:2.16.840.1.113883.6.96', code: '716186003' }] }))
      .toBe('unavailable');
  });

  // Was a finding (low): only *coded* SNOMED NKA was filtered. The rule is
  // an allow-list now, so text or a local code is never shared.
  test('an uncoded "No known allergies" is not sent', async ({ page }) => {
    expect(await allergyStatus(page, { text: 'No known allergies' }))
      .toBe('unavailable');
  });

  test('an NKDA with only a local code and display is not sent', async ({ page }) => {
    expect(await allergyStatus(page, { coding: [{
      system: 'urn:oid:1.2.3.4.5', code: 'NKDA',
      display: 'No known drug allergies' }] })).toBe('unavailable');
  });
});
