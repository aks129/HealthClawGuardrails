import { test, expect, Page } from '@playwright/test';
import * as fs from 'fs';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';

/**
 * CareAgents core journey (issue #233) — the first browser-level coverage of
 * the consumer app. Deliberately small: a pre-webinar safety net, not full
 * coverage.
 *
 * Covers:  landing renders and its CTA navigates · auth card renders ·
 *          a wrong email code is rejected without crashing and mints no
 *          session · email-code sign-in reaches the hub · /healthz reports
 *          the account store reachable.
 *
 * NOT covered, on purpose: chat/LLM turns, any HealthClaw call, passkey
 *          registration or passkey sign-in (WebAuthn is not driven here —
 *          only the enrolment *prompt* is asserted, then skipped), and the
 *          connect/wearable/Telegram/iMessage dialogs (#224 is rebuilding
 *          those, so assertions there would collide and rot).
 *
 * The app is booted by the second webServer entry in playwright.config.ts
 * with HEALTHCLAW_BASE pointed at a dead local port, so the server cannot
 * reach a real records service; blockThirdParty() below closes the same door
 * on the browser side.
 */

// CareAgents runs on its own port; every test in this file targets it.
test.use({ baseURL: CARE_BASE_URL });

/**
 * Fail the journey shut at the network edge: only the app under test may be
 * contacted. This is both a determinism fix — base.html pulls Google Fonts,
 * and a slow or blocked CDN stalls the `load` event that page.goto waits on —
 * and a standing check that the covered journey needs no third party.
 */
async function blockThirdParty(page: Page): Promise<void> {
  await page.route('**/*', (route) => {
    const url = route.request().url();
    if (!url.startsWith('http')) return route.continue();
    const host = new URL(url).hostname;
    return host === 'localhost' || host === '127.0.0.1'
      ? route.continue()
      : route.abort();
  });
}

test.beforeEach(async ({ page }) => {
  await blockThirdParty(page);
});

/**
 * Unique per test. Reusing an address breaks reruns: within RESEND_COOLDOWN
 * (30s, careagents/accounts.py:35) start_email_code returns early and mints
 * nothing, while /api/auth/email still answers {"sent": true} — so no new
 * code would ever reach the log.
 */
const uniqueEmail = () =>
  `e2e-${Date.now()}-${Math.floor(Math.random() * 1e4)}@example.test`;

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * With no RESEND_API_KEY the mail stub (careagents/mail.py:19) logs
 *   "DEV email — <verb> for <email>: <8 digits>"
 * to stderr instead of sending, and the webServer command redirects that
 * into CARE_LOG. Match on this test's own address so a code minted for
 * another test can never satisfy this poll.
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
async function requestCode(page: Page, email: string): Promise<void> {
  await page.goto('/auth');
  await page.locator('#email').fill(email);
  await page.locator('#email-btn').click();
  await expect(page.locator('#step-code')).toBeVisible();
  await expect(page.locator('#code-email')).toHaveText(email);
}

test.describe('CareAgents landing', () => {
  test('renders the hero and its CTA reaches sign-in', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/CareAgents/);
    await expect(page.locator('main.hero h1')).toContainText(
      'knows your health');
    // Signed out, the primary CTA is "Get started". Click it rather than
    // assert its href — a link that renders but does not route is the
    // failure a browser test exists to catch.
    await page.getByRole('link', { name: 'Get started' }).click();
    await expect(page).toHaveURL(/\/auth$/);
    await expect(page.locator('#auth-card')).toBeVisible();
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
    // digit) — deterministic, unlike guessing a fixed 8-digit string.
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

    // On a WebAuthn-capable browser the first verified sign-in offers passkey
    // enrolment (auth.js:72); elsewhere it goes straight to the hub. Assert
    // whichever this browser is, then skip — creating a passkey is out of
    // scope and needs a virtual authenticator.
    const canPasskey = await page.evaluate(() => !!window.PublicKeyCredential);
    if (canPasskey) {
      await expect(page.locator('#step-passkey')).toBeVisible();
      await page.locator('#skip-passkey-btn').click();
    }

    await expect(page).toHaveURL(/\/home$/);
    await expect(page).toHaveTitle(/Your hub — CareAgents/);
    await expect(page.locator('main.hub h1')).toHaveText('Welcome back');
    await expect(page.locator('.hub-sub')).toContainText(email);
    // NOT asserted: connection/agent/surface dialogs (#224 rebuild).
  });
});

test.describe('CareAgents health', () => {
  // What this can honestly assert here is the account store, which is what
  // the test was always named for. Overall readiness additionally requires a
  // live agent-run worker, and this harness points HEALTHCLAW_BASE at a dead
  // port on purpose so a stray call cannot reach the real records service.
  // So `status` is legitimately "degraded" in e2e, and asserting "ok" was
  // asserting that our own isolation had failed.
  test('healthz reports the account store reachable', async ({ request }) => {
    const res = await request.get('/healthz');
    const body = await res.json();

    expect(body.accounts).toBe(true);

    // Degradation must be attributable. If the account store ever goes
    // unreachable, that has to fail here rather than hide behind the
    // worker's absence.
    if (res.status() !== 200) {
      expect(body.status).toBe('degraded');
      expect(body.run_workers).toBe(false);
    }
  });
});
