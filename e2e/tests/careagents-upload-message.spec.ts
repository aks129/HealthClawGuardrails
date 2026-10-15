/**
 * What an upload says, and whether it reloads the hub, rendered by the real
 * careagents/static/home.js in a real browser (#948, patient test on #953).
 *
 * The defect: a file that held only the person's details showed its reason,
 * then the hub reloaded about 1.4 s later (it reloaded on `ingested > 0`) and
 * nothing explained what had happened. Now the hub reloads only when the
 * upload added records the card counts, and carries the sentence across the
 * reload; otherwise the sentence stays where it is.
 *
 * No backend: `page.route` answers the upload with the server's contract
 * (tests/test_careagents_upload_count_rule.py pins the Python side), the
 * same way careagents-sync-message.spec.ts answers the poll. Synthetic data.
 */

import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';

test.use({ baseURL: CARE_BASE_URL });

const CONN = 'conn-e2e-upload';
const FIXTURE = `${CARE_BASE_URL}/__e2e-upload-fixture`;

const FIXTURE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>upload fixture</title></head>
<body>
  <p class="hub-notice" id="hub-notice" role="status" aria-live="polite" hidden></p>
  <button id="new-agent-btn" type="button">new agent</button>
  <div class="conn-card">
    <button type="button" class="conn-upload" data-conn="${CONN}">Upload a file</button>
    <p class="conn-refresh-msg" hidden></p>
  </div>
  <div id="consent-modal" hidden>
    <button id="consent-agree" type="button">agree</button>
    <button id="consent-cancel" type="button">cancel</button>
  </div>
  <input type="file" id="upload-file" hidden>
  <script src="/static/home.js"></script>
</body></html>`;

// Longer than the old 1.4 s reload, with room to spare.
const PAST_OLD_RELOAD = 3_000;

interface Fixture { loads(): number; }

async function uploadWith(page: Page, body: Record<string, unknown>)
    : Promise<Fixture> {
  let loads = 0;
  await page.route(FIXTURE, (route) => {
    loads += 1;
    return route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML });
  });
  await page.route(`**/api/connections/${CONN}/upload`, (route) =>
    route.fulfill({ contentType: 'application/json',
                    body: JSON.stringify(body) }));
  await page.goto(FIXTURE);
  const chooser = page.waitForEvent('filechooser');
  await page.locator('.conn-upload').click();
  await (await chooser).setFiles({
    name: 'records.json', mimeType: 'application/fhir+json',
    buffer: Buffer.from('{"resourceType":"Bundle","type":"collection"}'),
  });
  return { loads: () => loads };
}

test.describe('what an upload reports', () => {
  test('personal details only: the reason stays, and nothing reloads',
       async ({ page }) => {
    const f = await uploadWith(page, {
      entries: 1, ingested: 1, skipped: 0, failed: 0, errors: [],
      connection_id: CONN, records_added: 0, documents_added: 0,
      uncounted_note: 'This file had personal details but no health records.',
    });
    const msg = page.locator('.conn-refresh-msg');
    const line = 'No new records added. This file had personal details ' +
                 'but no health records.';
    await expect(msg).toHaveText(line);
    await page.waitForTimeout(PAST_OLD_RELOAD);
    await expect(msg).toHaveText(line);
    expect(f.loads()).toBe(1);
  });

  test('records added: the hub reloads and says so after the reload',
       async ({ page }) => {
    const f = await uploadWith(page, {
      entries: 4, ingested: 4, skipped: 0, failed: 0, errors: [],
      connection_id: CONN, records_added: 3, documents_added: 0,
    });
    await expect.poll(() => f.loads()).toBe(2);
    await expect(page.locator('#hub-notice')).toHaveText(
      '3 new records added.');
  });
});
