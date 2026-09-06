/**
 * Shared CareAgents browser fixtures.
 *
 * Lifted verbatim out of careagents.spec.ts when a second CareAgents spec
 * appeared (the beta connect tiles), so both suites sign a session in the same
 * way rather than drifting apart. Not a `*.spec.ts`, so Playwright's default
 * testMatch never collects this file as a suite.
 *
 * Every helper here talks ONLY to a CareAgents server booted by
 * playwright.config.ts, whose HEALTHCLAW_BASE points at a dead local port.
 */

import { expect, Page } from '@playwright/test';
import * as fs from 'fs';

/**
 * Fail the journey shut at the network edge: only the app under test may be
 * contacted. This is both a determinism fix — a slow or blocked third party
 * stalls the `load` event that page.goto waits on — and a standing check that
 * the covered journey needs no third party.
 */
export async function blockThirdParty(page: Page): Promise<void> {
  await page.route('**/*', (route) => {
    const url = route.request().url();
    if (!url.startsWith('http')) return route.continue();
    const host = new URL(url).hostname;
    return host === 'localhost' || host === '127.0.0.1'
      ? route.continue()
      : route.abort();
  });
}

/**
 * Unique per test. Reusing an address breaks reruns: within RESEND_COOLDOWN
 * (30s, careagents/accounts.py:36) start_email_code mints nothing and
 * /api/auth/email answers {"sent": false, "reason": "cooldown"} (#262) — so
 * no new code would ever reach the log.
 */
export const uniqueEmail = () =>
  `e2e-${Date.now()}-${Math.floor(Math.random() * 1e4)}@example.test`;

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * With no RESEND_API_KEY the mail stub (careagents/mail.py:19) logs
 *   "DEV email — <verb> for <email>: <8 digits>"
 * to stderr instead of sending, and the webServer command redirects that
 * into a log file. Match on this test's own address so a code minted for
 * another test can never satisfy this poll.
 *
 * `logPath` is explicit — there is more than one CareAgents server now, and
 * polling the wrong one's log is a ten-second timeout with a confusing
 * message instead of a clear failure.
 */
export async function codeFromLog(
  email: string,
  logPath: string,
): Promise<string> {
  let code = '';
  await expect
    .poll(
      () => {
        const log = fs.existsSync(logPath)
          ? fs.readFileSync(logPath, 'utf8')
          : '';
        const re = new RegExp(
          `DEV email — [^\\n]* for ${escapeRe(email)}: (\\d{8})`, 'g');
        for (let m = re.exec(log); m !== null; m = re.exec(log)) code = m[1];
        return code;
      },
      // The stub logs before /api/auth/email even responds, so this is
      // generous; if it expires, the server was not booted through this
      // harness (or a real RESEND_API_KEY leaked into its env).
      { timeout: 10_000, message: `no DEV email code logged for ${email}` },
    )
    .toMatch(/^\d{8}$/);
  return code;
}

/** Walk the auth UI up to the code-entry step for a fresh email. */
export async function requestCode(page: Page, email: string): Promise<void> {
  await page.goto('/auth');
  await page.locator('#email').fill(email);
  await page.locator('#email-btn').click();
  await expect(page.locator('#step-code')).toBeVisible();
  await expect(page.locator('#code-email')).toHaveText(email);
}

/**
 * The whole email-code journey, ending on the hub. Same steps the
 * 'email-code sign-in reaches the hub' test asserts one by one — this is the
 * arrangement other tests need before they can look at anything signed in.
 */
export async function signIn(
  page: Page,
  email: string,
  logPath: string,
): Promise<void> {
  await requestCode(page, email);
  const code = await codeFromLog(email, logPath);
  await page.locator('#code').fill(code);
  await page.locator('#verify-btn').click();

  // On a WebAuthn-capable browser the first verified sign-in offers passkey
  // enrolment (auth.js:72); elsewhere it goes straight to the hub. Creating a
  // passkey is out of scope here and needs a virtual authenticator.
  if (await page.evaluate(() => !!window.PublicKeyCredential)) {
    await expect(page.locator('#step-passkey')).toBeVisible();
    await page.locator('#skip-passkey-btn').click();
  }
  await expect(page).toHaveURL(/\/home$/);
}
