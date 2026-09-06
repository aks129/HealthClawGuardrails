import { test, expect } from '@playwright/test';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import {
  blockThirdParty, codeFromLog, requestCode, uniqueEmail,
} from './careagents-fixtures';

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
 * NOT covered here, on purpose: chat/LLM turns, any HealthClaw call, and
 *          passkey registration or passkey sign-in (WebAuthn is not driven —
 *          only the enrolment *prompt* is asserted, then skipped). The
 *          connect tiles, the beta banner and the Telegram surface are
 *          covered in careagents-connect-tiles.spec.ts, which asserts what a
 *          beta tester sees under CARE_REAL_RECORDS. Only the four sign-in
 *          helpers moved out of this file (careagents-fixtures.ts).
 *
 * The app is booted by the second webServer entry in playwright.config.ts
 * with HEALTHCLAW_BASE pointed at a dead local port, so the server cannot
 * reach a real records service; blockThirdParty() closes the same door on the
 * browser side.
 */

// CareAgents runs on its own port; every test in this file targets it.
test.use({ baseURL: CARE_BASE_URL });

test.beforeEach(async ({ page }) => {
  await blockThirdParty(page);
});

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
    const real = await codeFromLog(email, CARE_LOG);
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
    const code = await codeFromLog(email, CARE_LOG);
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
