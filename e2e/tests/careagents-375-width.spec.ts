import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, signIn } from './careagents-fixtures';

/**
 * No sideways scroll at 375px (#847). The hub head prints the account's
 * email, and an address with nowhere to break held its column at full
 * width: /settings measured 629px on a 375px phone, and the surfaces grid
 * kept two columns. The address is unique per run and has no hyphen or dot
 * in its local part, which is the shape that never wraps.
 *
 * Chat is not covered here: it needs the records service, which this
 * harness pins to a dead port. Its header was measured by hand (PR #847).
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

const longEmail = () =>
  `averylongsynthetictesteraddress${Date.now()}${Math.floor(Math.random() * 1e4)}` +
  '@examplehealthclawtesting.test';

async function width(page: Page) {
  return page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
}

test.describe('375px, long email', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, longEmail(), CARE_LOG);
  });

  test('the hub does not scroll sideways', async ({ page }) => {
    await page.goto('/home');
    const w = await width(page);
    expect(w.scroll).toBe(w.client);
  });

  test('settings does not scroll sideways and stacks the surfaces', async ({ page }) => {
    await page.goto('/settings');
    const w = await width(page);
    expect(w.scroll).toBe(w.client);
    const cols = await page.locator('.surface-row').evaluate(
      (el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
    expect(cols).toBe(1);
  });
});
