// b6 + b7 recording: real CareAgents UI on the local stack (web :8600,
// engine :5099, worker). Every step waits on what it shows, so a recording
// only exists if the journey really ran.
//
//   node record_careagents.mjs <phase>   (playwright from <repo>/e2e/node_modules)
//
// phase "setup"  sign in by email code (from the dev mail-stub log), connect
//                the sample records, create an agent; saves storage state and
//                ids. Screenshots the sign-in page. Not video-recorded.
// phase "b6"     approvals ("Waiting for you") -> review -> Approve -> done;
//                downloads the delivered PDF. Screencast-recorded.
// phase "b7"     chat: asks "What do my blood test results say?" and waits
//                for the answer. Screencast-recorded.
//
// Playwright draws no cursor, so every click is logged with its timestamp
// and bounding box to <phase>_marks.json; the edit draws the highlight.
import { createRequire } from 'module';
const require = createRequire(path.join(process.env.HC_REPO_ROOT || process.cwd(), 'e2e', 'package.json'));
const { chromium } = require('playwright');
import fs from 'fs';
import path from 'path';

const V = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const CAP = path.join(V, 'capture');
const BASE = 'http://127.0.0.1:8600';
const LOG = path.join(V, 'run', 'careagents.log');
const STATE = path.join(CAP, 'state.json');
const IDS = path.join(CAP, 'ids.json');
// A 1280x720 CSS viewport at deviceScaleFactor 2: the UI lays out as it
// does on a laptop, and every frame is a 2560x1440 device-pixel render that
// the edit downscales to 1080p, so text stays sharp.
const VIEW = { width: 1280, height: 720 };
const phase = process.argv[2];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function codeFromLog(email) {
  const re = new RegExp(`DEV email — [^\\n]* for ${email.replace(/[.+]/g, '\\$&')}: (\\d{8})`);
  for (let i = 0; i < 50; i++) {
    const m = fs.existsSync(LOG) && fs.readFileSync(LOG, 'utf8').match(re);
    if (m) return m[1];
    await sleep(200);
  }
  throw new Error('no sign-in code in the log');
}

function recorder(page) {
  const t0 = Date.now();
  const marks = [];
  return {
    t0, marks,
    async mark(name, locator) {
      const e = { name, t: (Date.now() - t0) / 1000 };
      if (locator) e.box = await locator.boundingBox();
      marks.push(e);
      console.log(`${e.t.toFixed(2)}s ${name}`);
    },
  };
}

async function setup(browser) {
  const ctx = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const email = `demo-${Date.now()}@example.test`;
  await page.goto(`${BASE}/auth`);
  await page.locator('#email').fill(email);
  await page.screenshot({ path: path.join(CAP, 'signin.png') });
  await page.locator('#email-btn').click();
  await page.locator('#step-code').waitFor();
  await page.locator('#code').fill(await codeFromLog(email));
  await page.locator('#verify-btn').click();
  if (await page.evaluate(() => !!window.PublicKeyCredential)) {
    await page.locator('#step-passkey').waitFor();
    await page.locator('#skip-passkey-btn').click();
  }
  await page.waitForURL(/\/home$/);
  const post = (url, body) => page.evaluate(async ([u, b]) => {
    const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                               body: JSON.stringify(b || {}) });
    return { status: r.status, body: await r.json() };
  }, [url, body]);
  const conn = await post('/api/connections/sample');
  if (conn.status !== 200) throw new Error('sample connect: ' + JSON.stringify(conn));
  const agent = await post('/api/agents', { name: 'Juniper', persona: 'calm',
                                            connection_id: conn.body.id });
  if (agent.status !== 200) throw new Error('agent: ' + JSON.stringify(agent));
  await page.goto(`${BASE}/home`);
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: path.join(CAP, 'home.png') });
  await ctx.storageState({ path: STATE });
  fs.writeFileSync(IDS, JSON.stringify({ email, connection: conn.body.id,
                                         agent: agent.body.id }, null, 2));
  console.log('ids', conn.body.id, agent.body.id);
  await ctx.close();
}

// Recording. Playwright's recordVideo captures CSS pixels (a 1280x720
// viewport records at 1280x720 whatever the deviceScaleFactor, and a larger
// `size` only pads it), so it cannot give a sharp 1080p picture of a UI laid
// out at 1280. This uses the same Chromium screencast Playwright uses, asked
// for device pixels: every frame is a 2560x1440 JPEG with its own timestamp,
// written to <phase>_frames/ with an ffconcat list. Nothing is drawn on it.
async function recordedContext(browser, name) {
  const dir = path.join(CAP, `${name}_frames`);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const ctx = await browser.newContext({
    viewport: VIEW, deviceScaleFactor: 2, storageState: STATE,
  });
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  cdp.on('Page.screencastFrame', async (f) => {
    const file = `f${String(frames.length).padStart(5, '0')}.jpg`;
    fs.writeFileSync(path.join(dir, file), Buffer.from(f.data, 'base64'));
    frames.push({ file, ts: f.metadata.timestamp });
    try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch {}
  });
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92,
    maxWidth: VIEW.width * 2, maxHeight: VIEW.height * 2, everyNthFrame: 1 });
  return { ctx, page, dir, cdp, frames };
}

async function finish(ctx, page, dir, name, rec, cdp, frames) {
  await sleep(300);
  await cdp.send('Page.stopScreencast');
  await ctx.close();
  // ffconcat: each frame holds until the next one arrives (the screencast
  // only sends a frame when the page changes). Times are relative to the
  // recorder's t0, the same clock the click marks use.
  const t0 = rec.t0 / 1000;
  const lines = ['ffconcat version 1.0'];
  frames.forEach((f, i) => {
    const next = i + 1 < frames.length ? frames[i + 1].ts : f.ts + 0.5;
    lines.push(`file ${f.file}`, `duration ${Math.max(0.001, next - f.ts).toFixed(4)}`);
  });
  lines.push(`file ${frames[frames.length - 1].file}`);
  fs.writeFileSync(path.join(dir, 'list.ffconcat'), lines.join('\n') + '\n');
  fs.writeFileSync(path.join(CAP, `${name}_marks.json`), JSON.stringify({
    first_frame_offset: frames[0].ts - t0, marks: rec.marks }, null, 2));
}

async function b6(browser) {
  const ids = JSON.parse(fs.readFileSync(IDS, 'utf8'));
  const action = process.argv[3];
  const { ctx, page, dir, cdp, frames } = await recordedContext(browser, 'b6');
  const rec = recorder(page);
  await page.goto(`${BASE}/agents/${ids.agent}/approvals`);
  const card = page.locator(`#pending a[href$="/${action}"]`);
  await card.waitFor();
  await rec.mark('approvals', card);
  await page.screenshot({ path: path.join(CAP, 'b6_approvals.png') });
  await sleep(2600);
  await rec.mark('click_card', card);
  await card.click();
  await page.locator('#review-form').waitFor();
  await rec.mark('review');
  // v2: open the review at "Current medications". The demographics card above
  // it is the person's own view of their own record, but on camera it sits
  // right after the narration says contact details are taken out. The edit
  // starts after this mark, so the top of the page is never in the cut.
  await page.evaluate(() => {
    const h = [...document.querySelectorAll('.card-header')]
      .find((e) => e.textContent.includes('Current medications'));
    window.scrollTo(0, h.closest('.card').getBoundingClientRect().top + window.scrollY - 16);
  });
  await sleep(150);
  await rec.mark('review_at_meds');
  await sleep(1500);
  const meds = await page.locator('.med-row').count();
  for (let i = 0; i < meds; i++) {
    const l = page.locator(`label[for="med-${i}-yes"]`);
    await l.scrollIntoViewIfNeeded();
    await sleep(350);
    await rec.mark(`med_${i}`, l);
    await l.click();
    await sleep(500);
  }
  const allergies = await page.locator('.allergy-row').count();
  for (let i = 0; i < allergies; i++) {
    const l = page.locator(`label[for="allergy-${i}-confirm"]`);
    await l.scrollIntoViewIfNeeded();
    await sleep(350);
    await rec.mark(`allergy_${i}`, l);
    await l.click();
    await sleep(500);
  }
  if (allergies === 0) {
    // Nothing on file. The page never pre-checks "No known allergies"; the
    // person answers it. This click is that answer, on a synthetic record.
    const nka = page.locator('label[for="nka"]');
    await nka.scrollIntoViewIfNeeded();
    await sleep(350);
    await rec.mark('nka', nka);
    await nka.click();
    await sleep(500);
  }
  const btn = page.locator('#approve-btn');
  await btn.scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy({ top: 260, behavior: 'smooth' }));
  await sleep(900);
  await page.screenshot({ path: path.join(CAP, 'b6_ready.png') });
  await rec.mark('approve', btn);
  await btn.click();
  const msg = page.locator('#review-gate-msg');
  await page.waitForFunction(() => /^Review recorded\./.test(
    document.getElementById('review-gate-msg').textContent.trim()), null, { timeout: 60_000 });
  await rec.mark('confirmed', msg);
  await page.screenshot({ path: path.join(CAP, 'b6_confirmed.png') });
  await sleep(3000);
  const status = await page.evaluate(async ([a, g]) => {
    const r = await fetch(`/api/form/${a}?agent=${g}`);
    return r.json();
  }, [action, ids.agent]);
  fs.writeFileSync(path.join(CAP, 'b6_form_status.json'), JSON.stringify(status, null, 2));
  if (status.status !== 'completed' || !status.delivery_link) throw new Error('not delivered: ' + JSON.stringify(status));
  const pdf = await ctx.request.get(status.delivery_link);
  if (!pdf.ok()) throw new Error('pdf fetch ' + pdf.status());
  fs.writeFileSync(path.join(CAP, 'b6_delivered.pdf'), await pdf.body());
  await finish(ctx, page, dir, 'b6', rec, cdp, frames);
}

async function b7(browser) {
  const ids = JSON.parse(fs.readFileSync(IDS, 'utf8'));
  const { ctx, page, dir, cdp, frames } = await recordedContext(browser, 'b7');
  const rec = recorder(page);
  await page.goto(`${BASE}/chat?agent=${ids.agent}`);
  await page.locator('#box').waitFor();
  await rec.mark('chat');
  await sleep(1500);
  await rec.mark('typing', page.locator('#box'));
  await page.locator('#box').pressSequentially('What do my blood test results say?', { delay: 55 });
  await sleep(400);
  await rec.mark('send', page.locator('#send'));
  await page.locator('#send').click();
  page.on('console', (m) => console.log('console:', m.text()));
  page.on('requestfailed', (r) => console.log('requestfailed:', r.url()));
  await page.locator('#log .chip').first().waitFor({ timeout: 150_000 });
  await rec.mark('first_tool_chip');
  // The answer: an agent message after the user's, with the typing dots gone.
  await page.waitForFunction(() => {
    const log = document.getElementById('log');
    const kids = [...log.children];
    const u = kids.findIndex((n) => n.classList.contains('user'));
    const after = kids.slice(u + 1).filter((n) => n.matches('.msg.agent:not(.typing)'));
    return !log.querySelector('.typing') && after.length &&
      after[after.length - 1].textContent.length > 80;
  }, null, { timeout: 150_000 });
  await rec.mark('answer_text');
  await sleep(5000);  // let the typewriter finish on camera
  await rec.mark('done');
  await sleep(2500);
  await page.screenshot({ path: path.join(CAP, 'b7_answer.png'), fullPage: false });
  fs.writeFileSync(path.join(CAP, 'b7_transcript.txt'),
    await page.locator('#log').innerText());
  await sleep(1500);
  await finish(ctx, page, dir, 'b7', rec, cdp, frames);
}

const browser = await chromium.launch({ slowMo: 120 });
try {
  if (phase === 'setup') await setup(browser);
  else if (phase === 'b6') await b6(browser);
  else if (phase === 'b7') await b7(browser);
  else throw new Error('phase?');
} finally {
  await browser.close();
}
