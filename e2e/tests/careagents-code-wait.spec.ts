import { test, expect } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';
import { blockThirdParty, uniqueEmail } from './careagents-fixtures';

/**
 * The code screen's resend wait counts down and then gives the way to a
 * new code (patient re-walk on #979, 26-code-wait-after.png): it used to
 * stop at "in 22 seconds" with no button when the time was up.
 */

test.use({ viewport: { width: 375, height: 812 }, isMobile: true,
           hasTouch: true });

test('the resend wait counts down, then Send a new code comes back',
  async ({ page }) => {
    await blockThirdParty(page);
    await page.clock.install();
    const email = uniqueEmail();
    await page.goto(`${CARE_BASE_URL}/auth`);
    await page.fill('#email', email);
    await page.click('#email-btn');
    await expect(page.locator('#step-code')).toBeVisible();
    // Asked again at once: the server's 30-second wait holds the send.
    await page.click('#back-btn');
    await page.fill('#email', email);
    await page.click('#email-btn');
    const note = page.locator('#code-note');
    await expect(note).toContainText(/request a new one in \d+ seconds/);
    const first = Number((await note.textContent())!.match(/in (\d+) s/)![1]);
    await expect(page.locator('#resend-code')).toBeHidden();
    await page.clock.runFor(5_000);
    await expect(note).toContainText(`in ${first - 5} seconds`);
    await page.clock.runFor((first - 5) * 1000);
    await expect(note).toContainText('You can ask for a new code now.');
    await expect(page.locator('#resend-code')).toBeVisible();
  });
