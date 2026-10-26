import { test, expect } from '@playwright/test';
import {
  CARE_ALLOW_BASE_URL, CARE_ALLOW_BRIEF_EMAIL, CARE_ALLOW_LOG,
} from '../playwright.config';
import { blockThirdParty, signIn } from './careagents-fixtures';

/**
 * The visit brief on a phone and on paper. A patient hands it to a
 * clinician, so it needs a Print button that works and a print layout with
 * none of the app's own controls in it.
 *
 * The brief needs an assistant, and an assistant needs a real-record
 * connection, so this runs on the allowlist server with its own allowlisted
 * address. One test, one sign-in: a fixed address signed in twice within
 * the 30-second resend wait gets no new code.
 *
 * The records service is pinned to a dead port, so every section reads
 * "could not reach your records". The layout and the print rules do not
 * depend on what the sections hold.
 */

const PHONE = {
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
};

// Its own address, so the sign-in limiter, which keys on X-Real-IP with no
// edge in front, never counts this spec against another (TEST-NET-3,
// RFC 5737).
test.use({ baseURL: CARE_ALLOW_BASE_URL, ...PHONE,
           extraHTTPHeaders: { 'X-Real-IP': '203.0.113.31' } });

type Printed = { printed: number };

test('the brief fits a 375px phone, prints, and prints without controls',
  async ({ page }) => {
    await blockThirdParty(page);
    await signIn(page, CARE_ALLOW_BRIEF_EMAIL, CARE_ALLOW_LOG);

    const version = await page.locator('[data-consent-version]')
      .first().getAttribute('data-consent-version');
    const conn = await page.request.post('/api/connections/direct', {
      data: { consent: true, consent_version: version } });
    expect(conn.ok(), await conn.text()).toBeTruthy();
    const agent = await page.request.post('/api/agents', {
      data: { name: 'Ada', persona: 'direct',
              connection_id: (await conn.json()).id } });
    expect(agent.ok(), await agent.text()).toBeTruthy();
    await page.goto(
      `/brief?agent=${encodeURIComponent((await agent.json()).id)}`);
    await expect(page.locator('h1')).toHaveText('Visit brief');

    // On the phone: no sideways scroll, and the button is there.
    const w = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(w.scroll).toBe(w.client);
    const print = page.locator('#brief-print');
    await expect(print).toBeVisible();

    // QA #1038: no sideways scroll is not enough. A third pill squeezed the
    // title column to 33px at 375 (0 at 320): "Visi / brie" under the
    // button, the subtitle a few letters a line. The title must keep its
    // width and must not sit under the buttons.
    const head = await page.evaluate(() => {
      const h1 = document.querySelector('h1')!.getBoundingClientRect();
      const btn = document.getElementById('brief-print')!
        .getBoundingClientRect();
      const title = document.querySelector('h1')!;
      return { h1w: h1.width, need: title.scrollWidth,
               overlap: !(btn.left >= h1.right || btn.top >= h1.bottom
                          || btn.bottom <= h1.top || btn.right <= h1.left) };
    });
    expect.soft(head.need).toBeLessThanOrEqual(Math.ceil(head.h1w));
    expect.soft(head.h1w).toBeGreaterThanOrEqual(120);
    expect.soft(head.overlap).toBe(false);

    // The browser's dialog is not something a test can drive; that the
    // button calls window.print is.
    await page.evaluate(() => {
      (window as unknown as Printed).printed = 0;
      window.print = () => { (window as unknown as Printed).printed += 1; };
    });
    await print.click();
    expect(await page.evaluate(
      () => (window as unknown as Printed).printed)).toBe(1);

    // On paper: the content stays, the app's controls go, the page is white.
    await page.emulateMedia({ media: 'print' });
    for (const sel of ['#brief-print', '.hub-head-actions', '.topbar',
                       '.chat-feedback']) {
      await expect(page.locator(sel).first()).toBeHidden();
    }
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.locator('.hub-section h2').first()).toBeVisible();
    await expect(page.locator('.footer-fine')).toBeVisible();
    expect(await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor))
      .toBe('rgb(255, 255, 255)');

    // QA #1038: "black on white whatever the screen theme was". A phone in
    // dark mode prints with the dark tokens: careagents.css sets them on
    // :root:not([data-theme="light"]), which outranks brief-print.css's
    // bare :root, so grey-on-white text and a near-black page and cards.
    await page.emulateMedia({ media: 'print', colorScheme: 'dark' });
    const dark = await page.evaluate(() => {
      const css = (sel: string, prop: string) => {
        const el = document.querySelector(sel);
        return el ? getComputedStyle(el).getPropertyValue(prop) : null;
      };
      return {
        html: css('html', 'background-color'),
        body: css('body', 'background-color'),
        sub: css('.hub-sub', 'color'),
        fine: css('.footer-fine', 'color'),
        empty: css('.brief-empty', 'color'),
      };
    });
    expect.soft(dark.body).toBe('rgb(255, 255, 255)');
    expect.soft(['rgb(255, 255, 255)', 'rgba(0, 0, 0, 0)']).toContain(dark.html);
    for (const c of [dark.sub, dark.fine, dark.empty]) {
      // #333 or darker: the light-mode print value of --ink-soft.
      const [r, g, b] = (c ?? '').match(/\d+/g)!.map(Number);
      expect.soft(Math.max(r, g, b), JSON.stringify(dark)).toBeLessThanOrEqual(51);
    }
  });
