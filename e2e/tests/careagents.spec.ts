import { test, expect, Page } from '@playwright/test';
import * as fs from 'fs';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';

/**
 * CareAgents core journey (issue #233) — the first browser-level coverage of
 * the consumer app. Deliberately small: a pre-webinar safety net, not full
 * coverage.
 *
 * Covers:  landing renders with its CTA · auth card renders · a wrong email
 *          code is rejected without crashing · email-code sign-in reaches
 *          the hub (via the dev-mode mail stub that logs codes to stderr).
 * Not covered here (on purpose): chat/LLM turns, any HealthClaw call,
 *          passkeys/WebAuthn, and the connect/wearable/Telegram/iMessage
 *          dialogs (#224 is rebuilding those — do not assert on them).
 *
 * The app is booted by the second webServer entry in playwright.config.ts
 * with HEALTHCLAW_BASE pointed at a dead local port, so nothing in this file
 * can reach a real records service.
 */

// CareAgents runs on its own port; every test in this file targets it.
test.use({ baseURL: CARE_BASE_URL });

/** Unique per test so RESEND_COOLDOWN (30s per email) never bites. */
const uniqueEmail = () =>
  `e2e-${Date.now()}-${Math.floor(Math.random() * 1e4)}@example.test`;

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * The dev-mode mail stub (careagents/mail.py) logs
 *   "DEV email — <verb> for <email>: <8 digits>"
 * to stderr, which the webServer command redirects into CARE_LOG. Poll for
 * the newest code addressed to this email.
 */
async function codeFromLog(email: string): Promise<string> {
  let code = '';
  await expect
    .poll(
      () => {
        const log = fs.existsSync(CARE_LOG)
          ? fs.readFileSync(CARE_LOG, 'utf8')
          : '';
        const re = new RegExp(
          `DEV email — .* for ${escapeRe(email)}: (\\d{8})`, 'g');
        for (let m = re.exec(log); m !== null; m = re.exec(log)) code = m[1];
        return code;
      },
      // The stub logs before /api/auth/email even responds, so this is
      // generous; if it expires, the server was not booted through the
      // harness (or a real RESEND_API_KEY leaked into its env).
      { timeout: 10_000, message: `no DEV email code logged for ${email}` },
    )
    .toMatch(/^\d{8}$/);
  return code;
}

/** Walk the auth UI up to the code-entry step for a fresh email. */
async function requestCode(page: Page, email: string): Promise<void> {
  await page.goto('/auth');
  await page.locator('#email').fill(email);
  await page.locator('#email-btn').click();
  await expect(page.locator('#step-code')).toBeVisible();
  await expect(page.locator('#code-email')).toHaveText(email);
}

test.describe('CareAgents landing', () => {
  test('renders the hero with its CTA', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/CareAgents/);
    await expect(page.locator('main.hero h1')).toContainText(
      'knows your health');
    // Signed out, the primary CTA is "Get started" into /auth.
    const cta = page.getByRole('link', { name: 'Get started' });
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute('href', '/auth');
    // NOT asserted: the live trust-badge chip — it needs a reachable
    // HealthClaw, which this harness deliberately does not provide.
  });
});

test.describe('CareAgents auth', () => {
  test('auth page renders passkey-first with the email fallback', async ({
    page,
  }) => {
    await page.goto('/auth');
    await expect(page.locator('#auth-card h1')).toHaveText(
      'Your health, your keys');
    await expect(page.locator('#passkey-btn')).toBeVisible();
    await expect(page.locator('#email')).toBeVisible();
    await expect(page.locator('#email-btn')).toBeVisible();
  });

  test('a wrong code is rejected without crashing', async ({ page }) => {
    const email = uniqueEmail();
    await requestCode(page, email);
    // Derive a guaranteed-wrong code from the real one (flip the last
    // digit) — deterministic, unlike typing a fixed 8-digit guess.
    const real = await codeFromLog(email);
    const wrong =
      real.slice(0, 7) + ((parseInt(real[7], 10) + 1) % 10).toString();
    await page.locator('#code').fill(wrong);
    await page.locator('#verify-btn').click();

    const err = page.locator('#err');
    await expect(err).toBeVisible();
    await expect(err).toContainText('That code is wrong or expired.');
    // Still on the code step, still interactive — no crash, no redirect.
    await expect(page.locator('#step-code')).toBeVisible();
    await expect(page.locator('#code')).toBeEditable();
    // And no session was minted: the hub still bounces to /auth.
    await page.goto('/home');
    await expect(page).toHaveURL(/\/auth$/);
  });

  test('email-code sign-in reaches the hub', async ({ page }) => {
    const email = uniqueEmail();
    await requestCode(page, email);
    const code = await codeFromLog(email);
    await page.locator('#code').fill(code);
    await page.locator('#verify-btn').click();

    // First verified sign-in on a WebAuthn-capable browser offers passkey
    // enrolment; passkeys are out of scope for browser tests, so skip.
    await expect(page.locator('#step-passkey')).toBeVisible();
    await page.locator('#skip-passkey-btn').click();

    await expect(page).toHaveURL(/\/home$/);
    await expect(page).toHaveTitle(/Your hub — CareAgents/);
    await expect(page.locator('main.hub h1')).toHaveText('Welcome back');
    await expect(page.locator('.hub-sub')).toContainText(email);
    // NOT asserted: connection/agent/surface dialogs (#224 rebuild).
  });
});

test.describe('CareAgents health', () => {
  test('healthz reports the account store reachable', async ({ request }) => {
    const res = await request.get('/healthz');
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.status).toBe('ok');
    expect(body.accounts).toBe(true);
  });
});
