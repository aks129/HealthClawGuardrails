/**
 * The sample new-patient form (#1073, #1077), on a phone: a first-timer
 * goes from the hub to a made-up clinic's form, opens the answering tab,
 * is offered a made-up patient's records in plain words (a new account has
 * only the sample, which check-in cannot use), and sees the answers come
 * back on the form's own page; or shares nothing, and the form says it is
 * still blank. No outside site is involved: the form is served by
 * CareAgents and asks with the vendored Verifier library, as any check-in
 * website does.
 *
 * The journey runs on the second CareAgents server (CARE_ALLOW_*), which
 * has the test patient on; the first has it off, and there the hub does
 * not link to a form the account could not finish.
 *
 * Neither server has an engine behind it (HEALTHCLAW_BASE is a dead port),
 * so the made-up patient cannot really be seeded and the records relay is
 * answered here, by route: first "only the sample" (as the engine answers
 * a new account), then, once the made-up patient is chosen, the synthetic
 * test patient's export. What this proves is the pages, the hand-off, the
 * words and the answer coming back to its tab; the relay and the seed on
 * the real engine are tests/test_checkin_relay.py and
 * tests/test_checkin_wallet_test_patient.py.
 */

import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import {
  CARE_ALLOW_BASE_URL, CARE_ALLOW_LOG, CARE_BASE_URL, CARE_LOG,
} from '../playwright.config';
import { signIn, uniqueEmail } from './careagents-fixtures';

test.use({
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
  // Its own sign-in code budget (see careagents-checkin-wallet.spec.ts).
  extraHTTPHeaders: { 'X-Real-IP': '203.0.113.73' },
});

const EXPORT = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'checkin-export-test-patient.json'), 'utf8');
const LINK = 'Try a sample new-patient form';

// Only the app under test, for both tabs: blockThirdParty, set on the
// context, and falling back rather than continuing, so the routes added
// later (tried first) still answer in the second tab.
test.beforeEach(async ({ page }) => {
  await page.context().route('**/*', (route) => {
    const url = route.request().url();
    if (!url.startsWith('http')) return route.fallback();
    const host = new URL(url).hostname;
    return host === 'localhost' || host === '127.0.0.1'
      ? route.fallback() : route.abort();
  });
});

/** From the hub to the sample form, through the made-up patient offer,
 *  to the answering tab's review. */
async function openForm(page: Page): Promise<Page> {
  const base = CARE_ALLOW_BASE_URL;
  const asked: unknown[] = [];
  let chosen = false;
  await page.context().route(`${base}/checkin/wallet/records`, (route) => {
    asked.push(route.request().postDataJSON());
    return chosen
      ? route.fulfill({ status: 200, headers: { 'Cache-Control': 'no-store' },
        contentType: 'application/fhir+json', body: EXPORT })
      : route.fulfill({ status: 409, contentType: 'application/json',
        body: '{"error":"sample_only"}' });
  });
  await page.context().route(`${base}/checkin/wallet/test-patient`, (route) => {
    chosen = true;
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: '{"ready":true}' });
  });
  await signIn(page, uniqueEmail(), CARE_ALLOW_LOG);
  await page.getByRole('link', { name: LINK }).click();
  await expect(page).toHaveURL(`${base}/checkin/sample`);
  await expect(page.locator('.sample-note')).toContainText('made-up clinic');
  const [tab] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#sample-start').click()]);

  // Plain words for the made-up patient (#1077).
  const offer = tab.locator('#wallet-test-patient');
  await expect(offer).toBeVisible();
  const words = (await offer.innerText()).toLowerCase();
  for (const w of ['connectathon', 'test patient', 'us core']) {
    expect(words).not.toContain(w);
  }
  await tab.getByRole('button', { name: "Use a made-up patient's records to try this" }).click();

  await expect(tab.locator('#wallet-review')).toBeVisible();
  // The kinds of record the form names, nothing else, before and after.
  const types = { resource_types: ['AllergyIntolerance', 'Condition', 'MedicationRequest'] };
  expect(asked).toEqual([types, types]);
  // Every item starts unshared.
  await expect(tab.locator('#wallet-items .checkin-choice')).toHaveCount(4);
  await expect(tab.locator('#wallet-items input[value=share]:checked')).toHaveCount(0);
  // The free-text question is shown as one it does not answer, and the
  // line says it is about the question below it.
  const intake = tab.locator('#wallet-items li[data-item=intake]');
  await expect(intake).toContainText('What brings you in today?');
  await expect(intake.locator('.checkin-skipped-why'))
    .toHaveText("This page can't answer the next question. Nothing is sent for it.");
  return tab;
}

test.describe('sample new-patient form at 375px', () => {
  test.use({ baseURL: CARE_ALLOW_BASE_URL });

  test('the hub opens it, made-up records answer it, the answers come back',
    async ({ page }) => {
      const tab = await openForm(page);
      for (const id of ['medications', 'allergies', 'problems', 'intake']) {
        await tab.locator(`#wallet-items li[data-item=${id}] input[value=share]`).check();
      }
      await tab.locator('#wallet-items li[data-item=intake]').getByLabel('Yes').check();
      await tab.locator('#wallet-send').click();

      await expect(page.locator('#sample-result-title')).toHaveText('Your form, filled in');
      const answers = page.locator('#sample-answers > li');
      await expect(answers.nth(0)).toContainText('Lisinopril 10 mg');
      await expect(answers.nth(1)).toContainText('Penicillin');
      await expect(answers.nth(2)).toContainText('Hypertensive disorder');
      await expect(answers.nth(3)).toContainText('Is this your first visit to our clinic?Yes');
      await expect(answers.nth(4)).toContainText("You'd tell the clinic yourself");
      // Nothing scrolls sideways on a phone.
      expect(await page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });

  test('sharing nothing leaves the form blank', async ({ page }) => {
    const tab = await openForm(page);
    await tab.locator('#wallet-decline-all').click();
    await expect(page.locator('#sample-result-title')).toHaveText('Nothing was shared');
    await expect(page.locator('#sample-result-text')).toHaveText(
      'The answer said no to every item. The form is still blank.');
    await expect(page.locator('#sample-answers > li')).toHaveCount(0);
  });
});

test.describe('sample new-patient form, test patient off', () => {
  test.use({ baseURL: CARE_BASE_URL });

  test('a new account is not sent to a form it cannot finish', async ({ page }) => {
    await signIn(page, uniqueEmail(), CARE_LOG);
    await expect(page.locator('#waiting')).toBeVisible();
    await expect(page.getByRole('link', { name: LINK })).toHaveCount(0);
  });

  // The failure paths say so on the page rather than leaving "Waiting…" or
  // a dead button. The library is stood in for here, so no tab opens.
  test('an answer it cannot read says no answers came back', async ({ page }) => {
    await page.route('**/static/vendor/smart-health-checkin-client/0.4.4/checkin.js*',
      (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: `
        export const webWallet = () => ({});
        // Completed, but its entries carry no resource: drawing it throws.
        export const runCheckin = async () => ({ status: 'completed', response: {
          status: () => undefined, entries: () => [{}], form: () => undefined } });` }));
    await page.goto('/checkin/sample');
    await page.locator('#sample-start').click();
    await expect(page.locator('#sample-result-title')).toHaveText('No answers came back');
    await expect(page.locator('#sample-waiting')).toBeHidden();
  });

  test('a naming module that cannot load says the form could not start',
    async ({ page }) => {
      await page.route('**/static/checkin_answer.js*',
        (route) => route.fulfill({ status: 404, body: '' }));
      await page.goto('/checkin/sample');
      await expect(page.locator('#sample-stalled')).toBeVisible();
      await expect(page.locator('#sample-start')).toBeDisabled();
    });

  // The library itself missing: the module never runs, so the page's own
  // timer says so (8 s).
  test('a library that cannot load says the form could not start',
    async ({ page }) => {
      await page.route('**/static/vendor/smart-health-checkin-client/0.4.4/checkin.js*',
        (route) => route.fulfill({ status: 404, body: '' }));
      await page.goto('/checkin/sample');
      await expect(page.locator('#sample-stalled')).toBeVisible({ timeout: 12_000 });
      await expect(page.locator('#sample-start')).toBeDisabled();
    });
});
