import { test, expect } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, codeFromLog, uniqueEmail } from './careagents-fixtures';

/**
 * A link tapped while signed out comes back after the code (beta feedback,
 * 2026-10-22): a texted link to someone's own page opened in a browser with
 * no session, the code sign-in ended on the hub, and they had to find the
 * page again. auth.js still goes to /home; /home now sends it on.
 */

test.use({ viewport: { width: 375, height: 812 }, isMobile: true,
           hasTouch: true });

test('signing in from a tapped link lands on that page, not the hub',
  async ({ page }) => {
    await blockThirdParty(page);
    const email = uniqueEmail();
    await page.goto(`${CARE_BASE_URL}/settings`);
    await expect(page).toHaveURL(/\/auth$/);
    await page.fill('#email', email);
    await page.click('#email-btn');
    await expect(page.locator('#step-code')).toBeVisible();
    await page.fill('#code', await codeFromLog(email, CARE_LOG));
    await page.click('#verify-btn');
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.locator('h1')).toHaveText('Settings');
    // Once: the hub is the hub after that.
    await page.goto(`${CARE_BASE_URL}/home`);
    await expect(page).toHaveURL(/\/home$/);
  });
