/**
 * A Spanish turn and a markdown heading, run by the real
 * careagents/static/chat.js in a real browser (live 2026-10-26).
 *
 * A Spanish question got a Spanish answer (#1017) under English status
 * lines and an English "This wasn't right". The server now says the turn is
 * Spanish on the `accepted` event and stores Spanish status lines; this
 * pins the page side: the link follows `accepted`, and the chip shows the
 * label it is sent. An answer's "### " line showed as written; it is now a
 * heading, and a hostile one stays text.
 *
 * No backend, as careagents-chat-503.spec.ts: `page.route` answers the POST
 * with an event stream, and the fixture page loads the shipped script from
 * the app under test. Synthetic text only.
 */

import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';

// Its own address for the app's per-IP limits, though only /static/chat.js
// reaches the app (TEST-NET-3, RFC 5737).
test.use({ baseURL: CARE_BASE_URL,
           extraHTTPHeaders: { 'X-Real-IP': '203.0.113.41' } });

const FIXTURE = `${CARE_BASE_URL}/__e2e-chat-spanish-fixture`;

const FIXTURE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>chat spanish fixture</title></head>
<body>
  <script>window.CARE_AGENT = "agent-e2e"; window.CARE_CONVERSATION = "c-e2e";</script>
  <div id="log"></div>
  <div id="quick"></div>
  <div id="starters"></div>
  <form id="composer"><input id="box"><button id="send" type="submit">Send</button></form>
  <script src="/static/chat.js"></script>
</body></html>`;

function frame(id: number, ev: Record<string, unknown>) {
  return `id: ${id}\ndata: ${JSON.stringify(ev)}\n\n`;
}

/** Serve the fixture; answer POST /api/chat with one finished turn. */
async function answerWith(page: Page, lang: string | null, label: string,
                          text: string) {
  await page.route(FIXTURE, (route) =>
    route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }));
  await page.route('**/api/chat', (route) => {
    const accepted: Record<string, unknown> = {
      type: 'accepted', run_id: 'run-e2e', next_cursor: 0 };
    if (lang) accepted.lang = lang;
    const body = frame(0, accepted)
      + frame(1, { type: 'tool', name: 'get_labs', label })
      + frame(2, { type: 'text', text })
      + frame(3, { type: 'done', status: 'completed' });
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
                           headers: { 'X-CareAgents-Run-ID': 'run-e2e' },
                           body });
  });
  await page.goto(FIXTURE);
}

async function ask(page: Page, text: string) {
  await page.locator('#box').fill(text);
  await page.locator('#send').click();
}

test('a Spanish turn says "This wasn\'t right" in Spanish', async ({ page }) => {
  await answerWith(page, 'es', 'Interpretando sus análisis',
                   'Su A1c fue 6.1% el 1 de marzo.');
  await ask(page, '¿Cuáles son mis resultados de laboratorio?');
  await expect(page.locator('#log .chip'))
    .toHaveText('Interpretando sus análisis', { timeout: 10_000 });
  await expect(page.locator('#log .flag-open'))
    .toHaveText('Esto no estuvo bien', { timeout: 10_000 });
});

test('an English turn keeps "This wasn\'t right"', async ({ page }) => {
  await answerWith(page, null, 'Interpreting your labs',
                   'Your A1c was 6.1% on March 1.');
  await ask(page, 'What are my lab results?');
  await expect(page.locator('#log .flag-open'))
    .toHaveText("This wasn't right", { timeout: 10_000 });
});

test('a heading is laid out, and a hostile one is text', async ({ page }) => {
  await answerWith(page, null, 'Interpreting your labs',
    '### What the Guidelines Say\nScreening starts at 45.\n\n'
    + '### <img src=x onerror="window.__pwned=1">');
  await ask(page, 'When should I get screened?');
  const agent = page.locator('#log .msg.agent');
  await expect(agent.locator('h3')).toHaveCount(2, { timeout: 10_000 });
  await expect(agent.locator('h3').first())
    .toHaveText('What the Guidelines Say');
  await expect(agent).not.toContainText('###');
  await expect(agent.locator('h3').nth(1)).toContainText('<img src=x');
  await expect(agent.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__pwned)).toBeUndefined();
});
