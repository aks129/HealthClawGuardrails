import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';
import { blockThirdParty } from './careagents-fixtures';

/**
 * The /beta form's four answers in a browser at 375px (#979 QA). Only a
 * send that happened is green and says "We sent"; every other answer is
 * the warning style, says what happened, and offers "Get started".
 *
 * The harness has no mail provider, so a real submit is refused (502):
 * that one goes to the server. The other three answers are given by the
 * route, the way the server gives them, to drive the real page.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
};

test.use(PHONE);

async function fill(page: Page) {
  await page.goto(`${CARE_BASE_URL}/beta`);
  await page.fill('#beta-first', 'Robin');
  await page.fill('#beta-email', `robin${Date.now()}@example.test`);
  await page.check('#beta-consent');
}

async function answer(page: Page, status: number, body: object) {
  await page.route('**/beta', (route) =>
    route.request().method() === 'POST'
      ? route.fulfill({ status, contentType: 'application/json',
                        body: JSON.stringify(body) })
      : route.continue());
}

test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

test('a refused send says so in the warning style', async ({ page }) => {
  await fill(page);
  await page.click('#beta-form button[type=submit]');
  const done = page.locator('#beta-done');
  await expect(done).toBeVisible();
  await expect(page.locator('#beta-done-text')).toContainText(
    "We couldn't send the email");
  await expect(done).toHaveClass(/form-warn/);
  await expect(done).not.toHaveClass(/form-ok/);
  await expect(page.locator('#beta-again')).toBeVisible();
  await expect(page.locator('#beta-get-started')).toHaveAttribute(
    'href', '/auth');
});

test('a send that happened is green', async ({ page }) => {
  await answer(page, 200, { ok: true, sent: true });
  await fill(page);
  await page.click('#beta-form button[type=submit]');
  await expect(page.locator('#beta-done-text')).toContainText(
    'We sent an email to');
  await expect(page.locator('#beta-done')).toHaveClass(/form-ok/);
  await expect(page.locator('#beta-again')).toBeHidden();
});

test('a wait says when, and Send again comes after it', async ({ page }) => {
  await page.clock.install();
  await answer(page, 200, { sent: false, retry_after: 30 });
  await fill(page);
  await page.click('#beta-form button[type=submit]');
  await expect(page.locator('#beta-done-text')).toContainText(
    'ask for a new one in 30 seconds');
  await expect(page.locator('#beta-done')).toHaveClass(/form-warn/);
  await expect(page.locator('#beta-again')).toBeHidden();
  await page.clock.runFor(30_000);
  await expect(page.locator('#beta-again')).toBeVisible();
});

test('an unconfirmed send says we do not know', async ({ page }) => {
  await answer(page, 202, { sent: null, retry_after: 60 });
  await fill(page);
  await page.click('#beta-form button[type=submit]');
  await expect(page.locator('#beta-done-text')).toContainText(
    "We couldn't confirm the email reached");
  await expect(page.locator('#beta-done')).toHaveClass(/form-warn/);
  const w = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(w).toBeLessThanOrEqual(375);
});
