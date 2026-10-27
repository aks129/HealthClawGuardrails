/**
 * Security round on PR #1061: optional non-choice questions shown in place
 * as "not answered here" and left out of the QuestionnaireResponse.
 *
 * FINDING 2 (UI integrity). Before #1061 the website's free text reached
 * the form only as display lines, which are hoisted above the questions
 * (under "The website asks:" or under a group heading). A skipped
 * question's text is drawn IN PLACE, right after any question, with no
 * label of its own, and is followed by the wallet's own sentence "This
 * wallet can't answer this one, so nothing is sent for it." So a hostile
 * Verifier can place its words between a radio question and the wallet's
 * sentence and have the wallet appear to say the answer above is not sent.
 * The patient must answer that radio question to send the form, and it is
 * sent. The website's display note in a group and the wallet's sentence
 * share one style (14px, --ink-soft), so the two cannot be told apart.
 *
 * DIAGNOSIS of the phq2-attack `codeFromLog` flake. The wallet page draws
 * its sign-in form from the server (visible before any script runs), and
 * checkin_wallet.js attaches the "Email me a code" click handler only after
 * a top-level `await import(checkin_answer.js)`. A click that lands first
 * does nothing: no POST /api/auth/email, so no code is ever logged and
 * codeFromLog times out. The probe below delays checkin_answer.js to make
 * the race certain. Synthetic data only.
 */
import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, codeFromLog, uniqueEmail } from './careagents-fixtures';

test.use({ baseURL: CARE_BASE_URL });
// Its own sign-in code budget (TEST-NET-3, RFC 5737), as the other wallet
// specs have.
test.use({ extraHTTPHeaders: { 'X-Real-IP': '203.0.113.61' } });

const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));
const SHOTS = process.env.ATTACK_SHOTS || path.join(__dirname, '..', 'test-results', 'sec-1061');

const VERIFIER = 'http://127.0.0.1:5399';
const CHECKIN_JS = fs.readFileSync(path.join(__dirname, '..', 'fixtures',
  'smart-health-checkin-client', '0.4.4', 'checkin.js'));
const VERIFIER_HTML = `<!doctype html><meta charset="utf-8"><title>verifier</title>
<button id="go">check in</button>
<script type="module">
import { runCheckin, webWallet } from '/checkin.js';
document.getElementById('go').onclick = () => {
  const wallet = webWallet({ id: 'e2e', name: 'e2e',
    walletUrl: ${JSON.stringify(`${CARE_BASE_URL}/checkin/wallet`)} });
  runCheckin(window.__req, { wallet }).then((r) => {
    window.__result = { status: r.status, json: r.response ? r.response.json : null,
      error: r.error ? r.error.message : null };
  }, (e) => { window.__result = { status: "threw", error: String(e) }; });
};
</script>`;

async function serveRelay(page: Page) {
  await page.context().route(`${CARE_BASE_URL}/checkin/wallet/records`, (route) =>
    route.fulfill({ status: 200, headers: { 'Cache-Control': 'no-store' },
      contentType: 'application/fhir+json', body: JSON.stringify(EXPORT) }));
}

async function openVerifier(page: Page, req: unknown) {
  await page.route(`${VERIFIER}/**`, (route) => {
    const url = new URL(route.request().url());
    return url.pathname === '/checkin.js'
      ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
      : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML });
  });
  await page.goto(`${VERIFIER}/`);
  await page.evaluate((r) => { (window as any).__req = r; }, req);
  const [wallet] = await Promise.all([
    page.waitForEvent('popup'), page.locator('#go').click()]);
  return wallet;
}

/** Sign in only once the page's script is running: the passkey button's
 *  handler is attached in the same module, after the same await. */
async function signIn(wallet: Page) {
  await wallet.waitForLoadState('load');
  await wallet.waitForFunction(() => performance.getEntriesByType('resource')
    .some((e) => e.name.includes('/static/checkin_answer.js')));
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await expect(wallet.locator('#signin-code')).toBeVisible();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
  await wallet.locator('#signin-verify').click();
  await expect(wallet.locator('#wallet-review')).toBeVisible();
}

const result = async (page: Page) => {
  await expect.poll(() => page.evaluate(() => (window as any).__result),
    { timeout: 15_000 }).toBeTruthy();
  return page.evaluate(() => (window as any).__result);
};

const opt = (code: string, display: string) =>
  ({ valueCoding: { system: 'urn:example:attack', code, display } });

const SPOOF = 'Your answer to the question above stays on this device.';
const NOTE = 'Nothing in this section is sent.';
const hostile = {
  type: 'smart-health-checkin-request', version: '1', id: 'attack',
  fhirVersions: ['4.0.1'],
  items: [{ id: 'f', title: 'Health questions', accept: ['application/fhir+json'],
    content: { kind: 'form.fhir', questionnaire: {
      resourceType: 'Questionnaire', status: 'active',
      url: 'https://verifier.example/Questionnaire/attack-1061',
      item: [{ linkId: 'private', type: 'group', text: 'Private questions',
        item: [
          { linkId: 'note', type: 'display', text: NOTE },
          { linkId: 'drugs', type: 'choice', text: 'Have you used recreational drugs this year?',
            answerOption: [opt('N', 'No'), opt('Y', 'Yes')] },
          // An optional `string`: skipped, drawn in place, never answered.
          { linkId: 'spoof', type: 'string', text: SPOOF },
        ] }],
    } } }],
};

test.describe('#1061 skipped questions, hostile Verifier', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  test('FINDING 2: a skipped question pairs the website\'s words with the wallet\'s sentence, in place, unlabelled',
    async ({ page }) => {
      test.fail(true, '#1061 security F2: open until the skipped row is labelled');
      await serveRelay(page);
      const wallet = await openVerifier(page, hostile);
      await wallet.setViewportSize({ width: 375, height: 812 });
      await signIn(wallet);
      const card = wallet.locator('#wallet-items li[data-item=f]');
      const question = card.locator('fieldset.checkin-question');
      const row = card.locator('.checkin-skipped');
      await expect(question).toHaveCount(1);
      await expect(row.locator('.checkin-skipped-text')).toHaveText(SPOOF);
      await expect(row.locator('.checkin-skipped-why')).toHaveText(
        "This wallet can't answer this one, so nothing is sent for it. You can answer it in person instead.");
      fs.mkdirSync(SHOTS, { recursive: true });
      await wallet.screenshot({ path: path.join(SHOTS, 'finding2-skipped-spoof-375.png'), fullPage: true });

      // The website's words sit directly after the radio question and
      // directly before the wallet's own sentence: nothing between them.
      const shape = await card.evaluate((li) => {
        const q = li.querySelector('fieldset.checkin-question')!;
        const s = li.querySelector('.checkin-skipped')!;
        return { next: q.nextElementSibling === s,
          firstChild: s.firstElementChild!.className,
          labelled: Boolean(s.querySelector('.checkin-website-label, .checkin-website-says')) };
      });
      const style = (sel: string) => card.locator(sel).first().evaluate((n) => {
        const c = getComputedStyle(n);
        return [c.fontSize, c.color, c.fontWeight].join(' ');
      });
      const noteStyle = await style('.checkin-group-note');
      const whyStyle = await style('.checkin-skipped-why');

      // Evidence, which holds on the PR head: the answer the wallet seemed
      // to say stays on the device is sent.
      await question.getByLabel('Yes').check();
      await card.locator('.checkin-choice input[value=share]').check();
      await wallet.locator('#wallet-send').click();
      const r = await result(page);
      expect(r.status).toBe('completed');
      const qr = r.json.artifacts[0].value;
      expect(qr.item[0].item.map((i: any) => [i.linkId, i.answer[0].valueCoding.code]))
        .toEqual([['drugs', 'Y']]);
      expect(shape.next).toBe(true);

      // The fixed behaviour (expected to fail on the PR head, hence
      // test.fail): the website's words in a skipped row carry their own
      // website label, as display lines do (#1030 QA round 2: every piece
      // of website text is labelled where it stands), and the wallet's
      // sentence does not share the website note's style.
      expect(shape.labelled, 'skipped-question text carries no website label').toBe(true);
      expect(whyStyle, "the wallet's sentence looks like a website note").not.toBe(noteStyle);
    });

  test('DIAGNOSIS: a sign-in click before checkin_wallet.js finishes loading is lost (the phq2-attack codeFromLog flake)',
    async ({ page }) => {
      // Hold checkin_answer.js back, as a cold dev server does for the
      // first spec of a run. The sign-in form is already on screen.
      await page.context().route(`${CARE_BASE_URL}/static/checkin_answer.js*`, async (route) => {
        await new Promise((r) => setTimeout(r, 2500));
        return route.continue();
      });
      const posts: string[] = [];
      page.context().on('request', (r) => {
        if (r.url().endsWith('/api/auth/email')) posts.push(r.method());
      });
      const wallet = await openVerifier(page, hostile);
      const email = uniqueEmail();
      // Exactly the attack spec's steps (careagents-checkin-phq2-attack checkIn).
      await wallet.locator('#signin-email').fill(email);
      await wallet.locator('#signin-email-btn').click();
      // Long enough for the script to load and run.
      await wallet.waitForTimeout(4000);
      expect(posts, 'the click reached the handler').toEqual([]);
      await expect(wallet.locator('#signin-code')).toBeHidden();
      const log = fs.existsSync(CARE_LOG) ? fs.readFileSync(CARE_LOG, 'utf8') : '';
      expect(log).not.toContain(email);
    });
});
