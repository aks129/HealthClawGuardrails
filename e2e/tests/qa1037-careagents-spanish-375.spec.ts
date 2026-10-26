/**
 * QA for #1037 at phone width: a Spanish sample-mode turn, its status line
 * and "Esta respuesta no es correcta", and a hostile heading both live and replayed,
 * run by the shipped chat.js AND careagents.css in a real browser at 375px.
 *
 * The fixture mirrors chat.html's structure (.chat-wrap, the sample banner,
 * #log with one replayed bubble written as the template writes it: escaped
 * text inside a <p>, the starters, the composer). No backend: `page.route`
 * answers POST /api/chat with an event stream, as
 * careagents-chat-spanish-heading.spec.ts does. Synthetic text only.
 */

import { test, expect, Page } from '@playwright/test';
import { CARE_BASE_URL } from '../playwright.config';

test.use({
  baseURL: CARE_BASE_URL,
  extraHTTPHeaders: { 'X-Real-IP': '203.0.113.51' },
  viewport: { width: 375, height: 812 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
});

const FIXTURE = `${CARE_BASE_URL}/__qa1037-spanish-375-fixture`;

// What chat.html writes for a replayed answer: the model's text, escaped by
// Jinja, inside one <p>. Two hostile headings and a real one.
const REPLAYED_ESCAPED =
  '### Resumen de sus análisis\nSu A1c de muestra fue 6.1%.\n'
  + '### &lt;img src=x onerror=&quot;window.__pwnedReplay=1&quot;&gt;\n'
  + '# &lt;script&gt;window.__pwnedReplay=2&lt;/script&gt;';

const FIXTURE_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/careagents.css">
<title>qa1037 fixture</title></head>
<body class="page-chat">
<main class="chat-wrap">
  <div class="chat-head"><div class="chat-id"><div><div class="chat-name">Juniper</div></div></div></div>
  <p class="beta-banner sample-banner" role="note">These are made-up records.</p>
  <div class="chat-log" id="log" data-pending-reviews='[]'>
    <div class="chat-resumed">Picking up where you left off</div>
    <div class="msg user"><p>¿Qué dicen mis análisis?</p></div>
    <div class="msg agent"><p>${REPLAYED_ESCAPED}</p></div>
  </div>
  <div class="starters" id="starters" hidden></div>
  <form class="chat-input" id="composer">
    <input id="box" autocomplete="off" maxlength="2000" placeholder="Ask Juniper anything about the records…">
    <button class="btn-primary" id="send" type="submit">Send</button>
  </form>
</main>
<script>window.CARE_AGENT = "agent-qa1037"; window.CARE_CONVERSATION = "c-qa1037";
window.CARE_AGENT_NAME = "Juniper"; window.CARE_WHOSE = "";</script>
<script src="/static/chat.js"></script>
</body></html>`;

function frame(id: number, ev: Record<string, unknown>) {
  return `id: ${id}\ndata: ${JSON.stringify(ev)}\n\n`;
}

// The longest Spanish sample status line, and a long Spanish answer with a
// heading and a hostile heading.
const SAMPLE_LABEL =
  'Graficando los resultados de ejemplo a lo largo del tiempo';
const ANSWER =
  '### Lo que muestran sus análisis de muestra\n'
  + 'Estos registros son inventados. La hemoglobina A1c de muestra fue 6.1% '
  + 'el 1 de marzo, y el colesterol LDL fue 128 mg/dL.\n\n'
  + '### <img src=x onerror="window.__pwnedLive=1">\n'
  + '- Pregunte a su médico qué significa para usted.';

async function open(page: Page) {
  await page.route(FIXTURE, (route) =>
    route.fulfill({ contentType: 'text/html', body: FIXTURE_HTML }));
  await page.route('**/api/chat', (route) => {
    const body = frame(0, { type: 'accepted', run_id: 'run-qa1037',
                            next_cursor: 0 })
      + frame(1, { type: 'lang', lang: 'es' })
      + frame(2, { type: 'tool', name: 'show_lab_timeline',
                   label: SAMPLE_LABEL })
      + frame(3, { type: 'text', text: ANSWER })
      + frame(4, { type: 'done', status: 'completed' });
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
                           headers: { 'X-CareAgents-Run-ID': 'run-qa1037' },
                           body });
  });
  await page.goto(FIXTURE);
}

async function ask(page: Page) {
  await page.locator('#box').fill('¿Cómo han cambiado mis análisis?');
  await page.locator('#send').click();
  await expect(page.locator('#log .flag-open'))
    .toHaveText('Esta respuesta no es correcta', { timeout: 10_000 });
}

test('a replayed hostile heading stays text at 375px', async ({ page }) => {
  await open(page);
  const replayed = page.locator('#log .msg.agent').first();
  await expect(replayed.locator('h3')).toHaveCount(3);
  await expect(replayed.locator('h3').first())
    .toHaveText('Resumen de sus análisis');
  await expect(replayed.locator('h3').nth(1)).toContainText('<img src=x');
  await expect(replayed.locator('h3').nth(2)).toContainText('<script>');
  await expect(page.locator('#log img, #log script')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__pwnedReplay))
    .toBeUndefined();
  // A heading stays at the bubble's own size.
  const sizes = await replayed.evaluate((el) => ({
    bubble: getComputedStyle(el).fontSize,
    heading: getComputedStyle(el.querySelector('h3')!).fontSize,
  }));
  expect(sizes.heading).toBe(sizes.bubble);
});

test('a Spanish sample turn fits a 375px phone', async ({ page }) => {
  await open(page);
  await ask(page);
  await expect(page.locator('#log .chip')).toHaveText(SAMPLE_LABEL);
  const live = page.locator('#log .msg.agent').last();
  await expect(live.locator('h3')).toHaveCount(2);
  await expect(live).not.toContainText('###');
  await expect(page.locator('#log img')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__pwnedLive))
    .toBeUndefined();
  const w = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(w.scroll).toBe(w.client);
  for (const sel of ['#log .chip', '#log .flag-open']) {
    const box = await page.locator(sel).boundingBox();
    expect(box, sel).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  }
  await page.screenshot({
    path: process.env.QA1037_SHOT || 'test-results/qa1037-spanish-375.png',
    fullPage: true });
});

// QA #1037 F2: the sheet behind the Spanish link was all English,
// including "Optional. Please don't include health details." on a note
// that is emailed to the team.
test('the sheet behind "Esta respuesta no es correcta" is in Spanish',
  async ({ page }) => {
  await open(page);
  await ask(page);
  await page.locator('#log .flag-open').click();
  await expect(page.locator('#log .flag-ask')).toBeVisible();
  await expect(page.locator('#log .flag-ask'))
    .not.toHaveText('What was wrong with this answer?', { timeout: 2_000 });
  const placeholder = await page.locator('#log .flag-note')
    .getAttribute('placeholder');
  expect(placeholder).not.toContain("Please don't include health details");
  await expect(page.locator('#log .flag-send')).not.toHaveText('Send');
});
