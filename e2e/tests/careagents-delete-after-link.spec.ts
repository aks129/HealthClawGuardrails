/**
 * Delete after joining from the email link (patient re-walk on #979):
 * sign in by tapping Confirm in the beta email, open Settings, Delete my
 * account. Deleting needs a code sign-in (full_sign_in_required), and the
 * dialog now does it in place: Email me a code, the code, then type
 * delete. Before, the box closed on "For that, sign in again…" with no
 * way to do it.
 *
 * It needs the confirm email, which the default harness has no way to
 * read: a server with no mail provider sends nothing and logs nothing (no
 * email is ever logged). So it runs only against a server whose outgoing
 * beta mail is captured to a local file, and skips otherwise:
 *
 *   CARE_E2E_BASE=http://127.0.0.1:<port> \
 *   CARE_E2E_OUTBOX=<captured beta mail, one JSON object a line> \
 *   CARE_E2E_CODE_LOG=<that server's stderr, with the DEV sign-in codes> \
 *     npx playwright test careagents-delete-after-link \
 *       --config <a config with no webServer>
 *
 * Synthetic addresses only.
 */

import { test, expect } from '@playwright/test';
import fs from 'fs';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { codeFromLog, uniqueEmail } from './careagents-fixtures';

const BASE = process.env.CARE_E2E_BASE || CARE_BASE_URL;
const OUTBOX = process.env.CARE_E2E_OUTBOX || '';
const CODE_LOG = process.env.CARE_E2E_CODE_LOG || CARE_LOG;

test.use({ viewport: { width: 375, height: 812 }, isMobile: true,
           hasTouch: true });

function confirmLink(email: string): string {
  const rows = fs.readFileSync(OUTBOX, 'utf8').trim().split('\n')
    .map((l) => JSON.parse(l)).filter((m) => m.to === email);
  const m = String(rows[rows.length - 1].text)
    .match(/(https?:\/\/\S+\/beta\/confirm\?t=[\w-]+)/);
  if (!m) throw new Error('no confirm link for ' + email);
  return m[1];
}

test('a confirm-link session deletes its account through the code step',
  async ({ page }) => {
    test.skip(!OUTBOX, 'needs CARE_E2E_OUTBOX: a server with captured mail');
    const email = uniqueEmail();
    await page.goto(`${BASE}/beta`);
    await page.fill('#beta-first', 'Robin');
    await page.fill('#beta-email', email);
    await page.check('#beta-consent');
    await page.click('#beta-form button[type=submit]');
    await expect(page.locator('#beta-done')).toBeVisible();
    await expect.poll(() => {
      try { return confirmLink(email); } catch { return ''; }
    }, { timeout: 10_000 }).toMatch(/beta\/confirm/);

    // Tap Confirm on our page: a confirm-link session, not a code sign-in.
    await page.goto(confirmLink(email));
    await page.click('form[action="/beta/confirm"] button[type=submit]');
    await page.waitForURL(/\/(chat|home)/);

    await page.goto(`${BASE}/settings`);
    await page.click('#account-delete');
    const step = page.locator('#delete-code-step');
    await expect(step).toBeVisible();
    await expect(page.locator('#delete-code-why')).toContainText(
      "To delete, we'll email you a code first.");
    // The typed word alone does not open the button while the code waits.
    await page.fill('#delete-input', 'delete');
    await expect(page.locator('#delete-confirm')).toBeDisabled();

    await page.click('#delete-code-send');
    await expect(page.locator('#delete-code-note')).toContainText(email);
    await page.fill('#delete-code', await codeFromLog(email, CODE_LOG));
    await page.click('#delete-code-check');
    await expect(page.locator('#delete-code-why')).toContainText(
      'Now type delete below to finish.');
    await page.fill('#delete-input', '');
    await page.fill('#delete-input', 'delete');
    await expect(page.locator('#delete-confirm')).toBeEnabled();
    await page.click('#delete-confirm');
    await page.waitForURL(/\/\?deleted=1/);
    await expect(page.locator('.deleted-note')).toContainText(
      'Your account is deleted.');
  });
