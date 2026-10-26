/**
 * The check-in wallet, for an account with more than one set of its own
 * records (careagents/checkin_wallet.py, static/checkin_wallet.js).
 *
 * Seen live 2026-10-26: such an account was told this test version answers
 * from only one set, and could not check in. Now the relay refuses with the
 * sets it may answer from, each by connection label, how it was connected
 * and when; the person picks one, and the wallet asks again naming that
 * connection. The review says which set it uses and leads back to the
 * picker; a set removed before the tap is dropped and the rest offered.
 *
 * What only a browser shows: the picker at phone width, who asked, its
 * words set as text, focus moving with each screen, the relay calls
 * carrying the types and the picked id and nothing else, and each way out
 * answering the website. The relay is answered here by route, as in
 * careagents-checkin-wallet.spec.ts; that the server re-derives the tenant
 * from the signed-in account and refuses another account's id is
 * tests/test_checkin_wallet_pick.py.
 *
 * Synthetic labels only.
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
// The date is shown in the browser's time zone; pin it so the words are
// the same on any runner.
test.use({ timezoneId: 'America/New_York' });

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

// This year's dates read without a year; last year's carry one.
const YEAR = new Date().getFullYear();
const DAY = (md: string, year = YEAR) =>
  Date.parse(`${year}-${md}T12:00:00-04:00`) / 1000;
const LONG = 'Records from the synthetic clinic with a very long name that '
  + 'keeps going past the width of a phone screen';
const FASTEN = 'Records from your doctor';
const PORTAL = 'From a patient portal';
const CHOICES = [
  // Two portals, different days: told apart by the date alone.
  { id: 'conn_e2epicka', label: FASTEN, how: PORTAL, added: DAY('01-12') },
  { id: 'conn_e2epickb', label: FASTEN, how: PORTAL, added: DAY('01-20') },
  // A third the same day as the second: numbered as well.
  { id: 'conn_e2epicke', label: FASTEN, how: PORTAL, added: DAY('01-20') },
  { id: 'conn_e2epickc', label: '<b>Portal</b>', how: 'Uploaded file',
    added: DAY('09-01', YEAR - 1) },
  { id: 'conn_e2epickd', label: LONG, how: 'Uploaded file', added: DAY('02-02') },
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

/** Answer the relay: no id -> the sets to pick from; an id in `removed`
 *  -> `unknown_records`, as the server says for a set gone since it was
 *  listed; any other id -> the export. Keep each body the wallet sent. */
async function serveRelay(page: Page, removed: string[] = []) {
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
      return removed.includes(body.connection_id)
        ? json(409, { error: 'unknown_records' }) : json(200, EXPORT);
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
const focused = (wallet: Page) => wallet.evaluate(() => document.activeElement?.id);
const noSideScroll = async (wallet: Page) => expect(await wallet.evaluate(() =>
  document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
/** Every item's share choice, as its radio value. */
const choicesOn = (wallet: Page) => wallet.locator(
  '#wallet-items input[type=radio]:checked').evaluateAll((els) =>
  els.map((e) => (e as HTMLInputElement).value));

test.describe('check-in wallet, more than one set of records', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  test('at 375px: who asked, pick a set, see it in use, go back, pick again',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const wallet = await checkIn(page);
      const pick = wallet.locator('#wallet-pick');
      await expect(pick).toBeVisible();
      // Who asked, by the address the browser reported, set apart as text.
      const title = wallet.locator('#wallet-pick-title');
      await expect(title).toHaveText(`⁨${VERIFIER}⁩ asked for your records.`);
      await expect(pick).toContainText(
        "You've connected records from more than one place. Which should it see?");
      expect(await focused(wallet)).toBe('wallet-pick-title');

      const sets = wallet.locator('#wallet-pick-list .checkin-set');
      await expect(sets).toHaveCount(5);
      // Label, then how and when, as text. This year's dates carry no year;
      // two portal sets read apart by date; only lines still alike are
      // numbered.
      await expect(sets.nth(0)).toHaveText(`${FASTEN}${PORTAL} · Added Jan 12`);
      await expect(sets.nth(1)).toHaveText(`${FASTEN}${PORTAL} · Added Jan 20`);
      await expect(sets.nth(2)).toHaveText(`${FASTEN} (2)${PORTAL} · Added Jan 20`);
      await expect(sets.nth(3)).toHaveText(
        `<b>Portal</b>Uploaded file · Added Sep 1, ${YEAR - 1}`);
      await expect(sets.nth(3).locator('b')).toHaveCount(0);
      await expect(sets.nth(4)).toContainText('Uploaded file · Added Feb 2');
      const words = await sets.allInnerTexts();
      expect(new Set(words).size).toBe(words.length);
      // The how-and-when line fits one line at phone width.
      for (let i = 0; i < 5; i += 1) {
        const line = await sets.nth(i).locator('.checkin-pick-source').boundingBox();
        expect(line!.height, `set ${i}`).toBeLessThan(30);
      }
      // The ways out are not record cards: "Don't share" is a secondary
      // button, and a plain close is there as on the review.
      await expect(wallet.locator('#wallet-pick-no')).toHaveClass(/btn-secondary/);
      await expect(wallet.locator('#wallet-pick-no')).not.toHaveClass(/checkin-set/);
      await expect(wallet.locator('#wallet-pick-close'))
        .toHaveText('Close without answering');
      await noSideScroll(wallet);
      const box = await sets.nth(4).boundingBox();
      expect(box!.x + box!.width).toBeLessThanOrEqual(375);
      expect(box!.height).toBeGreaterThanOrEqual(44);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'pick-375.png') });
      }
      expect(await result(page)).toBeUndefined();

      // Pick: the review says which set it uses, and takes focus.
      await sets.nth(3).click();
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await expect(pick).toBeHidden();
      expect(await focused(wallet)).toBe('wallet-review-title');
      await expect(wallet.locator('#wallet-using')).toHaveText(
        `Using: <b>Portal</b> · Uploaded file · Added Sep 1, ${YEAR - 1}`);
      await expect(wallet.locator('#wallet-repick'))
        .toHaveText('Choose different records');
      expect(await choicesOn(wallet)).toEqual(['decline', 'decline', 'decline']);
      // Heading, the address, the Using line with the way back right under
      // it and aligned with it, then the instruction.
      const tops = await wallet.evaluate(() => {
        const sec = document.getElementById('wallet-review')!;
        const say = [...sec.querySelectorAll('p.setup-sub')].find((p) =>
          p.textContent!.includes('Choose what to share'))!;
        return [sec.querySelector('h1')!, document.getElementById('wallet-origin')!,
          document.getElementById('wallet-using')!,
          document.getElementById('wallet-repick')!, say]
          .map((e) => e.getBoundingClientRect())
          .map((r) => ({ top: r.top, left: r.left }));
      });
      for (let i = 1; i < tops.length; i += 1) {
        expect(tops[i].top, `row ${i}`).toBeGreaterThan(tops[i - 1].top);
      }
      expect(tops[3].left).toBe(tops[2].left);
      await expect(wallet.locator('#wallet-repick')).toHaveCSS('text-align', 'left');
      await noSideScroll(wallet);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'pick-review-375.png') });
      }

      // Choose something, then go back: the picker again, with focus.
      await wallet.locator('#wallet-items input[value=share]').first().check();
      await wallet.locator('#wallet-repick').click();
      await expect(pick).toBeVisible();
      expect(await focused(wallet)).toBe('wallet-pick-title');
      await expect(wallet.locator('#wallet-pick-note')).toBeHidden();

      // Another set: named on the review exactly as tapped, its number
      // included, and every item at "Don't share".
      await sets.nth(2).click();
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      expect(await focused(wallet)).toBe('wallet-review-title');
      await expect(wallet.locator('#wallet-using')).toHaveText(
        `Using: ${FASTEN} (2) · ${PORTAL} · Added Jan 20`);
      expect(await choicesOn(wallet)).toEqual(['decline', 'decline', 'decline']);

      // Each relay call is the types and, after a pick, that id; nothing else.
      expect(asked).toHaveLength(3);
      expect(Object.keys(asked[0]).sort()).toEqual(['resource_types']);
      for (const [i, id] of [[1, 'conn_e2epickc'], [2, 'conn_e2epicke']] as const) {
        expect(Object.keys(asked[i]).sort())
          .toEqual(['connection_id', 'resource_types']);
        expect(asked[i].connection_id).toBe(id);
        expect(asked[i].resource_types).toEqual(asked[0].resource_types);
      }
      await wallet.locator('#wallet-send').click();
      await expect.poll(() => result(page), { timeout: 15_000 })
        .toMatchObject({ status: 'completed' });
      expect((await result(page)).json.artifacts).toEqual([]);
    });

  test('a set removed before the tap: the rest are offered again',
    async ({ page }) => {
      const asked = await serveRelay(page, ['conn_e2epickb']);
      const wallet = await checkIn(page);
      const sets = wallet.locator('#wallet-pick-list .checkin-set');
      await sets.nth(1).click();
      const note = wallet.locator('#wallet-pick-note');
      await expect(note).toHaveText("That one was removed. Choose from what's left.");
      // A calm notice, not an error, and still announced.
      await expect(note).toHaveAttribute('role', 'status');
      await expect(note).toHaveClass(/checkin-note/);
      await expect(note).not.toHaveClass(/form-error/);
      await expect(sets).toHaveCount(4);
      await expect(wallet.locator('#wallet-pick-list [data-connection=conn_e2epickb]'))
        .toHaveCount(0);
      expect(await focused(wallet)).toBe('wallet-pick-title');
      // The one left from that day is no longer numbered.
      await expect(sets.nth(1)).toHaveText(`${FASTEN}${PORTAL} · Added Jan 20`);
      await noSideScroll(wallet);
      expect(await result(page)).toBeUndefined();
      await sets.nth(1).click();
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      expect(asked.map((b) => b.connection_id))
        .toEqual([undefined, 'conn_e2epickb', 'conn_e2epicke']);
    });

  test('every set removed: the usual no-records reason, held for OK',
    async ({ page }) => {
      await serveRelay(page, CHOICES.map((c) => c.id));
      const wallet = await checkIn(page);
      const sets = wallet.locator('#wallet-pick-list .checkin-set');
      for (let left = 5; left > 0; left -= 1) {
        await expect(sets).toHaveCount(left);
        await sets.first().click();
      }
      await expect(wallet.locator('#wallet-done-text'))
        .toContainText('There are no records on your account yet.');
      await wallet.waitForTimeout(500);
      expect(await result(page)).toBeUndefined();
      await wallet.locator('#wallet-ok').click();
      await expect.poll(() => result(page), { timeout: 15_000 }).toBeDefined();
      const r = JSON.stringify(await result(page));
      expect(r).toContain("couldn't share records this time");
      expect(r).not.toContain('unknown_records');
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

  test('"Close without answering" on the picker sends nothing',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const wallet = await checkIn(page);
      await wallet.locator('#wallet-pick-close').click();
      await expect(wallet.locator('#wallet-done-title')).toHaveText('Closed');
      await expect.poll(() => result(page), { timeout: 15_000 }).toBeDefined();
      const r = await result(page);
      expect(r.status).not.toBe('completed');
      expect(asked).toHaveLength(1);
    });
});
