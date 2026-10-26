import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * The visit brief on a phone and on paper. A patient hands it to a
 * clinician, so it needs a Print button that works and a print layout with
 * none of the app's own controls in it.
 *
 * This harness pins the records service to a dead port, so the brief's
 * sections all read "could not reach your records". The layout and the
 * print rules do not depend on what the sections hold.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

// Its own address, so the sign-in limiter, which keys on X-Real-IP with no
// edge in front, never counts this spec against another (TEST-NET-3,
// RFC 5737).
test.use({ baseURL: CARE_BASE_URL, ...PHONE,
           extraHTTPHeaders: { 'X-Real-IP': '203.0.113.31' } });

async function openBrief(page: Page): Promise<void> {
  await page.goto('/home');
  const version = await page.locator('[data-consent-version]')
    .first().getAttribute('data-consent-version');
  const conn = await page.request.post('/api/connections/direct', {
    data: { consent: true, consent_version: version } });
  expect(conn.ok(), await conn.text()).toBeTruthy();
  const agent = await page.request.post('/api/agents', {
    data: { name: 'Ada', persona: 'direct',
            connection_id: (await conn.json()).id } });
  expect(agent.ok(), await agent.text()).toBeTruthy();
  await page.goto(`/brief?agent=${encodeURIComponent((await agent.json()).id)}`);
  await expect(page.locator('h1')).toHaveText('Visit brief');
}

test.beforeEach(async ({ page }) => {
  await blockThirdParty(page);
  await signIn(page, uniqueEmail(), CARE_LOG);
});

test('the brief fits a 375px phone and offers Print', async ({ page }) => {
  await openBrief(page);
  const w = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(w.scroll).toBe(w.client);

  const print = page.locator('#brief-print');
  await expect(print).toBeVisible();
  // The browser's dialog is not something a test can drive; that the
  // button calls window.print is.
  await page.evaluate(() => {
    (window as unknown as { printed: number }).printed = 0;
    window.print = () => { (window as unknown as { printed: number }).printed += 1; };
  });
  await print.click();
  expect(await page.evaluate(
    () => (window as unknown as { printed: number }).printed)).toBe(1);
});

test('printed, the brief keeps its content and drops the controls',
  async ({ page }) => {
    await openBrief(page);
    await page.emulateMedia({ media: 'print' });
    for (const sel of ['#brief-print', '.hub-head-actions', '.topbar',
                       '.chat-feedback']) {
      await expect(page.locator(sel).first()).toBeHidden();
    }
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.locator('.hub-section h2').first()).toBeVisible();
    await expect(page.locator('.footer-fine')).toBeVisible();
    // Paper is white whatever the phone's theme.
    const bg = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor);
    expect(bg).toBe('rgb(255, 255, 255)');
  });
