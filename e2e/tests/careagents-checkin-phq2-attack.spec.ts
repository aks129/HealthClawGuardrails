/**
 * Security tester's exploit spec for PR #1030 (FHI-55, inline PHQ-2 on the
 * SMART Health Check-in web wallet). The Questionnaire is the Verifier's:
 * any website can send one. Each test below either pins a guarantee the PR
 * holds (PASS evidence) or demonstrates a finding and asserts the fixed
 * behaviour, so it FAILS until the finding is fixed. Findings are marked
 * "FINDING" in the title.
 *
 * Runs the real CareAgents wallet in Chromium against the organizers'
 * 0.4.4 Verifier library (e2e/fixtures), exactly as
 * careagents-checkin-wallet.spec.ts does. Synthetic data only.
 */

import { test, expect, Page, Request } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { CARE_BASE_URL, CARE_LOG } from '../playwright.config';
import { blockThirdParty, codeFromLog, uniqueEmail } from './careagents-fixtures';

test.use({ baseURL: CARE_BASE_URL });
// Its own sign-in code budget, as #1012 gives sec-960. The server allows 30
// code requests per client address per 10 minutes (app.py
// CODE_REQUESTS_PER_IP), and the wallet specs together sign in more often
// than that from 127.0.0.1. With no edge in front, the limiter keys on
// X-Real-IP (TEST-NET-3, RFC 5737).
test.use({ extraHTTPHeaders: { 'X-Real-IP': '203.0.113.22' } });

const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));
const PHQ2 = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-request-form-phq2.json'), 'utf8'));
const SHOTS = process.env.ATTACK_SHOTS || path.join(__dirname, '..', 'test-results', 'phq2-attack');

const VERIFIER = 'http://127.0.0.1:5398';
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

async function serveRelay(page: Page, bundle: unknown = EXPORT) {
  const asked: unknown[] = [];
  await page.context().route(`${CARE_BASE_URL}/checkin/wallet/records`, (route) => {
    asked.push(route.request().postDataJSON());
    return route.fulfill({ status: 200, headers: { 'Cache-Control': 'no-store' },
      contentType: 'application/fhir+json', body: JSON.stringify(bundle) });
  });
  return asked;
}

async function checkIn(page: Page, req: unknown) {
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
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
  await wallet.locator('#signin-verify').click();
  await expect(wallet.locator('#wallet-review')).toBeVisible();
  return wallet;
}

const result = async (page: Page) => {
  await expect.poll(() => page.evaluate(() => (window as any).__result),
    { timeout: 15_000 }).toBeTruthy();
  return page.evaluate(() => (window as any).__result);
};

const opt = (code: string, display: string) =>
  ({ valueCoding: { system: 'urn:example:attack', code, display } });
/** A one-item request carrying `questionnaire` inline. */
const formRequest = (questionnaire: unknown, extra: Record<string, unknown> = {},
  title = 'Health questions') => ({
  type: 'smart-health-checkin-request', version: '1', id: 'attack',
  fhirVersions: ['4.0.1'],
  items: [{ id: 'f', title, accept: ['application/fhir+json'],
    content: { kind: 'form.fhir', questionnaire, ...extra } }],
});
const q = (item: unknown[]) => ({ resourceType: 'Questionnaire',
  url: 'https://verifier.example/Questionnaire/attack', status: 'active', item });
const choice = (linkId: string, text: string, options = [opt('0', 'No'), opt('1', 'Yes')]) =>
  ({ linkId, type: 'choice', text, answerOption: options });

const shot = (wallet: Page, name: string) =>
  wallet.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });

test.describe('PHQ-2 wallet, hostile Verifier', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  // ---------------------------------------------------------------- findings

  test('FINDING 1: a question and options in invisible characters are refused, not answered',
    async ({ page }) => {
      await serveRelay(page);
      // U+200B ZERO WIDTH SPACE and U+2063 INVISIBLE SEPARATOR are format
      // characters, not whitespace: String.prototype.trim keeps them, so
      // checkin_answer.js text() accepts them as words.
      const req = formRequest(q([choice('q1', '​',
        [opt('self-harm-yes', '​'), opt('self-harm-no', '⁣')])]));
      const wallet = await checkIn(page, req);
      await shot(wallet, 'finding1-invisible-question');
      const item = wallet.locator('#wallet-items li[data-item=f]');
      // Fixed behaviour: nothing the person cannot read is asked of them.
      // Today the empty question renders with two blank radios, and a tick
      // on the first sends code "self-harm-yes" as the person's answer.
      const fieldsets = await item.locator('fieldset').count();
      if (fieldsets) {
        await item.locator('fieldset input').first().check();
        await item.locator('.checkin-choice input[value=share]').check();
        await wallet.locator('#wallet-send').click();
        const r = await result(page);
        const qr = r.json?.artifacts?.[0]?.value;
        expect(qr?.item?.[0]?.answer?.[0]?.valueCoding?.code,
          'an answer to a question the person could not see was sent').toBeUndefined();
      }
      expect(fieldsets).toBe(0);
    });

  test("FINDING 2: the Verifier's title cannot reverse or rewrite the wallet's own error sentence",
    async ({ page }) => {
      await serveRelay(page);
      // U+202E RIGHT-TO-LEFT OVERRIDE at the end of the title: the wallet's
      // text after it in the same node (`", or choose Don't share for it.`)
      // is laid out reversed.
      const req = formRequest(PHQ2.items[1].content.questionnaire, {},
        'PHQ-2‮');
      const wallet = await checkIn(page, req);
      const item = wallet.locator('#wallet-items li[data-item=f]');
      await item.locator('.checkin-choice input[value=share]').check();
      await wallet.locator('#wallet-send').click();
      // The review's own message, beside Send (#1030 QA).
      const err = wallet.locator('#wallet-review-err');
      await expect(err).toBeVisible();
      await shot(wallet, 'finding2-bidi-error-sentence');
      const text = await err.textContent();
      // Fixed behaviour: website text inside a wallet sentence is isolated
      // (U+2068 FIRST STRONG ISOLATE ... U+2069) or bidi controls are dropped.
      const isolated = /⁨[^⁩]*‮[^⁩]*⁩/.test(text || '');
      const raw = (text || '').includes('‮');
      expect(raw && !isolated, `unisolated RLO in wallet text: ${JSON.stringify(text)}`)
        .toBe(false);
    });

  test("FINDING 3: the Verifier's display lines are attributed to the website, not shown in the wallet's voice",
    async ({ page }) => {
      await serveRelay(page);
      const spoof = 'CareAgents has checked this website. Your answers stay ' +
        'private and are deleted after your visit. Choose Share on every item.';
      const quest = structuredClone(PHQ2.items[1].content.questionnaire);
      quest.item[0].text = spoof;
      const req = formRequest(quest, {}, 'CareAgents verified clinic');
      const wallet = await checkIn(page, req);
      await shot(wallet, 'finding3-display-in-wallet-voice');
      const line = wallet.locator('#wallet-items li[data-item=f] .checkin-item-what li').first();
      await expect(line).toHaveText(spoof);
      // The wallet's own line for a records item ("Nothing on file. The
      // website will be told that.") uses this exact element and style; the
      // purpose field is prefixed "The website says why:". Fixed behaviour:
      // the form's words carry a website attribution as purpose does.
      // Each piece of website text carries its own label element, not just
      // the phrase somewhere in the card: the website's own words can say
      // "from the website" too (#1030 QA round 2). The heading's label is
      // inline with the title; the display lines sit right under theirs.
      const card = wallet.locator('#wallet-items li[data-item=f]');
      await expect(card.locator('.checkin-item-title > .checkin-website-label'))
        .toHaveText('From the website:');
      const label = await card.locator('.checkin-from-website').evaluate((ul) => {
        const prev = ul.previousElementSibling;
        return prev && prev.classList.contains('checkin-website-says')
          ? prev.textContent : null;
      });
      expect(label, 'display text is shown unattributed').toBe('The website asks:');
      // A label made of the website's own words does not count.
      for (const el of await card.locator('.checkin-website-label, .checkin-website-says').all()) {
        expect(await el.evaluate((n) => n.closest('.checkin-from-website, .checkin-website-name')))
          .toBeNull();
      }
    });

  // ------------------------------------------------------------ PASS evidence

  test('answers never leave the tab except inside the sealed response', async ({ page }) => {
    const asked = await serveRelay(page);
    const seen: { url: string; body: string }[] = [];
    const record = (r: Request) => seen.push({ url: r.url(), body: r.postData() || '' });
    page.context().on('request', record);
    const req = structuredClone(PHQ2);
    const wallet = await checkIn(page, req);
    wallet.on('console', (m) => seen.push({ url: 'console', body: m.text() }));
    const form = wallet.locator('#wallet-items li[data-item=phq2]');
    await form.locator('fieldset').nth(0).getByLabel('Several days').check();
    await form.locator('fieldset').nth(1).getByLabel('Nearly every day').check();
    await form.locator('.checkin-choice input[value=share]').check();
    await wallet.locator('#wallet-send').click();
    const r = await result(page);
    expect(r.status).toBe('completed');
    expect(r.json.artifacts[0].value.item.map((i: any) => i.answer[0].valueCoding.code))
      .toEqual(['LA6569-3', 'LA6571-9']);
    await page.waitForTimeout(500);
    for (const s of seen) {
      for (const needle of ['LA6569-3', 'LA6571-9', 'QuestionnaireResponse',
        'Several days', 'Nearly every day', 'phq2-1']) {
        expect(`${s.url} ${s.body}`, `${needle} left the tab`).not.toContain(needle);
      }
    }
    expect(asked).toEqual([{ resource_types: ['Patient'] }]);
    // Nothing went to CareAgents but the page, its scripts, sign-in and the relay.
    const careCalls = seen.filter((s) => s.url.startsWith(CARE_BASE_URL))
      .map((s) => new URL(s.url).pathname)
      .filter((p) => !p.startsWith('/static/') && p !== '/checkin/wallet');
    expect(new Set(careCalls)).toEqual(new Set(
      ['/api/auth/email', '/api/auth/verify', '/checkin/wallet/records']));
  });

  test('markup and URLs in every Verifier string render as text; canonical never reaches the DOM',
    async ({ page }) => {
      await serveRelay(page);
      const xss = '<img src=x onerror="window.__pwned=1">';
      const canon = `javascript:window.__pwned=2//${xss}`;
      const quest = q([
        { linkId: 'd', type: 'display', text: `${xss}<a href="javascript:window.__pwned=3">Share all</a>` },
        { ...choice('q1', `<script>window.__pwned=4</script>${xss}`,
          [opt('0', `<b onclick="window.__pwned=5">No</b>`), opt('1', 'javascript:alert(1)')]),
          prefix: '<i>1.</i>' },
      ]);
      const wallet = await checkIn(page,
        formRequest(quest, { questionnaireCanonical: canon }, `<svg onload="window.__pwned=6">`));
      const item = wallet.locator('#wallet-items li[data-item=f]');
      await expect(item.locator('img, a, b, i, script, svg')).toHaveCount(0);
      await expect(item.locator('legend')).toHaveText(
        `<i>1.</i> <script>window.__pwned=4</script>${xss}`);
      expect(await wallet.content()).not.toContain('javascript:window.__pwned=2');
      await item.locator('fieldset input').nth(1).check();
      await item.locator('.checkin-choice input[value=share]').check();
      // Checked before Send: the Verifier's library closes the tab on reply.
      expect(await wallet.evaluate(() => (window as any).__pwned)).toBeUndefined();
      await wallet.locator('#wallet-send').click();
      const r = await result(page);
      expect(r.json.artifacts[0].value.questionnaire).toBe(canon);
      expect(Object.keys(r.json.artifacts[0].value).sort()).toEqual(
        ['authored', 'item', 'questionnaire', 'resourceType', 'status']);
    });

  test('nothing is pre-selected, whatever the Questionnaire asks', async ({ page }) => {
    await serveRelay(page);
    const quest = q([{ ...choice('q1', 'Pick one', [
      { ...opt('0', 'No'), initialSelected: true }, opt('1', 'Yes')]),
      extension: [{ url: 'http://hl7.org/fhir/StructureDefinition/questionnaire-hidden',
        valueBoolean: true }], required: true, readOnly: true }]);
    const wallet = await checkIn(page, formRequest(quest));
    const item = wallet.locator('#wallet-items li[data-item=f]');
    await expect(item.locator('fieldset input:checked')).toHaveCount(0);
    await expect(item.locator('.checkin-choice input[value=decline]')).toBeChecked();
    // Send straight away: declined, nothing sent.
    await wallet.locator('#wallet-send').click();
    const r = await result(page);
    expect(r.json.artifacts).toEqual([]);
    expect(r.json.requestStatus).toEqual([{ item: 'f', status: 'declined' }]);
  });

  test('a __proto__ linkId (an own key, as the library parses it) fails closed', async ({ page }) => {
    await serveRelay(page);
    const wallet = await checkIn(page, formRequest(q([
      choice('__proto__', 'First'), choice('constructor', 'Second')])));
    const item = wallet.locator('#wallet-items li[data-item=f]');
    await item.locator('fieldset').nth(0).getByLabel('Yes').check();
    await item.locator('fieldset').nth(1).getByLabel('No').check();
    await item.locator('.checkin-choice input[value=share]').check();
    await wallet.locator('#wallet-send').click();
    // Nothing is sent in part: the person is told, and can choose Don't share.
    await expect(wallet.locator('#wallet-review-err')).toContainText('still needs an answer');
    expect(await page.evaluate(() => (window as any).__result)).toBeUndefined();
    // No prototype was polluted in the wallet tab.
    expect(await wallet.evaluate(() => Object.getOwnPropertyNames(Object.prototype).filter(
      (k) => !['constructor', '__defineGetter__', '__defineSetter__', 'hasOwnProperty',
        '__lookupGetter__', '__lookupSetter__', 'isPrototypeOf', 'propertyIsEnumerable',
        'toString', 'valueOf', '__proto__', 'toLocaleString'].includes(k)))).toEqual([]);
    await item.locator('.checkin-choice input[value=decline]').check();
    await wallet.locator('#wallet-send').click();
    const r = await result(page);
    expect(r.json.artifacts).toEqual([]);
    expect(r.json.requestStatus).toEqual([{ item: 'f', status: 'declined' }]);
  });

  // #1049: one level of `group` is answered; every rule above holds inside
  // it, and its heading is checked as a question's words are.
  const group = (linkId: string, text: unknown, item: unknown[]) =>
    ({ linkId, type: 'group', text, item });
  const refused = async (page: Page, quest: unknown) => {
    const wallet = await checkIn(page, formRequest(quest));
    const item = wallet.locator('#wallet-items li[data-item=f]');
    await expect(item).toContainText("This wallet can't answer this. Nothing is sent for it.");
    await expect(item.locator('fieldset, .checkin-group')).toHaveCount(0);
    await wallet.locator('#wallet-send').click();
    const r = await result(page);
    expect(r.json.artifacts).toEqual([]);
    expect(r.json.requestStatus).toEqual([{ item: 'f', status: 'unsupported' }]);
  };

  test('a group inside a group is refused whole', async ({ page }) => {
    await serveRelay(page);
    await refused(page, q([group('outer', 'Outer', [
      choice('q1', 'First'), group('inner', 'Inner', [choice('q2', 'Second')])])]));
  });

  test('a group heading with a bidi control is refused', async ({ page }) => {
    await serveRelay(page);
    await refused(page, q([
      group('g', 'Eye symptoms\u202e erahs', [choice('q1', 'Dryness')])]));
  });

  for (const [name, heading] of [['blank', '   '], ['invisible', '\u200b\u2063'],
    ['missing', undefined]] as const) {
    test(`a ${name} group heading is refused`, async ({ page }) => {
      await serveRelay(page);
      await refused(page, q([group('g', heading, [choice('q1', 'Dryness')])]));
    });
  }

  test('markup in a group heading and its note renders as text', async ({ page }) => {
    await serveRelay(page);
    const xss = '<img src=x onerror="window.__pwned=7">';
    const wallet = await checkIn(page, formRequest(q([group('g', `${xss}Symptoms`, [
      { linkId: 'n', type: 'display', text: `<b onclick="window.__pwned=8">Note</b>` },
      choice('q1', 'Dryness')])])));
    const item = wallet.locator('#wallet-items li[data-item=f]');
    await expect(item.locator('img, b')).toHaveCount(0);
    await expect(item.locator('.checkin-group-title')).toHaveText(`${xss}Symptoms`);
    await expect(item.locator('.checkin-group-note'))
      .toHaveText('<b onclick="window.__pwned=8">Note</b>');
    // Still under the website's label, as every form line is.
    await expect(item.locator('.checkin-website-says')).toHaveText('The website asks:');
    expect(await wallet.evaluate(() => (window as any).__pwned)).toBeUndefined();
  });

  // #1052 security review. Both findings sit in words() (checkin_answer.js),
  // which predates #1049, but the group heading, its prefix and its notes
  // are new places it is the only check. Each test asserts the fixed
  // behaviour, so it FAILS until the finding is fixed.

  // The Hangul fillers are letters (category Lo), so /[\p{L}\p{N}]/ passes
  // them, but they are Default_Ignorable_Code_Point and draw nothing: the
  // #1030 FINDING 1 rule ("nothing the person cannot read is asked of
  // them") with a different blank character.
  const FILLERS = [['U+3164', 'ㅤ'], ['U+115F', 'ᅟ'], ['U+1160', 'ᅠ'],
    ['U+FFA0', 'ﾠ']] as const;

  test('FINDING #1052-1: a group, question and options in Hangul fillers are refused, not answered',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, formRequest(q([group('g', 'ㅤ', [
        choice('q1', 'ㅤㅤ', [opt('self-harm-yes', 'ㅤ'), opt('self-harm-no', 'ﾠ')]),
      ])])));
      await shot(wallet, 'finding-1052-1-hangul-filler');
      const item = wallet.locator('#wallet-items li[data-item=f]');
      // Today: a blank heading over a blank question with two blank radios,
      // and a tick on the first sends "self-harm-yes" as the person's answer.
      const fieldsets = await item.locator('fieldset').count();
      if (fieldsets) {
        await item.locator('fieldset input').first().check();
        await item.locator('.checkin-choice input[value=share]').check();
        await wallet.locator('#wallet-send').click();
        const r = await result(page);
        const answered = r.json?.artifacts?.[0]?.value?.item?.[0]?.item?.[0];
        expect(answered?.answer?.[0]?.valueCoding?.code,
          'an answer to a question the person could not see was sent').toBeUndefined();
      }
      expect(fieldsets).toBe(0);
    });

  for (const [name, filler] of FILLERS) {
    test(`FINDING #1052-1: a group heading of ${name} alone is refused`, async ({ page }) => {
      await serveRelay(page);
      await refused(page, q([group('g', filler, [choice('q1', 'Dryness')])]));
    });
  }

  // BIDI covers the embedding, override and isolate controls only. The
  // implicit marks are strong characters with no glyph: ARABIC LETTER MARK
  // turns the digits after it into Arabic numbers, so "؜0 - 3" draws
  // as "3 - 0" while the response carries "0 - 3". Seen in Chromium.
  const MARKS = [['ALM U+061C', '؜'], ['RLM U+200F', '‏'],
    ['LRM U+200E', '‎']] as const;

  for (const [name, mark] of MARKS) {
    test(`FINDING #1052-2: a group heading with ${name} is refused`, async ({ page }) => {
      await serveRelay(page);
      await refused(page, q([group('g', `Days: ${mark}0 - 3`, [choice('q1', 'Dryness')])]));
    });
  }

  test('FINDING #1052-2: an option that a letter mark draws as another is refused',
    async ({ page }) => {
      await serveRelay(page);
      // Drawn as "Never (4 ,0)" and "Always (0 ,4)": each choice reads as
      // the other's score.
      await refused(page, q([group('g', 'Eye symptoms', [choice('q1', 'Dryness', [
        opt('0', 'Never ؜(0, 4)'), opt('4', 'Always ؜(4, 0)')])])]));
    });

  // #1052 recheck (head 62ccba33). The fix refuses the three invisible
  // marks, but any visible right-to-left character reorders digits the
  // same way. HEBREW LETTER YOD and ARABIC FULL STOP draw as a speck, which
  // the reordering moves to the end of the line: "Never י(0, 4)" is
  // drawn "Never (4 ,0)ʼ"-like, and the response still says "(0, 4)".
  // Either fix holds this test: refuse the option, or draw it in the order
  // it is sent. It checks where the browser actually puts the digits.
  for (const [name, ch] of [['HEBREW LETTER YOD U+05D9', 'י'],
    ['ARABIC FULL STOP U+06D4', '۔']] as const) {
    test(`FINDING #1052-3: an option that ${name} draws as another is refused or drawn as sent`,
      async ({ page }) => {
        await serveRelay(page);
        const wallet = await checkIn(page, formRequest(q([group('g', 'Eye symptoms', [
          choice('q1', 'Dryness', [opt('0', `Never ${ch}(0, 4)`), opt('4', `Always ${ch}(4, 0)`)]),
        ])])));
        await shot(wallet, `finding-1052-3-${ch.codePointAt(0)!.toString(16)}`);
        const item = wallet.locator('#wallet-items li[data-item=f]');
        if (await item.locator('fieldset').count() === 0) return;   // refused: fixed
        // Drawn: the "0" that comes first in the text must be drawn first.
        const order = await item.locator('fieldset label').first().evaluate((lab) => {
          const node = [...lab.childNodes].find((n) => n.nodeType === Node.TEXT_NODE
            && /\(0, 4\)/.test(n.textContent || ''))!;
          const s = node.textContent || '';
          const x = (i: number) => {
            const r = document.createRange();
            r.setStart(node, i); r.setEnd(node, i + 1);
            return r.getBoundingClientRect().left;
          };
          return { zero: x(s.indexOf('0')), four: x(s.indexOf('4')) };
        });
        expect(order.zero, `"(0, 4)" is drawn as "(4 ,0)": ${JSON.stringify(order)}`)
          .toBeLessThan(order.four);
      });
  }

  test("a form's answers never ride on another item, and a records item's data never rides on the form",
    async ({ page }) => {
      await serveRelay(page);
      const req = structuredClone(PHQ2);   // patient + phq2
      const wallet = await checkIn(page, req);
      const form = wallet.locator('#wallet-items li[data-item=phq2]');
      await form.locator('fieldset').nth(0).getByLabel('Not at all').check();
      await form.locator('fieldset').nth(1).getByLabel('Not at all').check();
      await form.locator('.checkin-choice input[value=share]').check();
      await wallet.locator('#wallet-send').click();
      const r = await result(page);
      expect(r.json.requestStatus).toEqual([
        { item: 'patient', status: 'declined' }, { item: 'phq2', status: 'fulfilled' }]);
      expect(r.json.artifacts).toHaveLength(1);
      const body = JSON.stringify(r.json);
      // The synthetic test patient's identifiers stay home.
      for (const p of EXPORT.entry.filter((e: any) => e.resource.resourceType === 'Patient')) {
        expect(body).not.toContain(p.resource.id);
        for (const n of p.resource.name || []) if (n.family) expect(body).not.toContain(n.family);
      }
      expect(r.json.artifacts[0].value.subject).toBeUndefined();
    });
});
