/**
 * The check-in wallet, for an account with more than one set of its own
 * records (careagents/checkin_wallet.py, static/checkin_wallet.js).
 *
 * Seen live 2026-10-26: such an account was told this test version answers
 * from only one set, and could not check in. Now the relay refuses with the
 * sets it may answer from, each by connection label and provider, the
 * person picks one, and the wallet asks again naming that connection.
 *
 * What only a browser shows: the picker at phone width, its words set as
 * text, the second relay call carrying the types and the picked id and
 * nothing else, and "Don't share anything" answering the website. The relay
 * is answered here by route, as in careagents-checkin-wallet.spec.ts; that
 * the server re-derives the tenant from the signed-in account and refuses
 * another account's id is tests/test_checkin_wallet_pick.py.
 *
 * Synthetic labels and providers only.
 */

import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, codeFromLog, uniqueEmail } from './careagents-fixtures';

test.use({ baseURL: CARE_BASE_URL });
// Its own sign-in code budget (TEST-NET-2, RFC 5737), as the other wallet
// specs have theirs.
test.use({ extraHTTPHeaders: { 'X-Real-IP': '198.51.100.61' } });

const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));

const US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/';
const ITEMS = [
  ['patient', 'us-core-patient'],
  ['allergies', 'us-core-allergyintolerance'],
  ['coverage', 'us-core-coverage'],
].map(([id, profile]) => ({
  id, title: id,
  content: { kind: 'selection.fhir', profiles: [US_CORE + profile] },
  accept: ['application/fhir+json'],
}));

const LONG = 'Records from the synthetic clinic with a very long name that '
  + 'keeps going past the width of a phone screen';
const CHOICES = [
  { id: 'conn_e2epicka', label: 'My records', provider: null },
  { id: 'conn_e2epickb', label: 'My records', provider: null },
  { id: 'conn_e2epickc', label: '<b>Portal</b>', provider: 'Synthetic Hospital C' },
  { id: 'conn_e2epickd', label: LONG, provider: 'Synthetic Clinic D' },
];

const VERIFIER = 'http://127.0.0.1:5398';
const CHECKIN_JS = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'smart-health-checkin-client', '0.4.4', 'checkin.js'));
const VERIFIER_HTML = `<!doctype html><meta charset="utf-8"><title>verifier</title>
<button id="go">check in</button>
<script type="module">
import { runCheckin, webWallet } from '/checkin.js';
document.getElementById('go').onclick = () => {
  const wallet = webWallet({ id: 'e2e', name: 'e2e',
    walletUrl: ${JSON.stringify(`${CARE_BASE_URL}/checkin/wallet`)} });
  runCheckin(window.__req, { wallet }).then((r) => {
    window.__result = { status: r.status, json: r.response ? r.response.json : null,
      error: r.error ? r.error.message : null };
  }, (e) => { window.__result = { status: "threw", error: String(e) }; });
};
</script>`;

/** Answer the relay: no id -> the sets to pick from; an id -> `picked`
 *  (the export, or a refusal code). Keep each body the wallet sent. */
async function serveRelay(page: Page, picked: string | null = null) {
  const asked: any[] = [];
  await page.context().route(`${CARE_BASE_URL}/checkin/wallet/records`,
    (route) => {
      const body = route.request().postDataJSON();
      asked.push(body);
      const json = (status: number, b: unknown) => route.fulfill({ status,
        headers: { 'Cache-Control': 'no-store' },
        contentType: status === 200 ? 'application/fhir+json' : 'application/json',
        body: JSON.stringify(b) });
      if (!('connection_id' in body))
        return json(409, { error: 'several_records', choices: CHOICES });
      return picked ? json(409, { error: picked }) : json(200, EXPORT);
    });
  return asked;
}

async function checkIn(page: Page) {
  await page.route(`${VERIFIER}/**`, (route) => {
    const url = new URL(route.request().url());
    return url.pathname === '/checkin.js'
      ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
      : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML });
  });
  await page.goto(`${VERIFIER}/`);
  await page.evaluate((items) => { (window as any).__req = { items }; }, ITEMS);
  const [wallet] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#go').click()]);
  await wallet.setViewportSize({ width: 375, height: 812 });
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
  await wallet.locator('#signin-verify').click();
  return wallet;
}

const result = (page: Page) => page.evaluate(() => (window as any).__result);

test.describe('check-in wallet, more than one set of records', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  test('at 375px the person picks a set, then reviews as usual',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const wallet = await checkIn(page);
      const pick = wallet.locator('#wallet-pick');
      await expect(pick).toBeVisible();
      const buttons = wallet.locator('#wallet-pick-list button');
      await expect(buttons).toHaveCount(4);
      // Label then provider, as text; the same words twice are numbered.
      await expect(buttons.nth(0)).toHaveText('My records');
      await expect(buttons.nth(1)).toHaveText('My records (2)');
      await expect(buttons.nth(2)).toHaveText('<b>Portal</b>Synthetic Hospital C');
      await expect(buttons.nth(2).locator('b')).toHaveCount(0);
      await expect(buttons.nth(3)).toContainText('Synthetic Clinic D');
      // Nothing runs off the side of a phone, the long label included.
      expect(await wallet.evaluate(() =>
        document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
      const box = await buttons.nth(3).boundingBox();
      expect(box!.x + box!.width).toBeLessThanOrEqual(375);
      expect(box!.height).toBeGreaterThanOrEqual(44);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'pick-375.png') });
      }
      // Nothing has been answered while the person chooses.
      expect(await result(page)).toBeUndefined();

      await buttons.nth(2).click();
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await expect(pick).toBeHidden();
      // The second call names the picked connection and nothing else new:
      // not the website, not a tenant.
      expect(asked).toHaveLength(2);
      expect(Object.keys(asked[0]).sort()).toEqual(['resource_types']);
      expect(Object.keys(asked[1]).sort())
        .toEqual(['connection_id', 'resource_types']);
      expect(asked[1].connection_id).toBe('conn_e2epickc');
      expect(asked[1].resource_types).toEqual(asked[0].resource_types);
      // Per-item consent is unchanged: every item starts at "Don't share".
      const checked = wallet.locator('#wallet-items input[type=radio]:checked');
      expect(await checked.evaluateAll((els) =>
        els.map((e) => (e as HTMLInputElement).value)))
        .toEqual(Array(await checked.count()).fill('decline'));
      expect(await wallet.evaluate(() =>
        document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
      await wallet.locator('#wallet-send').click();
      await expect.poll(() => result(page), { timeout: 15_000 })
        .toMatchObject({ status: 'completed' });
      expect((await result(page)).json.artifacts).toEqual([]);
    });

  test('"Don\'t share anything" on the picker shares nothing',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const wallet = await checkIn(page);
      await expect(wallet.locator('#wallet-pick')).toBeVisible();
      await wallet.locator('#wallet-pick-no').click();
      await expect.poll(() => result(page), { timeout: 15_000 })
        .toMatchObject({ status: 'completed' });
      const r = await result(page);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.map((s: any) => s.status))
        .toEqual(Array(ITEMS.length).fill('declined'));
      expect(asked).toHaveLength(1);
    });

  test('a set gone by the time it is picked says so and waits for OK',
    async ({ page }) => {
      await serveRelay(page, 'unknown_records');
      const wallet = await checkIn(page);
      await wallet.locator('#wallet-pick-list button').first().click();
      await expect(wallet.locator('#wallet-done-text'))
        .toContainText("isn't available any more");
      await wallet.waitForTimeout(500);
      expect(await result(page)).toBeUndefined();
      await wallet.locator('#wallet-ok').click();
      await expect.poll(() => result(page), { timeout: 15_000 }).toBeDefined();
      const r = JSON.stringify(await result(page));
      expect(r).toContain("couldn't share records this time");
      expect(r).not.toContain('unknown_records');
    });
});
