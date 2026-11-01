import { test, expect } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * The hub's invite card and section sentences at 375px (#1080): a new
 * account sees its personal link, the count as slots, one Share button
 * (the phone's share sheet, or a copy where there is none), and one plain
 * sentence under every section header. The gating (paused, used up,
 * turned off, confirm-link session) is tests/test_careagents_hub_invite_1080.py.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

test.describe('hub invite card at 375px', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
  });

  test('a new account sees the card and a sentence under every header',
    async ({ page }) => {
      await signIn(page, uniqueEmail(), CARE_LOG);
      const card = page.locator('#invite-section');
      await expect(card).toBeVisible();
      await expect(card.locator('.section-line')).toHaveText(
        'Give someone their own health assistant. Their records stay theirs.');
      await expect(card.locator('.invite-used')).toHaveText('0 of 5 used');
      await expect(card.locator('.invite-slot')).toHaveCount(5);
      await expect(card.locator('.invite-slot.is-used')).toHaveCount(0);
      await expect(card.locator('#invite-url')).toContainText('/i?c=');
      await expect(card.getByRole('button')).toHaveCount(1);
      await expect(card.getByRole('button', { name: 'Share your link' })).toBeVisible();

      const heads = page.locator('main.hub h2');
      const n = await heads.count();
      expect(n).toBeGreaterThanOrEqual(5);
      for (let i = 0; i < n; i++) {
        const line = heads.nth(i).locator(
          'xpath=(following::p[contains(@class,"section-line")])[1]');
        await expect(line).toBeVisible();
      }
      await expect(page.locator('main.hub h1')).toHaveText('Your home page');
      // Nothing scrolls sideways on a phone.
      expect(await page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });

  test('Share opens the share sheet with the link', async ({ page }) => {
    await page.addInitScript(() => {
      (window as any).__shared = null;
      (navigator as any).share = async (data: unknown) => {
        (window as any).__shared = data;
      };
    });
    await signIn(page, uniqueEmail(), CARE_LOG);
    const link = (await page.locator('#invite-url').innerText()).trim();
    await page.getByRole('button', { name: 'Share your link' }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__shared))
      .toMatchObject({ url: link });
  });

  test('without a share sheet, Share copies the link', async ({ page }) => {
    await page.addInitScript(() => {
      // Chromium on a desktop has no share sheet; make sure of it.
      Object.defineProperty(navigator, 'share', { value: undefined });
    });
    await signIn(page, uniqueEmail(), CARE_LOG);
    await page.getByRole('button', { name: 'Share your link' }).click();
    // Copied, or, with no clipboard access, the link is selected to copy.
    await expect(page.locator('#invite-msg'))
      .toHaveText(/^(Link copied\.|Press and hold the link to copy it\.)$/);
  });

  test('/hub opens the home page', async ({ page }) => {
    await signIn(page, uniqueEmail(), CARE_LOG);
    await page.goto('/hub');
    await expect(page).toHaveURL(`${CARE_BASE_URL}/home`);
  });
});
