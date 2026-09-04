/**
 * The four outcomes of a post-refresh sync poll, rendered by the real
 * careagents/static/home.js in a real browser (#226).
 *
 * Why this file exists: the branch selection in `watchForNewRecords` was
 * verified by inspection, and inspection is what missed the defect it now
 * covers. `home.js` gated its entire render on `new_records > 0`, so a
 * refresh that delivered only DocumentReferences — ingested, uncountable,
 * unreadable — displayed NOTHING. A person who had just watched a sync run
 * could not tell that from a sync that did nothing.
 *
 * Why it needs no backend: `page.route` fulfils both calls the journey makes
 * (the refresh POST and the poll GET), so the four bodies below are the
 * contract `careagents/app.py::poll_connection` is tested against on the
 * Python side. What runs here is the shipped JavaScript, served by the app
 * under test at /static/home.js — not a copy, not a port of the rule.
 *
 * The fixture page carries only the markup `home.js` touches. It is loaded
 * over a routed same-origin URL so the script's own <script src> resolves to
 * the real file. `#new-agent-btn` is present because home.js dereferences it
 * unguarded at module scope; without it the IIFE throws before the assertions
 * mean anything.
 */

import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';

test.use({ baseURL: CARE_BASE_URL });

const TENANT = 'care-e2e-tenant';
const CONN = 'conn-e2e-1';
const FIXTURE = `${CARE_BASE_URL}/__e2e-sync-fixture`;

const FIXTURE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>sync fixture</title></head>
<body>
  <button id="new-agent-btn" type="button">new agent</button>
  <div class="conn-card" data-tenant="${TENANT}">
    <button class="conn-refresh" type="button" data-conn="${CONN}">Refresh</button>
    <p class="conn-refresh-msg" hidden></p>
  </div>
  <div id="consent-modal" hidden>
    <button id="consent-agree" type="button">agree</button>
    <button id="consent-cancel" type="button">cancel</button>
  </div>
  <script src="/static/home.js"></script>
</body></html>`;

// `watchForNewRecords` polls on a 5s interval, so the first answer lands just
// past Playwright's 5s default. Every assertion below must outlast a tick, or
// it races the thing it is measuring.
const TICK = 15_000;

/** Counts poll requests so a test can prove the poll actually happened. */
interface Watch { polls(): number; }

/**
 * Serve the fixture, answer the refresh with a reauth_url (the only branch
 * that starts the watch), and answer every poll with `body`.
 */
async function watchWith(page: Page, body: Record<string, unknown>,
                         status = 200): Promise<Watch> {
  let polls = 0;
  await page.route(FIXTURE, (route) =>
    route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }));

  // The handler opens this in a new tab; keep it same-origin and trivial so
  // no third party is contacted and no popup stalls the run.
  await page.route('**/__e2e-reauth', (route) =>
    route.fulfill({ contentType: 'text/html', body: '<html></html>' }));

  await page.route(`**/api/connections/${CONN}/refresh`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ status: 'reauth', connection_id: CONN,
                             reauth_url: `${CARE_BASE_URL}/__e2e-reauth` }),
    }));

  await page.route(`**/api/connections/${TENANT}/poll`, (route) => {
    polls += 1;
    return route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.goto(FIXTURE);
  await page.locator('.conn-refresh').click();
  return { polls: () => polls };
}

const message = (page: Page) => page.locator('.conn-refresh-msg');

test.describe('what a refresh reports when the poll comes back', () => {
  test('readable records: the count, with the standing document caveat', async ({
    page,
  }) => {
    await watchWith(page, {
      status: 'active', record_count: 105, new_records: 5,
      uncounted_note: 'Notes and documents are not yet readable here.',
    });
    await expect(message(page)).toHaveText(
      '5 new records added. Notes and documents are not yet readable here.',
      { timeout: TICK });
  });

  test('readable records only: the count alone', async ({ page }) => {
    await watchWith(page, {
      status: 'active', record_count: 105, new_records: 5,
    });
    await expect(message(page)).toHaveText('5 new records added.',
                                           { timeout: TICK });
  });

  test('documents only: says so instead of saying nothing', async ({ page }) => {
    // The defect this file was written for. Before the fix the poll returned
    // exactly this body and the page rendered the previous "Finish signing
    // in…" text forever.
    await watchWith(page, {
      status: 'active', record_count: 100, new_records: 0,
      uncounted_note:
        'Notes and documents arrived, and they are not readable here yet.',
    });
    await expect(message(page)).toHaveText(
      'No new records you can read. Notes and documents arrived, and they ' +
      'are not readable here yet.', { timeout: TICK });
  });

  test('nothing at all: the message does not change', async ({ page }) => {
    // A refresh that genuinely brought nothing stays quiet — the watcher is
    // still running, so the "Finish signing in" text set by the click must
    // survive a zero-and-zero poll rather than being overwritten.
    const watch = await watchWith(page, {
      status: 'active', record_count: 100, new_records: 0,
    });
    // Prove the poll ACTUALLY RAN before concluding it stayed quiet —
    // otherwise this passes on a route that never fired, which is how a
    // "quiet" assertion becomes decoration.
    await expect.poll(() => watch.polls(), { timeout: TICK })
      .toBeGreaterThan(0);
    await expect(message(page)).toHaveText(
      'Finish signing in to your provider — new records appear here.');
  });

  test('the probe failed: the count stands and names what it could not check',
    async ({ page }) => {
      await watchWith(page, {
        status: 'active', record_count: 105, new_records: 5,
        uncounted_note:
          'We could not check whether notes or documents were left out.',
      });
      await expect(message(page)).toHaveText(
        '5 new records added. We could not check whether notes or documents ' +
        'were left out.', { timeout: TICK });
    });

  test('an unreachable record store is reported, not rendered as nothing new',
    async ({ page }) => {
      // Pre-existing behaviour, pinned here because the branch above it moved.
      await watchWith(page, {
        status: 'unavailable', error: 'records_unavailable',
        message: "We couldn't reach your records right now.",
      }, 503);
      await expect(message(page)).toHaveText(
        "We couldn't reach your records right now.", { timeout: TICK });
    });
});

test.describe('the watch keeps running until readable records land', () => {
  test('a document-only poll upgrades to the count when records arrive',
    async ({ page }) => {
      // The interim message must not freeze: `clearInterval` fires only on a
      // readable record, so a later poll replaces it.
      //
      // MUTATION: call clearInterval unconditionally -> red, the message
      // stays on the document-only sentence.
      let polls = 0;
      await page.route(FIXTURE, (route) =>
        route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }));
      await page.route('**/__e2e-reauth', (route) =>
        route.fulfill({ contentType: 'text/html', body: '<html></html>' }));
      await page.route(`**/api/connections/${CONN}/refresh`, (route) =>
        route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({ reauth_url: `${CARE_BASE_URL}/__e2e-reauth` }),
        }));
      await page.route(`**/api/connections/${TENANT}/poll`, (route) => {
        polls += 1;
        const body = polls === 1
          ? { status: 'active', record_count: 100, new_records: 0,
              uncounted_note:
                'Notes and documents arrived, and they are not readable here yet.' }
          : { status: 'active', record_count: 105, new_records: 5 };
        return route.fulfill({ contentType: 'application/json',
                               body: JSON.stringify(body) });
      });

      await page.goto(FIXTURE);
      await page.locator('.conn-refresh').click();

      await expect(message(page)).toHaveText(
        'No new records you can read. Notes and documents arrived, and they ' +
        'are not readable here yet.', { timeout: TICK });
      // The poller ticks every 5s; the second answer replaces the first.
      await expect(message(page)).toHaveText('5 new records added.',
                                             { timeout: TICK });
    });
});
