import { test, expect, Page } from '@playwright/test';
import {
  CARE_ALLOW_BASE_URL, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG,
  CARE_BASE_URL, CARE_LOG,
} from '../playwright.config';
import { blockThirdParty, signIn, uniqueEmail } from './careagents-fixtures';

/**
 * A beta tester's first screen, in a browser (#553, council ruling
 * 2026-09-02 D3/D6).
 *
 * The review of #553 proved the server side of the CARE_REAL_RECORDS switch —
 * the catalog JSON, the 503 on a closed connect POST — and left one gap in
 * writing: "nothing has confirmed *in a browser* that home.js's soon-tile
 * handler renders the 503 text under the tile on a phone." That is what this
 * file closes, at 390x844, which is the device this product is used on.
 *
 * WHAT THESE TESTS PROVE
 *   The browser RENDERS a given state: which tiles a signed-in account sees,
 *   what words are on them, what appears under one when a thumb taps it, and
 *   that a tile the beta closed opens no dialog and starts no connection.
 *   Every assertion is on text or DOM a person could read off the screen, not
 *   on a network call.
 *
 * WHAT THEY DO NOT PROVE
 *   That HealthClaw accepted anything. Both CareAgents servers here run with
 *   HEALTHCLAW_BASE on a dead local port (playwright.config.ts), so no records
 *   service exists to accept or reject a thing: no tenant is created, no
 *   bundle is ingested, and the sample-records flow gets exactly as far as the
 *   call to HealthClaw and no further. Cross-boundary behaviour needs a
 *   running HealthClaw and is out of scope here, deliberately.
 *   Also not proven: the Fasten and wearable flows themselves (nothing here
 *   starts one), and anything about the deployed site.
 *
 * Synthetic addresses only (@example.test, RFC 2606). No mail is sent: the
 * dev-mode stub logs each sign-in code to stderr, and the spec reads it back.
 */

// A phone, because that is where a tester meets this: the hub's marketplace
// is a single column here, and the refusal has to land where a thumb is.
const PHONE = {
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

/** The three sources that take a person's OWN records (connectors.py:74). */
const REAL_RECORD_TILES = ['fasten', 'wearable', 'direct'] as const;

/** connectors.py:75 — verbatim. This is the sentence a tester reads. */
const REFUSED =
  "real-records connect isn't open on this beta deployment yet — " +
  'start with the sample records';

/** connectors.py:94 — the blurb that replaces the live one when closed. */
const CLOSED_BLURB = 'Not open in this beta — start with the sample records.';

/** _beta_banner.html — eleven words, on the landing page and the hub. */
const BANNER = 'Beta: synthetic records only. Things will break — tell us.';

const tile = (page: Page, id: string) =>
  page.locator(`.connector-tile[data-connector="${id}"]`);

/** The one shared #connect-msg, but only where say() moved it: directly
 *  after the tapped tile. A CSS sibling selector, so it retries. */
const msgUnder = (page: Page, id: string) =>
  page.locator(`.connector-tile[data-connector="${id}"] + #connect-msg`);

test.describe('CareAgents hub — real records closed (CARE_REAL_RECORDS off)', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
    // A fresh account per test: no connections, no leftovers, and the first
    // screen is what a new tester actually gets.
    await signIn(page, uniqueEmail(), CARE_LOG);
  });

  test('the Fasten, wearable and upload tiles read "coming soon"', async ({
    page,
  }) => {
    for (const id of REAL_RECORD_TILES) {
      const t = tile(page, id);
      await expect(t, `${id} tile missing`).toBeVisible();
      // The words on the tile, as read off the screen.
      await expect(t.locator('.connector-tag')).toHaveText('coming soon');
      await expect(t.locator('.connector-blurb')).toHaveText(CLOSED_BLURB);
      await expect(t).toHaveClass(/tier-soon/);
      // ...and it is not dressed as a connection that will happen: no consent
      // card is promised, because there is nothing to consent to.
      await expect(t).toHaveAttribute('data-soon', '1');
      expect(await t.getAttribute('data-consent'),
        `${id} still carries the consent flag`).toBeNull();
    }
    // What the eye actually gets, which the DOM text alone does not say: the
    // tag is CSS-uppercased ("COMING SOON" on screen, `coming soon` in the
    // node above), and the tile is drawn with a dashed border — the visual
    // difference between a source you can tap into and one that is parked.
    const tag = await tile(page, 'fasten').locator('.connector-tag').evaluate(
      (el) => getComputedStyle(el).textTransform);
    expect(tag, 'the "coming soon" tag is no longer uppercased on screen')
      .toBe('uppercase');
    const border = await tile(page, 'fasten').evaluate(
      (el) => getComputedStyle(el).borderStyle);
    expect(border, 'a closed tile is drawn as a solid, tappable card')
      .toBe('dashed');

    // The labels stay — a tester should see the source exists and is not yet
    // open, rather than wonder where it went.
    await expect(tile(page, 'fasten').locator('.connector-label'))
      .toHaveText('Your provider (verified)');
    await expect(tile(page, 'wearable').locator('.connector-label'))
      .toHaveText('Apple Health & wearables');
    await expect(tile(page, 'direct').locator('.connector-label'))
      .toHaveText('Upload records');
  });

  test('tapping a closed tile shows the refusal under that tile', async ({
    page,
  }) => {
    for (const id of REAL_RECORD_TILES) {
      await tile(page, id).click();

      const msg = msgUnder(page, id);
      await expect(msg, `no refusal under the ${id} tile`).toBeVisible();
      await expect(msg).toHaveText(REFUSED);
      // On a phone the message is worthless if it lands off-screen.
      await expect(msg).toBeInViewport();

      // Nothing else happened: no consent card, no provider picker (the
      // wearable tile still carries its provider list), no popup window, no
      // connection on the hub.
      await expect(page.locator('#consent-modal')).toBeHidden();
      await expect(page.locator('#provider-picker')).toBeHidden();
      expect(page.context().pages().length,
        'a tap on a closed tile opened a window').toBe(1);
      await expect(page.locator('#connections .conn-card')).toHaveCount(0);
      // And the tile does not promise a follow-up it will not send: the
      // waitlist wording belongs to genuinely-soon sources, not to one the
      // beta switch closed.
      await expect(tile(page, id).locator('.connector-tag'))
        .toHaveText('coming soon');
    }
  });

  test('the sample-records tile is still live and still opens its flow',
    async ({ page }) => {
      const t = tile(page, 'sample');
      await expect(t).toBeVisible();
      await expect(t).toHaveClass(/tier-live/);
      await expect(t.locator('.connector-label'))
        .toHaveText('Try it with sample records');
      // A live tile carries no tag at all — nothing telling a tester to wait.
      await expect(t.locator('.connector-tag')).toHaveCount(0);
      expect(await t.getAttribute('data-soon')).toBeNull();

      await t.click();

      // What this can honestly assert: the tap reached the LIVE path. This
      // harness points HEALTHCLAW_BASE at a dead port on purpose, so the flow
      // runs as far as the records service and stops there. That message can
      // only come from the live branch — a closed tile is refused before
      // HealthClaw is ever called, with the sentence asserted above. So the
      // two paths are distinguishable here, and this is the live one.
      // NOT asserted: that a sample tenant was created or seeded. That needs
      // a running HealthClaw and belongs in a cross-boundary test.
      const msg = msgUnder(page, 'sample');
      await expect(msg).toBeVisible();
      await expect(msg).toHaveText('records service unavailable');
      await expect(page.locator('#consent-modal')).toBeHidden();
    });

  test('the Telegram surface is a label, not a control', async ({ page }) => {
    const tg = page.locator('.surface', { hasText: 'Telegram' });
    await expect(tg).toBeVisible();
    await expect(tg.locator('b')).toHaveText('Telegram');
    await expect(tg.locator('span')).toHaveText('coming soon');
    await expect(tg).toHaveClass(/soon/);
    // #536/D6: the ids are gone, so home.js has nothing to bind.
    expect(await tg.getAttribute('id')).toBeNull();
    await expect(page.locator('#tg-surface')).toHaveCount(0);
    await expect(page.locator('#tg-state')).toHaveCount(0);

    // And a tap does nothing at all: no request, no dialog, no text change.
    const calls: string[] = [];
    page.on('request', (r) => {
      if (r.url().includes('/api/')) calls.push(`${r.method()} ${r.url()}`);
    });
    await tg.click();
    // Proving an absence, so give the handler that does not exist time to
    // run: a bound handler would have fired its fetch well inside this.
    await page.waitForTimeout(500);
    expect(calls, 'the Telegram tile called the API').toEqual([]);
    await expect(page.locator('#code-card')).toBeHidden();
    await expect(page.locator('#surfaces-msg')).toBeHidden();
    await expect(tg.locator('span')).toHaveText('coming soon');
  });

  test('screenshot: the hub as a beta tester first sees it', async ({
    page,
  }, testInfo) => {
    const first = testInfo.outputPath('hub-real-records-off-390x844.png');
    await page.screenshot({ path: first, fullPage: true });
    await testInfo.attach('hub — CARE_REAL_RECORDS off (390x844)',
      { path: first, contentType: 'image/png' });

    // ...and the state nobody had seen: the refusal, under the thumb.
    await tile(page, 'fasten').click();
    await expect(msgUnder(page, 'fasten')).toBeVisible();
    const refused = testInfo.outputPath('hub-closed-tile-refused-390x844.png');
    await page.screenshot({ path: refused, fullPage: true });
    await testInfo.attach('hub — a closed tile tapped (390x844)',
      { path: refused, contentType: 'image/png' });
  });
});

test.describe('CareAgents beta banner', () => {
  test.use({ baseURL: CARE_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
  });

  test('renders on the landing page', async ({ page }) => {
    await page.goto('/');
    const banner = page.locator('.beta-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveText(BANNER);
    await expect(banner).toBeInViewport();
  });

  test('renders on the hub', async ({ page }) => {
    await signIn(page, uniqueEmail(), CARE_LOG);
    const banner = page.locator('.beta-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveText(BANNER);
    // Above the fold on a phone — a tester should not have to scroll to
    // learn the records are synthetic.
    await expect(banner).toBeInViewport();
  });
});

test.describe('CareAgents hub — CARE_REAL_RECORDS=allowlist', () => {
  // A second server, same code, allowlist mode (playwright.config.ts). Fasten
  // key and wearables are wired there, so the allowlist is the only thing
  // deciding what these two accounts see.
  test.use({ baseURL: CARE_ALLOW_BASE_URL, ...PHONE });

  test.beforeEach(async ({ page }) => {
    await blockThirdParty(page);
  });

  test('the allowlisted account sees the real tiles live', async ({ page }) => {
    await signIn(page, CARE_ALLOW_EMAIL, CARE_ALLOW_LOG);

    for (const id of REAL_RECORD_TILES) {
      const t = tile(page, id);
      await expect(t).toBeVisible();
      await expect(t).not.toHaveClass(/tier-soon/);
      expect(await t.getAttribute('data-soon'),
        `${id} still closed for an allowlisted account`).toBeNull();
      // Live real-record tiles carry the consent gate; the closed ones cannot.
      await expect(t).toHaveAttribute('data-consent', '1');
    }
    // "Live" has no badge of its own, so the absence of the tag IS the
    // rendering: no "coming soon" for a tester to wait on.
    await expect(tile(page, 'fasten').locator('.connector-tag')).toHaveCount(0);
    await expect(tile(page, 'wearable').locator('.connector-tag'))
      .toHaveCount(0);
    await expect(tile(page, 'direct').locator('.connector-tag'))
      .toHaveText('import');
    await expect(tile(page, 'wearable'))
      .toHaveAttribute('data-providers', /Apple Health/);
    // The live blurb is back — the beta closure sentence is gone.
    await expect(tile(page, 'fasten').locator('.connector-blurb')).toHaveText(
      'Log in to your clinic or hospital portal. Verified; we never see ' +
      'your password.');
    await expect(page.locator('.marketplace')).not.toContainText(CLOSED_BLURB);
  });

  test('an account that is not on the list sees "coming soon" on the same deployment',
    async ({ page }) => {
      await signIn(page, uniqueEmail(), CARE_ALLOW_LOG);

      for (const id of REAL_RECORD_TILES) {
        const t = tile(page, id);
        await expect(t.locator('.connector-tag')).toHaveText('coming soon');
        await expect(t).toHaveAttribute('data-soon', '1');
        expect(await t.getAttribute('data-consent')).toBeNull();
      }
      // And the refusal is per account, not per deployment: the same server
      // that just showed live tiles refuses this one, in words.
      await tile(page, 'fasten').click();
      await expect(msgUnder(page, 'fasten')).toHaveText(REFUSED);
      // The synthetic track stays open to everyone.
      await expect(tile(page, 'sample')).toHaveClass(/tier-live/);
    });
});
