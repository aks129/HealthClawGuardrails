/**
 * The sample new-patient form (#1073), on a phone: a first-timer goes from
 * the hub to a made-up clinic's form, lets the check-in tab answer it, and
 * sees the answers come back on the form's own page; or shares nothing,
 * and the form says it is still blank. No outside site is involved: the
 * form is served by CareAgents and asks with the vendored Verifier library,
 * as any check-in website does.
 *
 * This suite's CareAgents has no engine behind it (HEALTHCLAW_BASE is a
 * dead port), so the sample tap cannot seed made-up records and the
 * records relay is answered here, by route, with the synthetic test
 * patient's export, as careagents-checkin-wallet.spec.ts does. What this
 * proves is the page, the hand-off and the answer coming back to its tab;
 * the relay on the real engine is tests/test_checkin_relay.py.
 */

import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { signIn, uniqueEmail } from './careagents-fixtures';

test.use({
  baseURL: CARE_BASE_URL,
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
  // Its own sign-in code budget (see careagents-checkin-wallet.spec.ts).
  extraHTTPHeaders: { 'X-Real-IP': '203.0.113.73' },
});

const EXPORT = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'checkin-export-test-patient.json'), 'utf8');

/** From the hub to the sample form, then its tab for answering. */
async function openForm(page: Page): Promise<Page> {
  const asked: unknown[] = [];
  await page.context().route(`${CARE_BASE_URL}/checkin/wallet/records`,
    (route) => {
      asked.push(route.request().postDataJSON());
      return route.fulfill({ status: 200, headers: { 'Cache-Control': 'no-store' },
        contentType: 'application/fhir+json', body: EXPORT });
    });
  await signIn(page, uniqueEmail(), CARE_LOG);
  await page.getByRole('link', { name: 'Try a sample new-patient form' }).click();
  await expect(page).toHaveURL(`${CARE_BASE_URL}/checkin/sample`);
  await expect(page.locator('.sample-note')).toContainText('made-up clinic');
  const [tab] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#sample-start').click()]);
  await expect(tab.locator('#wallet-review')).toBeVisible();
  // The wallet asked for the kinds of record the form names, nothing else.
  expect(asked).toEqual([{ resource_types:
    ['AllergyIntolerance', 'Condition', 'MedicationRequest'] }]);
  // Every item starts unshared.
  const choices = tab.locator('#wallet-items .checkin-choice');
  await expect(choices).toHaveCount(4);
  await expect(tab.locator('#wallet-items input[value=share]:checked')).toHaveCount(0);
  // The free-text question is shown as one it does not answer.
  await expect(tab.locator('#wallet-items li[data-item=intake]'))
    .toContainText('What brings you in today?');
  return tab;
}

test.describe('sample new-patient form at 375px', () => {
  // Only the app under test, for both tabs: blockThirdParty, set on the
  // context, and falling back rather than continuing, so the relay's route
  // (added later, so tried first) still answers in the second tab.
  test.beforeEach(async ({ page }) => {
    await page.context().route('**/*', (route) => {
      const url = route.request().url();
      if (!url.startsWith('http')) return route.fallback();
      const host = new URL(url).hostname;
      return host === 'localhost' || host === '127.0.0.1'
        ? route.fallback() : route.abort();
    });
  });

  test('the hub opens it, the records answer it, the answers come back',
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
    await expect(page.locator('#sample-result-text')).toContainText('still blank');
    await expect(page.locator('#sample-answers > li')).toHaveCount(0);
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

  test('a script that cannot load says the form could not start', async ({ page }) => {
    await page.route('**/static/checkin_answer.js*',
      (route) => route.fulfill({ status: 404, body: '' }));
    await page.goto('/checkin/sample');
    await expect(page.locator('#sample-stalled')).toBeVisible();
    await expect(page.locator('#sample-start')).toBeDisabled();
  });
});
