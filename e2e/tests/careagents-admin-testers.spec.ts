import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG, CARE_OWNER_EMAIL } from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * The owner's Testers table on /admin/funnel (careagents/signins.py), at
 * desktop width and at 375px. A fresh tester signs in first, so the owner
 * sees a row for an account that joined and never asked a question, which
 * is highlighted. The owner address is fixed server env, so the owner signs
 * in once per run: one test walks both widths.
 */

async function width(page: Page) {
  return page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
}

test.describe('owner Testers table', () => {
  test.use({ baseURL: CARE_BASE_URL });

  test('a non-owner gets 404, the owner sees each tester at both widths',
    async ({ browser }) => {
      const tester = uniqueEmail();
      const tctx = await browser.newContext({ baseURL: CARE_BASE_URL });
      const tpage = await tctx.newPage();
      await blockThirdParty(tpage);
      await signIn(tpage, tester, CARE_LOG);
      expect((await tpage.request.get('/admin/funnel')).status()).toBe(404);
      expect((await tpage.request.get('/admin/funnel/testers.csv')).status())
        .toBe(404);
      await tctx.close();

      const ctx = await browser.newContext({
        baseURL: CARE_BASE_URL, viewport: { width: 1280, height: 900 } });
      const page = await ctx.newPage();
      await blockThirdParty(page);
      await signIn(page, CARE_OWNER_EMAIL, CARE_LOG);

      await page.goto('/admin/funnel');
      const section = page.locator('#funnel-testers');
      await expect(section.getByRole('heading', { name: 'Testers' }))
        .toBeVisible();
      const row = section.locator('tbody tr', { hasText: tester });
      await expect(row).toHaveCount(1);
      await expect(row).toHaveClass(/is-stuck/);
      await expect(row).toContainText('never asked');
      await expect(row).toContainText('email code');
      await expect(row).toContainText('Get started');
      let w = await width(page);
      expect(w.scroll).toBe(w.client);
      // At 1280px the whole table fits: no column behind a hidden scroll.
      const box = await section.locator('.funnel-scroll').evaluate(
        (el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      expect(box.scroll).toBeLessThanOrEqual(box.client);
      await page.screenshot({ path: test.info().outputPath('testers-desktop.png'),
                              fullPage: true });

      const csv = await page.request.get('/admin/funnel/testers.csv');
      expect(csv.status()).toBe(200);
      expect(csv.headers()['cache-control']).toBe('no-store');
      expect(await csv.text()).toContain(tester);

      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto('/admin/funnel');
      await expect(section.locator('tbody tr', { hasText: tester }))
        .toHaveCount(1);
      // The table scrolls inside its own box; the page never does.
      w = await width(page);
      expect(w.scroll).toBe(w.client);
      await page.screenshot({ path: test.info().outputPath('testers-375.png'),
                              fullPage: true });
      await ctx.close();
    });
});
