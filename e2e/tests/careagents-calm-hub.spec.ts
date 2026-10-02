import { test, expect } from '@playwright/test';
import {
  CARE_ALLOW_BASE_URL, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG,
  CARE_BASE_URL, CARE_LOG,
} from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * The calm hub a beta tester first sees, on a phone
 * (docs/superpowers/specs/2026-09-26-careagents-calm-hub-design.md).
 *
 * Proves what the browser renders. HEALTHCLAW_BASE is a dead port here,
 * so nothing about records being accepted is proven.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

const CLOSED_LINE =
  "Coming for invited testers: your doctor's records, Apple Health and " +
  'wearables, uploading a file from your patient portal.';

test.describe('calm hub, real records closed', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, uniqueEmail(), CARE_LOG);
  });

  test('one primary action and one line, no coming-soon rows', async ({ page }) => {
    await expect(page.locator('#explore-sample'))
      .toHaveText('Explore with made-up records');
    await expect(page.locator('.menu-closed-line')).toHaveText(CLOSED_LINE);
    await expect(page.locator('.connector-row')).toHaveCount(0);
    await expect(page.locator('.chip')).toHaveCount(0);
  });

  test('the sample action reaches the live path', async ({ page }) => {
    await page.locator('#explore-sample').click();
    // The records service is a dead port in this harness, so the live
    // branch stops there, under the button. A closed source is refused
    // before HealthClaw is called, with different words.
    const msg = page.locator('#explore-sample + #connect-msg');
    await expect(msg).toHaveText('records service unavailable');
  });

  test('a new account is told honestly that nothing waits', async ({ page }) => {
    await expect(page.locator('#waiting .waiting-line'))
      .toHaveText(/^Nothing yet\./);
  });

  test('a failed count says so', async ({ page }) => {
    await page.route('**/api/approvals/count', (route) => route.fulfill({
      status: 503, contentType: 'application/json',
      body: '{"error":"unavailable"}',
    }));
    await page.reload();
    await expect(page.locator('#waiting .waiting-line'))
      .toHaveText("Couldn't check for requests.");
    await expect(page.locator('#waiting')).toHaveAttribute('data-state', 'fail');
  });

  test('two assistants with requests get one link each', async ({ page }) => {
    await page.route('**/api/approvals/count', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        count: 3, agent_id: 'agent_a', href: '/agents/agent_a/approvals',
        queues: [
          { agent_id: 'agent_a', name: 'Juniper', count: 2,
            href: '/agents/agent_a/approvals' },
          { agent_id: 'agent_b', name: 'Coach', count: 1,
            href: '/agents/agent_b/approvals' },
        ],
      }),
    }));
    await page.reload();
    await expect(page.locator('#waiting')).toHaveAttribute('data-state', 'pending');
    await expect(page.locator('#waiting .waiting-line'))
      .toHaveText('3 requests waiting for your approval:');
    const links = page.locator('#waiting .waiting-queues a');
    await expect(links).toHaveText(['Juniper: 2 requests', 'Coach: 1 request']);
    await expect(links.nth(1)).toHaveAttribute('href', '/agents/agent_b/approvals');
  });

  test('the banner names the sample for a new account', async ({ page }) => {
    await expect(page.locator('.beta-banner'))
      .toHaveText('Beta: sample records. Things will break, tell us.');
  });

  test('the hub has no horizontal scroll at 375px', async ({ page }) => {
    const wide = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth);
    expect(wide).toBe(false);
  });
});

test.describe('calm hub, real records open (allowlisted)', () => {
  test.use({ baseURL: CARE_ALLOW_BASE_URL, ...PHONE });

  test('grouped menu with one chip per source', async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG);
    for (const g of ['Find my records', 'Record services', 'Bring a file',
                     'Devices and apps']) {
      await expect(page.locator('.menu-group h3', { hasText: g })).toBeVisible();
    }
    await expect(page.locator('.connector-row[data-connector="direct"] .chip'))
      .toHaveText('Available');
    await expect(page.locator('.connector-row[data-connector="hbo"] .chip'))
      .toHaveText('Coming soon');
    await expect(page.locator('#explore-sample')).toHaveCount(0);
    await expect(page.locator('.sample-link')).toBeVisible();
  });
});

/**
 * Phone-width polish from the 2026-10-01 synthetic-hospital shakeout
 * (finding 5). Two upload connections and an assistant are made through the
 * API: a direct upload never calls HealthClaw, so the dead port is fine.
 */
test.describe('phone width, two uploads and an assistant (allowlisted)', () => {
  test.use({ baseURL: CARE_ALLOW_BASE_URL, ...PHONE });

  // The allowlisted account is shared by every test on this server, so its
  // connections pile up across tests. Each test looks only at its own two.
  let agentId = '';
  let ids: string[] = [];

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG);
    ids = [];
    for (let i = 0; i < 2; i++) {
      const r = await page.request.post('/api/connections/direct',
                                        { data: { consent: true } });
      expect(r.ok()).toBe(true);
      ids.push((await r.json()).id);
    }
    await page.reload();
    const a = await page.request.post('/api/agents', {
      data: { name: 'Morning Helper', persona: 'calm', connection_id: ids[0] },
    });
    expect(a.ok()).toBe(true);
    agentId = (await a.json()).id;
  });

  test('chat header pills sit inside the gutter on one line each',
    async ({ page }, testInfo) => {
      // The grade a real deployment shows, so the pill is as wide as a
      // tester sees it, not the "…" placeholder.
      await page.route('**/api/trust', (route) => route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ badge: 'A (all checks pass)' }),
      }));
      await page.goto(`/chat?agent=${agentId}`);
      await expect(page.locator('#trust-pill')).toHaveText('guardrails A');
      await page.screenshot({ path: testInfo.outputPath('chat-375.png') });

      const width = page.viewportSize()!.width;
      const lines = (sel: string) => page.locator(sel).evaluateAll((els) =>
        els.map((el) => {
          const cs = getComputedStyle(el);
          const inner = el.getBoundingClientRect().height
            - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)
            - parseFloat(cs.borderTopWidth) - parseFloat(cs.borderBottomWidth);
          return Math.round(inner / parseFloat(cs.lineHeight));
        }));

      const pills = page.locator('.chat-tools .pill');
      await expect(pills).toHaveCount(3);
      for (const box of await pills.evaluateAll((els) =>
        els.map((el) => el.getBoundingClientRect().toJSON()))) {
        expect(box.left).toBeGreaterThanOrEqual(16);
        expect(box.right).toBeLessThanOrEqual(width - 16);
      }
      expect(await lines('.chat-tools .pill')).toEqual([1, 1, 1]);
      expect(await lines('.chat-name')).toEqual([1]);
      expect(await lines('.chat-sub')).toEqual([1]);
    });

  test('the upload control looks like the other card buttons',
    async ({ page }, testInfo) => {
      const card = page.locator(`.conn-card[data-conn="${ids[0]}"]`);
      await card.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath('hub-375.png'),
                              fullPage: true });
      const style = (sel: string) => card.locator(sel).evaluate((el) => {
        const cs = getComputedStyle(el);
        return [cs.borderRadius, cs.borderStyle, cs.backgroundColor,
                cs.minHeight, cs.fontSize, cs.fontWeight, cs.fontFamily];
      });
      expect(await style('.conn-upload')).toEqual(await style('.conn-disconnect'));
    });

  test('two upload connections can be told apart', async ({ page }) => {
    const name = (id: string) =>
      page.locator(`.conn-card[data-conn="${id}"] .hub-card-name`);
    await expect(name(ids[0])).toHaveText(/^Uploaded records \d+$/);
    await expect(name(ids[1])).toHaveText(/^Uploaded records \d+$/);
    expect(await name(ids[0]).textContent())
      .not.toBe(await name(ids[1]).textContent());
  });
});
