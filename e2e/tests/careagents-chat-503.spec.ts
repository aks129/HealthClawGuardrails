/**
 * What the chat page says when /api/chat refuses a turn with a 503
 * (#1017), run by the real careagents/static/chat.js in a real browser.
 *
 * Why this file exists: the Python side pins chat.js's 503 branch by its
 * source text, and two mutations of it survived (the sentence never kept,
 * or cleared before the retry). The server's 503 body always carries a 911
 * line (careagents/beta.py with_911). An ordinary 503 is retried silently,
 * so a blip stays invisible, and its sentence is shown once the retries run
 * out instead of "Connection hiccup". A 503 marked `emergency` (crisis
 * wording) is shown at once, with no retry.
 *
 * Why it needs no backend: `page.route` answers the POST, so no account,
 * agent or HealthClaw is involved and nothing signs in. The fixture page
 * carries only the markup chat.js touches at module scope, and loads the
 * shipped script from the app under test at /static/chat.js.
 */

import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';

test.use({ baseURL: CARE_BASE_URL });

const FIXTURE = `${CARE_BASE_URL}/__e2e-chat-503-fixture`;

const FIXTURE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>chat 503 fixture</title></head>
<body>
  <script>window.CARE_AGENT = "agent-e2e"; window.CARE_CONVERSATION = "c-e2e";</script>
  <div id="log"></div>
  <div id="quick"></div>
  <div id="starters"></div>
  <form id="composer"><input id="box"><button id="send" type="submit">Send</button></form>
  <script src="/static/chat.js"></script>
</body></html>`;

// Synthetic sentences, as app.py's _with_emergency builds them.
const TRAILER =
  'If this is an emergency, call 911. Si es una emergencia, llame al 911.';
const UNAVAILABLE = 'Chat is temporarily unavailable. Try again soon.';
const LEAD =
  'If this is an emergency, call 911 now. Si es una emergencia, llame al 911 ahora.';

/** Serve the fixture; answer every POST /api/chat with `body` as a 503. */
async function refuseWith(page: Page, body: Record<string, unknown>) {
  const posts: number[] = [];
  await page.route(FIXTURE, (route) =>
    route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }));
  await page.route('**/api/chat', (route) => {
    posts.push(Date.now());
    return route.fulfill({ status: 503, contentType: 'application/json',
                           body: JSON.stringify(body) });
  });
  await page.goto(FIXTURE);
  return posts;
}

async function ask(page: Page, text: string) {
  await page.locator('#box').fill(text);
  await page.locator('#send').click();
}

test('an ordinary 503 is retried, then its sentence and the 911 line show',
  async ({ page }) => {
    const posts = await refuseWith(page, {
      error: 'run_workers_unavailable',
      message: `${UNAVAILABLE}\n\n${TRAILER}`,
    });
    await ask(page, 'how are my labs?');
    const agent = page.locator('#log .msg.agent');
    // Retried before anything is said: six POSTs, 400ms apart.
    await expect.poll(() => posts.length, { timeout: 10_000 }).toBe(6);
    await expect(agent).toHaveCount(1, { timeout: 10_000 });
    await expect(agent).toContainText(UNAVAILABLE, { timeout: 10_000 });
    await expect(agent).toContainText('call 911', { timeout: 10_000 });
    await expect(agent).toContainText('llame al 911', { timeout: 10_000 });
    await expect(page.locator('#log')).not.toContainText('Connection hiccup');
  });

test('an emergency 503 is shown at once, with no retry', async ({ page }) => {
  const posts = await refuseWith(page, {
    error: 'run_workers_unavailable',
    emergency: true,
    message: `${LEAD}\n\n${UNAVAILABLE}`,
  });
  await ask(page, 'me duele el pecho');
  const agent = page.locator('#log .msg.agent');
  await expect(agent).toContainText('call 911 now', { timeout: 5_000 });
  await expect(agent).toContainText(UNAVAILABLE, { timeout: 5_000 });
  // Give a retry time to happen; there must be none.
  await page.waitForTimeout(1_500);
  expect(posts.length).toBe(1);
});

test('a 503 with no sentence ends in the page\'s own line, with 911',
  async ({ page }) => {
    await refuseWith(page, { error: 'run_workers_unavailable' });
    await ask(page, 'hello');
    const agent = page.locator('#log .msg.agent');
    await expect(agent).toContainText('Connection hiccup', { timeout: 10_000 });
    await expect(agent).toContainText('call 911', { timeout: 10_000 });
  });
