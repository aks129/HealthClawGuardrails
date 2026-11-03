/**
 * Security: for the paths below, the check-in wallet sends records only
 * after the person taps Share and then Send (V3: the person approves).
 *
 * Why this exists: a connectathon run against the public Testing EHR
 * recorded a "fulfilled" answer to its no-selector case (one item, no
 * selector, so "anything on file") while the tester's Playwright command to
 * click Share and Send errored on a wallet tab that had already closed. The
 * question was whether the wallet can answer with records on its own. Each
 * test below tries one way it might, and counts the raw
 * `digital-credentials/web-wallet/response` messages the opener receives:
 * that count, not the Verifier's result, is the evidence that nothing left.
 * A negative count is read only after a canary (a response-shaped message
 * posted from the wallet tab itself) has shown the counter is listening.
 *
 * Paths tried: a review left untouched for longer than the 24 s observed;
 * Share chosen but Send never tapped; the tab closed mid-review with Share
 * chosen; a reload, and a back-and-forward, mid-review with Share chosen; a
 * second, third and fourth request from the opener to the same tab; two
 * clicks on Send; Send with the defaults. And the tester's error,
 * reproduced: Share, Send, then any further step on the wallet tab throws
 * "closed", because the Verifier closes the tab the moment the answer
 * arrives, while the Verifier holds a fulfilled answer.
 *
 * Two ways to run it. By default the records relay is answered here by
 * route with the synthetic test patient's export, as
 * careagents-checkin-wallet.spec.ts does, so it needs no engine; the test
 * fails if the relay route was not the one that answered. With
 * CHECKIN_NO_SEND_LIVE=1 nothing is routed: CareAgents asks a real engine,
 * the fresh account has no records, and the test takes the connectathon
 * test patient (CARE_CHECKIN_TEST_PATIENT=1 on that server).
 *
 * Mutation testing: CHECKIN_WALLET_MUTANT (a file served in place of
 * careagents/static/checkin_wallet.js) and CHECKIN_WALLET_LIB_MUTANT (in
 * place of the vendored wallet.js) let a mutant run in the real browser
 * without touching the files on disk. Unset, they do nothing. When set, a
 * test fails unless the mutant was the file served.
 *
 * Synthetic data only; the check-in page is a local one, never the public
 * Testing EHR.
 */

import { test, expect, Page, BrowserContext } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { codeFromLog, uniqueEmail } from './careagents-fixtures';

test.use({ baseURL: CARE_BASE_URL });

const LIVE = process.env.CHECKIN_NO_SEND_LIVE === '1';
const RESPONSE = 'digital-credentials/web-wallet/response';
const REQUEST_TYPE = 'digital-credentials/web-wallet/request';
const READY = 'digital-credentials/web-wallet/ready';

const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));

const MUTANTS: Array<[string, string | undefined]> = [
  ['/static/checkin_wallet.js', process.env.CHECKIN_WALLET_MUTANT],
  ['/static/vendor/smart-health-checkin-client/0.4.4/wallet.js',
    process.env.CHECKIN_WALLET_LIB_MUTANT],
];

// The Testing EHR's no-selector case (connectathon requests/no-selector.json),
// with an id of our own.
const NO_SELECTOR = {
  type: 'smart-health-checkin-request', version: '1', id: 'no-send-no-selector',
  purpose: 'Pre-visit check-in', fhirVersions: ['4.0.1'],
  items: [{
    id: 'anything', title: "Anything you'd like your doctor to see",
    summary: 'Share whatever you think is relevant to this visit.',
    content: { kind: 'selection.fhir' }, accept: ['application/fhir+json'],
  }],
};
const US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/';
const item = (id: string, profile: string) => ({
  id, title: id, content: { kind: 'selection.fhir', profiles: [US_CORE + profile] },
  accept: ['application/fhir+json'],
});
const RECORDS = {
  type: 'smart-health-checkin-request', version: '1', id: 'no-send-records',
  fhirVersions: ['4.0.1'],
  items: [item('patient', 'us-core-patient'),
    item('problems', 'us-core-condition-problems-health-concerns'),
    item('medications', 'us-core-medicationrequest'),
    item('immunizations', 'us-core-immunization'),
    item('coverage', 'us-core-coverage'),
    { id: 'anything', title: 'Anything', content: { kind: 'selection.fhir' },
      accept: ['application/fhir+json'] }],
};

// A check-in page on another origin, with the organizers' Verifier library.
// It counts every raw wallet response and every `ready` it receives, and
// keeps the wallet's window handle (window.open wrapped) so a test can post
// to it as the opener.
const VERIFIER = 'http://127.0.0.1:5398';
const CHECKIN_JS = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'smart-health-checkin-client', '0.4.4', 'checkin.js'));
const VERIFIER_HTML = `<!doctype html><meta charset="utf-8"><title>verifier</title>
<button id="go">check in</button>
<script>
window.__raw = [];
window.__ready = 0;
window.addEventListener('message', (e) => {
  const d = e.data;
  if (d && d.type === ${JSON.stringify(RESPONSE)})
    window.__raw.push({ requestId: d.requestId, outcome: d.outcome, origin: e.origin });
  if (d && d.type === ${JSON.stringify(READY)}) window.__ready += 1;
});
const open = window.open;
window.open = function (...a) { const w = open.apply(this, a); window.__w = w; return w; };
</script>
<script type="module">
import { runCheckin, webWallet } from '/checkin.js';
document.getElementById('go').onclick = () => {
  const wallet = webWallet({ id: 'no-send-wallet', name: 'no-send-wallet',
    walletUrl: ${JSON.stringify(`${CARE_BASE_URL}/checkin/wallet`)} });
  window.__t0 = performance.now();
  runCheckin(window.__req, { wallet }).then((r) => {
    window.__ms = Math.round(performance.now() - window.__t0);
    window.__result = { status: r.status, json: r.response ? r.response.json : null,
      error: r.error ? r.error.message : null };
  }, (e) => { window.__result = { status: 'threw', error: String(e) }; });
};
</script>`;

/** What the context's routes answered, for the positive checks. */
type Served = { relay: number; mutants: number };

/** Every route on the context, none on a page. A page route outranks a
 *  context route, and one that calls continue() skips the context's routes
 *  altogether: that is how a page-level "block third parties" route once
 *  sent this spec's relay to the real server. Context routes registered
 *  later win, so the blocker is registered first. */
async function routes(context: BrowserContext): Promise<Served> {
  const served: Served = { relay: 0, mutants: 0 };
  await context.route('**/*', (route) => {
    const url = route.request().url();
    if (!url.startsWith('http')) return route.continue();
    const host = new URL(url).hostname;
    return host === 'localhost' || host === '127.0.0.1'
      ? route.continue() : route.abort();
  });
  await context.route(`${VERIFIER}/**`, (route) =>
    new URL(route.request().url()).pathname === '/checkin.js'
      ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
      : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML }));
  if (!LIVE) {
    await context.route(`${CARE_BASE_URL}/checkin/wallet/records`, (route) => {
      served.relay += 1;
      return route.fulfill({ status: 200, contentType: 'application/fhir+json',
        headers: { 'Cache-Control': 'no-store' }, body: JSON.stringify(EXPORT) });
    });
  }
  for (const [where, file] of MUTANTS) {
    if (!file) continue;
    const body = fs.readFileSync(file);
    await context.route((u) => u.origin === new URL(CARE_BASE_URL).origin &&
      u.pathname === where, (route) => {
      served.mutants += 1;
      return route.fulfill({ status: 200, contentType: 'text/javascript', body,
        headers: { 'Cache-Control': 'no-store' } });
    });
  }
  return served;
}

/** One sign-in address per test and retry. The server allows 30 code
 *  requests per client address per 10 minutes, keyed on X-Real-IP with no
 *  edge in front; one shared address for ten tests and their CI retries
 *  sits at that limit. TEST-NET-1 (RFC 5737), clear of the other specs. */
function signInAddress(): string {
  const info = test.info();
  return `192.0.2.${100 + (info.title.length * 7 + info.retry) % 150}`;
}

/** The check-in page, its wallet tab signed in, at the review. */
async function toReview(page: Page, req: unknown) {
  const context = page.context();
  await context.setExtraHTTPHeaders({ 'X-Real-IP': signInAddress() });
  const served = await routes(context);
  // Every request message the wallet tab receives, kept for replay.
  await context.addInitScript((t) => {
    (window as any).__reqs = [];
    window.addEventListener('message', (e) => {
      if (e.data && e.data.type === t) (window as any).__reqs.push(e.data);
    });
  }, REQUEST_TYPE);
  await page.goto(`${VERIFIER}/`);
  await page.evaluate((r) => { (window as any).__req = r; }, req);
  const [wallet] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#go').click()]);
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
  await wallet.locator('#signin-verify').click();
  const review = wallet.locator('#wallet-review');
  const offer = wallet.locator('#wallet-test-patient');
  await expect(wallet.locator('#wallet-review:visible, #wallet-test-patient:visible'))
    .toHaveCount(1, { timeout: 20_000 });
  if (await offer.isVisible()) {
    // Only the live relay says "no records"; the route never does.
    expect(LIVE, 'the relay route did not answer: the real server did').toBe(true);
    // No records on a fresh account: the made-up patient, as a visiting
    // team would choose it.
    await wallet.locator('#wallet-test-patient-use').click();
    await expect(review).toBeVisible({ timeout: 30_000 });
  }
  if (!LIVE) expect(served.relay, 'the relay route answered').toBeGreaterThan(0);
  if (MUTANTS.some(([, f]) => f)) {
    expect(served.mutants, 'the mutant was the file served').toBeGreaterThan(0);
  }
  return wallet;
}

const raw = (page: Page) => page.evaluate(() => (window as any).__raw);
const result = (page: Page) => page.evaluate(() => (window as any).__result);
const ready = (page: Page) => page.evaluate(() => (window as any).__ready);
/** The wallet's real answers: everything counted but the canary. */
const sent = async (page: Page) =>
  (await raw(page)).filter((r: any) => r.requestId !== 'canary');

/** Positive control for a zero count: a response-shaped message posted from
 *  the wallet tab reaches the opener's counter. The Verifier library
 *  ignores it (its requestId is not the request's). */
async function canary(page: Page, wallet: Page) {
  await wallet.evaluate((t) => window.opener.postMessage(
    { type: t, requestId: 'canary', outcome: 'canary' }, '*'), RESPONSE);
  await expect.poll(() => raw(page)).toContainEqual(
    expect.objectContaining({ requestId: 'canary' }));
}

const share = (wallet: Page) =>
  wallet.locator('input[name="choice-0"][value="share"]');

test.describe('check-in wallet: no send without Share', () => {
  test('every item starts at "Don\'t share", nothing picked', async ({ page }) => {
    const wallet = await toReview(page, RECORDS);
    const shares = wallet.locator('#wallet-items input[value="share"]');
    const declines = wallet.locator('#wallet-items input[value="decline"]');
    expect(await shares.count()).toBeGreaterThan(0);
    expect(await shares.count()).toBe(await declines.count());
    for (const s of await shares.all()) await expect(s).not.toBeChecked();
    for (const d of await declines.all()) await expect(d).toBeChecked();
    expect(await wallet.locator('.checkin-choice[data-picked]').count()).toBe(0);
  });

  test('a review left untouched for 30 s sends nothing', async ({ page }) => {
    test.setTimeout(90_000);
    const wallet = await toReview(page, NO_SELECTOR);
    await page.waitForTimeout(30_000);
    expect(wallet.isClosed()).toBe(false);
    await expect(wallet.locator('#wallet-review')).toBeVisible();
    await canary(page, wallet);
    expect(await sent(page)).toEqual([]);
    expect(await result(page)).toBeUndefined();
  });

  test('Share chosen, Send never tapped, sends nothing', async ({ page }) => {
    const wallet = await toReview(page, NO_SELECTOR);
    await share(wallet).check();
    await expect(share(wallet)).toBeChecked();
    await page.waitForTimeout(5_000);
    await canary(page, wallet);
    expect(await sent(page)).toEqual([]);
    expect(await result(page)).toBeUndefined();
  });

  test('closing the tab mid-review, Share chosen, sends nothing', async ({ page }) => {
    const wallet = await toReview(page, NO_SELECTOR);
    await share(wallet).check();
    await canary(page, wallet);
    await wallet.close();
    await expect.poll(() => result(page), { timeout: 5_000 }).toBeTruthy();
    const r = await result(page);
    // The Verifier library reads a closed tab as a decline; no answer came.
    expect(r.status).toBe('declined');
    expect(r.json).toBeNull();
    expect(await sent(page)).toEqual([]);
  });

  test('a reload mid-review, Share chosen, sends nothing and asks nothing again',
    async ({ page }) => {
      const wallet = await toReview(page, NO_SELECTOR);
      await share(wallet).check();
      expect(await ready(page)).toBe(1);
      await wallet.reload();
      // The reloaded tab is signed in and has told its opener it is ready
      // again; only then is "no request, no review" a finding.
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      await expect.poll(() => ready(page)).toBe(2);
      await page.waitForTimeout(3_000);
      // The Verifier sends its request once; the reloaded tab never gets one.
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      await expect(wallet.locator('#wallet-review')).toBeHidden();
      expect(await wallet.evaluate(() => (window as any).__reqs.length)).toBe(0);
      await canary(page, wallet);
      expect(await sent(page)).toEqual([]);
    });

  test('back and forward mid-review, Share chosen, sends nothing',
    async ({ page }) => {
      const wallet = await toReview(page, NO_SELECTOR);
      await share(wallet).check();
      await wallet.goto(`${CARE_BASE_URL}/`);
      await wallet.goBack();
      await expect(wallet).toHaveURL(`${CARE_BASE_URL}/checkin/wallet`);
      await page.waitForTimeout(3_000);
      await canary(page, wallet);
      expect(await sent(page)).toEqual([]);
      expect(await result(page)).toBeUndefined();
      // Whatever state came back, it waits for a tap: recorded, not asserted.
      const shown = await wallet.locator('#wallet-review').isVisible();
      test.info().annotations.push({ type: 'back-forward',
        description: `review visible after back: ${shown}` });
    });

  test('Send with the defaults answers declined, with no records', async ({ page }) => {
    const wallet = await toReview(page, NO_SELECTOR);
    await wallet.locator('#wallet-send').click();
    await expect.poll(() => result(page), { timeout: 10_000 }).toBeTruthy();
    const r = await result(page);
    expect(r.status).toBe('completed');
    expect(r.json.artifacts).toEqual([]);
    expect(r.json.requestStatus).toEqual([{ item: 'anything', status: 'declined' }]);
    expect((await sent(page)).length).toBe(1);
  });

  test('one Share answers one request: later requests to the tab are dropped',
    async ({ page }) => {
      const wallet = await toReview(page, NO_SELECTOR);
      const got = await wallet.evaluate(() => (window as any).__reqs);
      expect(got.length).toBe(1);
      const first = got[0];
      const post = (id: string) => page.evaluate(({ m, origin }) =>
        (window as any).__w.postMessage(m, origin),
      { m: { ...first, requestId: id }, origin: CARE_BASE_URL });
      await post('second-during-review');
      await share(wallet).check();
      await post('third-before-send');
      await wallet.locator('#wallet-send').click();
      await post('fourth-while-sending');
      // All four reached the tab (it closes about 1.5 s after Send).
      await expect.poll(() => wallet.evaluate(() =>
        (window as any).__reqs.map((r: any) => r.requestId)), { timeout: 1_000 })
        .toEqual([first.requestId, 'second-during-review', 'third-before-send',
          'fourth-while-sending']);
      await expect.poll(() => result(page), { timeout: 10_000 }).toBeTruthy();
      await page.waitForTimeout(1_000);
      // ...and only the first was answered.
      expect(await raw(page)).toEqual([{ requestId: first.requestId,
        outcome: 'approved', origin: new URL(CARE_BASE_URL).origin }]);
      expect((await result(page)).json.requestStatus)
        .toEqual([{ item: 'anything', status: 'fulfilled' }]);
    });

  test('two clicks on Send answer once', async ({ page }) => {
    const wallet = await toReview(page, NO_SELECTOR);
    await share(wallet).check();
    // Two click events, counted where the button receives them, so a pass
    // cannot mean the second one never landed.
    const clicks = await wallet.evaluate(() => {
      const send = document.getElementById('wallet-send')!;
      let n = 0;
      send.addEventListener('click', () => { n += 1; });
      send.click();
      send.click();
      return n;
    });
    expect(clicks).toBe(2);
    await expect.poll(() => result(page), { timeout: 10_000 }).toBeTruthy();
    await page.waitForTimeout(1_000);
    expect((await raw(page)).length).toBe(1);
  });

  test('the tester\'s error: Share and Send went through, then the tab was gone',
    async ({ page }) => {
      const wallet = await toReview(page, NO_SELECTOR);
      const reviewAt = await page.evaluate(() =>
        Math.round(performance.now() - (window as any).__t0));
      await share(wallet).check();
      const sentAt = Date.now();
      await wallet.locator('#wallet-send').click();
      // "Sending" is on screen for a moment, then the answer goes and the
      // Verifier closes the tab at once.
      await wallet.waitForEvent('close', { timeout: 10_000 });
      const closedAfter = Date.now() - sentAt;
      // Any next step on the wallet tab, as the tester's command took, fails.
      await expect(wallet.title()).rejects.toThrow(/closed/i);
      const r = await result(page);
      expect(r.status).toBe('completed');
      expect(r.json.requestStatus).toEqual([{ item: 'anything', status: 'fulfilled' }]);
      expect((await raw(page)).length).toBe(1);
      const ms = await page.evaluate(() => (window as any).__ms);
      test.info().annotations.push({ type: 'timing', description:
        `review shown at ${reviewAt} ms; Send to tab closed ${closedAfter} ms; ` +
        `check-in start to answer ${ms} ms` });
    });
});
