import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, codeFromLog, uniqueEmail } from './careagents-fixtures';

/**
 * A link tapped while signed out comes back after the code (beta feedback,
 * 2026-10-22): a texted link to someone's own page opened in a browser with
 * no session, the code sign-in ended on the hub, and they had to find the
 * page again. auth.js still goes to /home; /home now sends it on. The
 * patient walk of #1016 added the words: why a code, and that the page is
 * kept, and no passkey offer standing between the code and the page.
 */

// Its own code-request bucket (TEST-NET-2): from the shared 127.0.0.1 one
// this sign-in spent the budget other specs count on (sec-960, #1016 QA F1).
test.use({ viewport: { width: 375, height: 812 }, isMobile: true,
           hasTouch: true,
           extraHTTPHeaders: { 'X-Real-IP': '198.51.100.16' } });

const RESUME_LINE = "To keep your health information safe, we check it's " +
  'you first. Enter the ' +
  "code we email you and we'll take you back to the page you opened.";

async function codeIn(page: Page, email: string): Promise<void> {
  await page.fill('#email', email);
  await page.click('#email-btn');
  await expect(page.locator('#step-code')).toBeVisible();
  await page.fill('#code', await codeFromLog(email, CARE_LOG));
  await page.click('#verify-btn');
}

/** Answer the code check as for an account that already has records and
 * no passkey: the one case auth.js offers "Add a passkey" in. The real
 * check still runs and signs in; only the two flags are set. */
async function asAnAccountWithRecords(page: Page): Promise<void> {
  await page.route('**/api/auth/verify', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, first_run: false,
                                            has_passkey: false } });
  });
}

test('signing in from a tapped link lands on that page, not the hub',
  async ({ page }) => {
    await blockThirdParty(page);
    await page.goto(`${CARE_BASE_URL}/settings`);
    await expect(page).toHaveURL(/\/auth$/);
    // Why a code, and that the page is kept; never which page.
    await expect(page.locator('#resume-why')).toHaveText(RESUME_LINE);
    await codeIn(page, uniqueEmail());
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.locator('h1')).toHaveText('Settings');
    // Once: the hub is the hub after that.
    await page.goto(`${CARE_BASE_URL}/home`);
    await expect(page).toHaveURL(/\/home$/);
  });

test('no passkey offer stands between the code and the tapped page',
  async ({ page }) => {
    await blockThirdParty(page);
    await asAnAccountWithRecords(page);
    await page.goto(`${CARE_BASE_URL}/settings`);
    await expect(page).toHaveURL(/\/auth$/);
    await codeIn(page, uniqueEmail());
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.locator('h1')).toHaveText('Settings');
  });

test('with no page waiting, the same answer still offers a passkey',
  async ({ page }) => {
    // The control: proves the answer above reaches the passkey branch.
    await blockThirdParty(page);
    await asAnAccountWithRecords(page);
    await page.goto(`${CARE_BASE_URL}/auth`);
    await expect(page.locator('#resume-why')).toHaveCount(0);
    await codeIn(page, uniqueEmail());
    await expect(page.locator('#step-passkey')).toBeVisible();
  });
