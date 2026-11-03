/**
 * The guest wallet, /checkin/try-wallet (CARE_CHECKIN_GUEST_WALLET), in a
 * real browser, opened by a check-in page on another origin as a Verifier
 * team would add it. What only a browser shows:
 *
 *  - It never asks for a sign-in, and never calls the records relay, the
 *    test-patient endpoint or a sign-in endpoint: any of those fails the
 *    test. Its records are the synthetic test patient's file.
 *  - The review says the records are made up, and every item starts at
 *    "Don't share".
 *  - What is sent is the test patient's records and nothing else.
 *
 * The page's routes and headers: tests/test_checkin_guest_wallet.py.
 */

import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL } from '../playwright.config';
import { blockThirdParty } from './careagents-fixtures';

test.use({ baseURL: CARE_BASE_URL });

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', '..', 'careagents', 'checkin',
    'us_core_test_patient.json'), 'utf8'));
const FIXTURE_IDS = new Set(FIXTURE.entry.map((e: any) =>
  `${e.resource.resourceType}/${e.resource.id}`));

const US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/';
const item = (id: string, profile: string) => ({
  id, title: id,
  content: { kind: 'selection.fhir', profiles: [US_CORE + profile] },
  accept: ['application/fhir+json'],
});
const REQUEST = {
  type: 'smart-health-checkin-request', version: '1', id: 'e2e-guest',
  fhirVersions: ['4.0.1'],
  items: [
    item('patient', 'us-core-patient'),
    item('problems', 'us-core-condition-problems-health-concerns'),
    item('allergies', 'us-core-allergyintolerance'),
    item('medications', 'us-core-medicationrequest'),
    item('immunizations', 'us-core-immunization'),
    item('coverage', 'us-core-coverage'),
  ],
};

const VERIFIER = 'http://127.0.0.1:5399';
const CHECKIN_JS = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'smart-health-checkin-client', '0.4.4', 'checkin.js'));
const VERIFIER_HTML = `<!doctype html><meta charset="utf-8"><title>verifier</title>
<button id="go">check in</button>
<script type="module">
import { runCheckin, webWallet } from '/checkin.js';
document.getElementById('go').onclick = () => {
  const wallet = webWallet({ id: 'e2e', name: 'e2e',
    walletUrl: ${JSON.stringify(`${CARE_BASE_URL}/checkin/try-wallet`)} });
  runCheckin(window.__req, { wallet }).then((r) => {
    window.__result = { status: r.status, json: r.response ? r.response.json : null,
      error: r.error ? r.error.message : null };
  }, (e) => { window.__result = { status: "threw", error: String(e) }; });
};
</script>`;

/** Anything the guest wallet must never call, recorded and refused. And
 *  every request the context makes, so "saw traffic" is told apart from
 *  "saw nothing forbidden". */
async function forbid(page: Page) {
  const hit: string[] = [];
  const seen: string[] = [];
  page.context().on('request', (r) => seen.push(r.url()));
  for (const p of ['/checkin/wallet/records', '/checkin/wallet/test-patient',
    '/webauthn/**', '/auth/**', '/api/**']) {
    await page.context().route(`${CARE_BASE_URL}${p}`, (route) => {
      hit.push(route.request().url());
      return route.abort();
    });
  }
  return { hit, seen };
}

/** The guard can see a call: one forbidden fetch from a CareAgents page in
 *  the same context (the wallet tab has closed by now) lands in `hit`. */
async function positiveControl(page: Page, hit: string[], seen: string[]) {
  expect(seen.some((u) => u.includes('/static/checkin_wallet.js'))).toBe(true);
  const probe = await page.context().newPage();
  await probe.goto(`${CARE_BASE_URL}/checkin/try-wallet`);
  await probe.evaluate(() => fetch('/checkin/wallet/records',
    { method: 'POST' }).catch(() => null));
  expect(hit).toHaveLength(1);
  expect(hit[0]).toBe(`${CARE_BASE_URL}/checkin/wallet/records`);
}

async function openGuest(page: Page) {
  await page.route(`${VERIFIER}/**`, (route) => {
    const url = new URL(route.request().url());
    return url.pathname === '/checkin.js'
      ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
      : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML });
  });
  await page.goto(`${VERIFIER}/`);
  await page.evaluate((r) => { (window as any).__req = r; }, REQUEST);
  const [wallet] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#go').click()]);
  return wallet;
}

test.describe('guest check-in wallet', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  test('no sign-in, made up, and every item starts at "Don\'t share"',
    async ({ page }) => {
      const { hit, seen } = await forbid(page);
      const wallet = await openGuest(page);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await expect(wallet.locator('#wallet-signin')).toBeHidden();
      await expect(wallet.locator('#wallet-made-up')).toBeVisible();
      await expect(wallet.locator('#wallet-note')).toHaveText(
        "This answers from a made-up patient's records, not yours. You choose what the website gets.");
      const checked = wallet.locator('#wallet-items input[type=radio]:checked');
      await expect(checked).toHaveCount(6);
      expect(await checked.evaluateAll((els) =>
        els.map((e) => (e as HTMLInputElement).value)))
        .toEqual(Array(6).fill('decline'));
      await wallet.locator('#wallet-send').click();
      await expect.poll(() => page.evaluate(() => (window as any).__result),
        { timeout: 15_000 }).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(hit).toEqual([]);
      await positiveControl(page, hit, seen);
    });

  test('what is sent is the test patient\'s records only', async ({ page }) => {
    const { hit, seen } = await forbid(page);
    const wallet = await openGuest(page);
    await expect(wallet.locator('#wallet-review')).toBeVisible();
    for (const id of REQUEST.items.map((i) => i.id)) {
      const share = wallet.locator(
        `#wallet-items li[data-item=${id}] input[value=share]`);
      if (await share.count()) await share.check();
    }
    await wallet.locator('#wallet-send').click();
    await expect.poll(() => page.evaluate(() => (window as any).__result),
      { timeout: 15_000 }).toMatchObject({ status: 'completed' });
    const r = await page.evaluate(() => (window as any).__result);
    const sent = r.json.artifacts.flatMap((a: any) => a.value.entry.map(
      (e: any) => `${e.resource.resourceType}/${e.resource.id}`));
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.filter((k: string) => !FIXTURE_IDS.has(k))).toEqual([]);
    expect(sent).toContain('Patient/checkin-test-patient');
    expect(hit).toEqual([]);
    await positiveControl(page, hit, seen);
  });

  test('unreadable guest records reach the usual can\'t-share answer',
    async ({ page }) => {
      // The page as served, with its inlined records broken.
      await page.context().route(`${CARE_BASE_URL}/checkin/try-wallet`,
        async (route) => {
          const res = await route.fetch();
          const body = (await res.text()).replace(
            /(<script type="application\/json" id="wallet-guest-records">)[\s\S]*?(<\/script>)/,
            '$1{$2');
          return route.fulfill({ response: res, body });
        });
      const wallet = await openGuest(page);
      await expect(wallet.locator('#wallet-done-text'))
        .toContainText("couldn't read your records just now");
      await wallet.locator('#wallet-ok').click();
      await expect.poll(() => page.evaluate(() => (window as any).__result),
        { timeout: 15_000 }).toBeDefined();
      expect(JSON.stringify(await page.evaluate(() => (window as any).__result)))
        .toContain("couldn't share records this time");
    });
});
