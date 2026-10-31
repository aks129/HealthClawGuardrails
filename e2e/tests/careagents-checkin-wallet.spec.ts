/**
 * SMART Health Check-in web wallet skeleton (FHI-54), in a real browser.
 *
 * Two things only a browser can show:
 *
 *  1. The hand-off order. The wallet must not tell its opener `ready` until
 *     the person has signed in, in that tab, without navigating away; a
 *     request sent to a tab that then leaves for /auth is lost. An opener page
 *     here counts the messages it receives, before and after sign-in.
 *  2. The answer rules in careagents/static/checkin_answer.js, run as shipped
 *     against the synthetic test patient (FHI-51): unprofiled records left
 *     out, nothing on file -> unavailable, an allergy sent only when it codes
 *     a substance, unchosen -> declined, a form named only by its canonical
 *     -> unsupported (an inline PHQ-2 is filled in, FHI-55), and a
 *     response the vendored library's own pre-flight check finds clean.
 *
 * The records come from POST /checkin/wallet/records, which relays the
 * engine's checkin export (careagents/checkin_wallet.py). This suite's
 * CareAgents has no engine behind it, so the relay is answered here, by
 * route, with the test patient; what the wallet asks it is recorded and
 * checked. The relay itself, on the real engine: tests/test_checkin_relay.py.
 *
 * The sealed round trip with a real check-in page is the organizers' Testing
 * EHR (https://smart-health-checkin.org/connectathon/testing-ehr/), run by
 * hand; it needs the public internet, which this suite never touches.
 */

import { test, expect, Page } from '@playwright/test';
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
test.use({ extraHTTPHeaders: { 'X-Real-IP': '203.0.113.21' } });

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', '..', 'careagents', 'checkin',
    'us_core_test_patient.json'), 'utf8'));
// What the relay answers with: the engine's own checkin export of the test
// patient, upstream words dropped and our labels written (pinned to the
// engine by tests/test_checkin_relay.py).
const EXPORT = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
  'fixtures', 'checkin-export-test-patient.json'), 'utf8'));

const US_CORE = 'http://hl7.org/fhir/us/core/StructureDefinition/';
const item = (id: string, profile: string) => ({
  id, title: id,
  content: { kind: 'selection.fhir', profiles: [US_CORE + profile] },
  accept: ['application/fhir+json'],
});
// The connectathon's share-records request, plus an inline form.
const REQUEST = {
  type: 'smart-health-checkin-request', version: '1', id: 'e2e-request',
  fhirVersions: ['4.0.1'],
  items: [
    item('patient', 'us-core-patient'),
    item('problems', 'us-core-condition-problems-health-concerns'),
    item('allergies', 'us-core-allergyintolerance'),
    item('medications', 'us-core-medicationrequest'),
    item('immunizations', 'us-core-immunization'),
    item('coverage', 'us-core-coverage'),
    { id: 'phq2', title: 'PHQ-2',
      content: { kind: 'form.fhir',
        questionnaireCanonical: 'http://example.org/Questionnaire/phq-2' },
      accept: ['application/fhir+json'] },
  ],
};

/** Run the shipped answer module in the page against `bundle`. */
async function answer(page: Page, bundle: unknown, share: Record<string, boolean>) {
  return page.evaluate(async ({ request, bundle, share }) => {
    const m = await import('/static/checkin_answer.js');
    const lib = await import(
      '/static/vendor/smart-health-checkin-client/0.4.4/wallet.js');
    const matches = m.matchItems(request, [], m.prepareRecords(bundle));
    const response = m.buildResponse(request, matches, share);
    const problems = lib.checkWalletResponse(request, response);
    const status = Object.fromEntries(
      response.requestStatus.map((r: any) => [r.item, r.status]));
    const ids = Object.fromEntries(response.artifacts.map((a: any) => [
      a.fulfills[0], a.value.entry.map((e: any) => e.resource.id)]));
    const versions = response.artifacts.map((a: any) => a.fhirVersion);
    return { status, ids, versions, problems };
  }, { request: REQUEST, bundle, share });
}

// A check-in page on another origin, served by route, using the organizers'
// Verifier library (e2e/fixtures, the 0.4.4 release asset; the security
// spec pins its hash).
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

async function signInInTab(wallet: Page) {
  const email = uniqueEmail();
  await wallet.locator('#signin-email').fill(email);
  await wallet.locator('#signin-email-btn').click();
  await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
  await wallet.locator('#signin-verify').click();
}

/** Answer the records relay for every page in the context with `bundle`
 *  (or, with a status, the relay's refusal), and keep each body the wallet
 *  sent it. */
async function serveRelay(page: Page, bundle: unknown = EXPORT, status = 200) {
  const asked: unknown[] = [];
  await page.context().route(`${CARE_BASE_URL}/checkin/wallet/records`,
    (route) => {
      asked.push(route.request().postDataJSON());
      return route.fulfill({ status, headers: { 'Cache-Control': 'no-store' },
        contentType: status === 200 ? 'application/fhir+json' : 'application/json',
        body: JSON.stringify(bundle) });
    });
  return asked;
}

/** The Verifier page, opening the wallet with `req`; resolves to the
 *  signed-in wallet tab. */
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
  await signInInTab(wallet);
  return wallet;
}

const ALL = { patient: true, problems: true, allergies: true,
  medications: true, immunizations: true, coverage: true, phq2: true };

test.describe('check-in wallet', () => {
  test.beforeEach(async ({ page }) => { await blockThirdParty(page); });

  test('opened directly, it says there is nothing to answer', async ({ page }) => {
    await page.goto('/checkin/wallet');
    await expect(page.locator('#wallet-direct')).toBeVisible();
    await expect(page.locator('#wallet-signin')).toBeHidden();
  });

  test('it signs in where it stands, and only then tells its opener ready',
    async ({ page }) => {
      await page.goto('/');
      await page.evaluate(() => {
        (window as any).__got = [];
        window.addEventListener('message', (e) => (window as any).__got.push(
          { type: e.data && e.data.type, outcome: e.data && e.data.outcome }));
      });
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'),
        page.evaluate(() => { (window as any).__w = window.open('/checkin/wallet'); }),
      ]);
      await blockThirdParty(wallet);
      await expect(wallet.locator('#wallet-signin')).toBeVisible();
      // Signed out: no `ready`, so a check-in page has sent nothing yet.
      await wallet.waitForTimeout(500);
      expect(await page.evaluate(() => (window as any).__got)).toEqual([]);

      const email = uniqueEmail();
      await wallet.locator('#signin-email').fill(email);
      await wallet.locator('#signin-email-btn').click();
      await wallet.locator('#signin-code-input').fill(await codeFromLog(email, CARE_LOG));
      await wallet.locator('#signin-verify').click();
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      // Same tab, same opener: nothing navigated away.
      expect(wallet.url()).toBe(`${CARE_BASE_URL}/checkin/wallet`);
      await expect.poll(() => page.evaluate(() => (window as any).__got))
        .toEqual([{ type: 'digital-credentials/web-wallet/ready' }]);

      // A request it cannot read gets an error reply, not silence.
      await page.evaluate((origin) => (window as any).__w.postMessage({
        type: 'digital-credentials/web-wallet/request', requestId: 'r1',
        credentialRequestOptions: {} }, origin), CARE_BASE_URL);
      await expect.poll(() => page.evaluate(() => (window as any).__got.length))
        .toBe(2);
      expect((await page.evaluate(() => (window as any).__got))[1]).toEqual(
        { type: 'digital-credentials/web-wallet/response', outcome: 'error' });
      await expect(wallet.locator('#wallet-done-title'))
        .toHaveText("Couldn't read the request");
    });

  test('shares only what claims the profile, and answers what it cannot',
    async ({ page }) => {
      await page.goto('/');
      const r = await answer(page, FIXTURE, ALL);
      expect(r.problems).toEqual([]);
      expect(r.status).toEqual({
        patient: 'fulfilled', problems: 'fulfilled', allergies: 'fulfilled',
        medications: 'fulfilled', immunizations: 'fulfilled',
        coverage: 'fulfilled', phq2: 'unsupported' });
      // The unprofiled Condition is left out; nobody stamps a claim on it.
      expect(r.ids.problems).toEqual(['checkin-test-condition-htn']);
      // Referenced payer and prescriber ride along; the Patient does not.
      expect(r.ids.coverage.sort()).toEqual(
        ['checkin-test-coverage', 'checkin-test-payer']);
      expect(r.ids.medications.sort()).toEqual(
        ['checkin-test-medreq-lisinopril', 'checkin-test-prescriber']);
      expect(new Set(r.versions)).toEqual(new Set(['4.0.1']));
    });

  test('unchosen is declined, and nothing on file is unavailable',
    async ({ page }) => {
      await page.goto('/');
      const noAllergies = { ...FIXTURE, entry: FIXTURE.entry.filter(
        (e: any) => e.resource.resourceType !== 'AllergyIntolerance') };
      const r = await answer(page, noAllergies, { patient: true });
      expect(r.problems).toEqual([]);
      expect(r.status.patient).toBe('fulfilled');
      expect(r.status.problems).toBe('declined');
      // Not "none": nothing is inferred from an absence.
      expect(r.status.allergies).toBe('unavailable');
      expect(r.ids.allergies).toBeUndefined();
    });

  test('a coded "no known allergy" is never sent as an allergy',
    async ({ page }) => {
      await page.goto('/');
      const nka = structuredClone(FIXTURE);
      const allergy = nka.entry.find(
        (e: any) => e.resource.resourceType === 'AllergyIntolerance').resource;
      allergy.code = { coding: [{ system: 'http://snomed.info/sct',
        code: '716186003' }] };
      const r = await answer(page, nka, ALL);
      expect(r.problems).toEqual([]);
      expect(r.status.allergies).toBe('unavailable');
      expect(r.ids.allergies).toBeUndefined();
    });

  // Said in words rather than a SNOMED code: in code.text, or as a local
  // code's display. Still an absence, still never sent.
  for (const code of [
    { text: 'No known allergies' },
    { text: 'NKDA' },
    { text: 'None' },
    { text: 'nil known' },
    { text: 'No drug allergies.' },
    { coding: [{ system: 'urn:oid:1.2.3.4.5', code: 'X1',
      display: 'No Known Allergies' }] },
    { coding: [{ system: 'urn:oid:1.2.3.4.5', code: 'NKA', display: 'NKA' }] },
  ]) {
    test(`an absence in words is unavailable: ${JSON.stringify(code)}`,
      async ({ page }) => {
        await page.goto('/');
        const nka = structuredClone(FIXTURE);
        nka.entry.find((e: any) => e.resource.resourceType ===
          'AllergyIntolerance').resource.code = code;
        const r = await answer(page, nka, ALL);
        expect(r.status.allergies).toBe('unavailable');
      });
  }

  // An allow-list, not a list of absence phrases (#960 security review):
  // only a coded substance is sent, and words never decide either way.
  test('an allergy said only in words is unavailable', async ({ page }) => {
    await page.goto('/');
    for (const text of ['Penicillin', 'Nickel', 'Allergies: none',
      'Patient denies allergies', 'Zero allergies', 'N/A',
      'Sin alergias conocidas']) {
      const one = structuredClone(FIXTURE);
      one.entry.find((e: any) => e.resource.resourceType ===
        'AllergyIntolerance').resource.code = { text };
      const r = await answer(page, one, ALL);
      expect(r.status.allergies, text).toBe('unavailable');
    }
  });

  test('a coded substance is sent whatever its words say', async ({ page }) => {
    await page.goto('/');
    const RXNORM = 'http://www.nlm.nih.gov/research/umls/rxnorm';
    for (const code of [
      { coding: [{ system: RXNORM, code: '7980' }], text: 'Penicillin (no known reaction)' },
      { coding: [{ system: RXNORM, code: '7980' }], text: 'NKA-12 (allergen extract)' },
      { coding: [{ system: 'http://snomed.info/sct', code: '762952008' }], text: 'Peanut' },
      { coding: [{ system: 'http://fdasis.nlm.nih.gov', code: 'QE1QX6B99R' }] },
    ]) {
      const one = structuredClone(FIXTURE);
      one.entry.find((e: any) => e.resource.resourceType ===
        'AllergyIntolerance').resource.code = code;
      const r = await answer(page, one, ALL);
      expect(r.status.allergies, JSON.stringify(code)).toBe('fulfilled');
    }
  });

  test('a refuted or entered-in-error allergy is unavailable', async ({ page }) => {
    await page.goto('/');
    for (const status of ['refuted', 'entered-in-error']) {
      const one = structuredClone(FIXTURE);
      one.entry.find((e: any) => e.resource.resourceType ===
        'AllergyIntolerance').resource.verificationStatus = { coding: [{
          system: 'http://terminology.hl7.org/CodeSystem/allergyintolerance-verification',
          code: status }] };
      const r = await answer(page, one, ALL);
      expect(r.status.allergies, status).toBe('unavailable');
    }
  });

  test('a substance coding beside an absence coding is unavailable',
    async ({ page }) => {
      await page.goto('/');
      const one = structuredClone(FIXTURE);
      one.entry.find((e: any) => e.resource.resourceType ===
        'AllergyIntolerance').resource.code = { coding: [
        { system: 'http://www.nlm.nih.gov/research/umls/rxnorm', code: '7980' },
        { system: 'http://snomed.info/sct', code: '716186003' }] };
      const r = await answer(page, one, ALL);
      expect(r.status.allergies).toBe('unavailable');
    });

  test('the relay is asked only for the kinds of record requested',
    async ({ page }) => {
      await page.goto('/');
      const got = await page.evaluate(async ({ request }) => {
        const m = await import('/static/checkin_answer.js');
        const sel = (content: unknown) => ({ id: 'x', title: 'x',
          accept: ['application/fhir+json'], content });
        const ask = (items: unknown[]) => m.requestedTypes(
          { ...request, items }, []);
        return {
          shareRecords: m.requestedTypes(request, []),
          coverageOnly: ask([sel({ kind: 'selection.fhir', profiles: [
            'http://hl7.org/fhir/us/core/StructureDefinition/us-core-coverage|7.0.0'] })]),
          c4dic: ask([sel({ kind: 'selection.fhir', profiles: [
            'http://hl7.org/fhir/us/insurance-card/StructureDefinition/C4DIC-Coverage'] })]),
          unknownProfile: ask([sel({ kind: 'selection.fhir', profiles: [
            'http://example.org/StructureDefinition/patient'] })]),
          anyUsCore: ask([sel({ kind: 'selection.fhir', profilesFrom: [
            'http://hl7.org/fhir/us/core'] })]),
          byType: ask([sel({ kind: 'selection.fhir',
            resourceTypes: ['Immunization', 'Observation'] })]),
          noSelector: ask([sel({ kind: 'selection.fhir' })]),
          formOnly: ask([request.items[6]]),
          protoKey: ask([sel({ kind: 'selection.fhir',
            resourceTypes: ['__proto__', 'constructor', 'toString'] })]),
        };
      }, { request: REQUEST });
      expect(got.shareRecords).toEqual(['AllergyIntolerance', 'Condition',
        'Coverage', 'Immunization', 'MedicationRequest', 'Patient']);
      expect(got.coverageOnly).toEqual(['Coverage']);
      expect(got.c4dic).toEqual(['Coverage']);
      expect(got.unknownProfile).toEqual([]);
      expect(got.anyUsCore).toEqual(got.shareRecords);
      expect(got.byType).toEqual(['Immunization']);
      expect(got.noSelector).toEqual(got.shareRecords);
      expect(got.formOnly).toEqual([]);
      expect(got.protoKey).toEqual([]);
    });

  test('a member ID shows at most half of itself', async ({ page }) => {
    await page.goto('/');
    const got = await page.evaluate(async () => {
      const m = await import('/static/checkin_answer.js');
      return ['12345', '1234567', 'ABCD1234', 'FAKE-MEMBER-0000001', 42, null]
        .map((v) => m.masked(v));
    });
    expect(got).toEqual(['on file', 'on file', 'ending 1234', 'ending 0001',
      'on file', 'on file']);
  });

  test('a choice keyed by a built-in name shares nothing', async ({ page }) => {
    await page.goto('/');
    const got = await page.evaluate(async ({ request, bundle }) => {
      const m = await import('/static/checkin_answer.js');
      const req = { ...request, items: [{ ...request.items[0], id: 'constructor' }] };
      const matches = m.matchItems(req, [], m.prepareRecords(bundle));
      // Nothing chosen: `share` holds a `constructor: true` only by
      // inheritance, never as its own choice.
      const share = Object.create({ constructor: true });
      return m.buildResponse(req, matches, share).requestStatus[0].status;
    }, { request: REQUEST, bundle: FIXTURE });
    expect(got).toBe('declined');
  });

  test('the review speaks plain words, never FHIR type names',
    async ({ page }) => {
      const asked = await serveRelay(page);
      await page.setViewportSize({ width: 375, height: 812 });
      await page.route(`${VERIFIER}/**`, (route) => {
        const url = new URL(route.request().url());
        return url.pathname === '/checkin.js'
          ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
          : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML });
      });
      await page.goto(`${VERIFIER}/`);
      await page.evaluate((req) => { (window as any).__req = req; }, {
        purpose: 'Pre-visit check-in', items: REQUEST.items.slice(0, 6) });
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'), page.locator('#go').click()]);
      await wallet.setViewportSize({ width: 375, height: 812 });
      await signInInTab(wallet);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      await expect(wallet.locator('#wallet-origin')).toHaveText(VERIFIER);
      const text = await wallet.locator('#wallet-review').innerText();
      for (const type of ['Patient', 'Condition', 'AllergyIntolerance',
        'MedicationRequest', 'Immunization', 'Coverage', 'Organization',
        'Practitioner', 'Bundle', 'FHIR']) {
        expect(text, type).not.toContain(type);
      }
      for (const plain of ['Your allergies', 'Penicillin', 'Your medicines',
        'Lisinopril 10 mg', 'Your insurance card',
        'Synthetic Test Health Plan, member ID ending 0001',
        'Your vaccines', 'Influenza vaccine, 1 October 2025',
        'Casey Testpatient, born 4 July 1985', 'Your address, phone, email',
        'Hypertensive disorder']) {
        expect(text, plain).toContain(plain);
      }
      // The member ID is masked.
      expect(text).not.toContain('FAKE-MEMBER-0000001');
      // The relay was asked for the six kinds of record, and nothing else:
      // not the request, its purpose, or the website's origin.
      expect(asked).toEqual([{ resource_types: ['AllergyIntolerance',
        'Condition', 'Coverage', 'Immunization', 'MedicationRequest',
        'Patient'] }]);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'review-375.png') });
      }
    });

  test('every item starts at "Don\'t share", and a bare Send releases nothing',
    async ({ page }) => {
      await serveRelay(page);
      await page.route(`${VERIFIER}/**`, (route) => {
        const url = new URL(route.request().url());
        return url.pathname === '/checkin.js'
          ? route.fulfill({ status: 200, contentType: 'text/javascript', body: CHECKIN_JS })
          : route.fulfill({ status: 200, contentType: 'text/html', body: VERIFIER_HTML });
      });
      await page.goto(`${VERIFIER}/`);
      await page.evaluate((req) => { (window as any).__req = req; },
        { items: REQUEST.items.slice(0, 6) });
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'), page.locator('#go').click()]);
      await signInInTab(wallet);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      const checked = wallet.locator('#wallet-items input[type=radio]:checked');
      await expect(checked).toHaveCount(6);
      expect(await checked.evaluateAll((els) =>
        els.map((e) => (e as HTMLInputElement).value)))
        .toEqual(Array(6).fill('decline'));
      await wallet.locator('#wallet-send').click();
      await expect.poll(() => page.evaluate(() => (window as any).__result),
        { timeout: 15_000 }).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.map((s: any) => s.status))
        .toEqual(Array(6).fill('declined'));
    });

  // #973 patient test, finding 3: the heading does not depend on whether
  // there is anything on file. And a form says what to do instead.
  test('an item with nothing on file still has our heading', async ({ page }) => {
    await serveRelay(page, { ...EXPORT, entry: EXPORT.entry.filter((e: any) =>
      !['Immunization', 'Coverage', 'Organization'].includes(e.resource.resourceType)) });
    const wallet = await checkIn(page, { items: REQUEST.items });
    await expect(wallet.locator('#wallet-review')).toBeVisible();
    const vaccines = wallet.locator('#wallet-items li[data-item=immunizations]');
    await expect(vaccines.locator('.checkin-item-title')).toHaveText('Your vaccines');
    const card = wallet.locator('#wallet-items li[data-item=coverage]');
    await expect(card.locator('.checkin-item-title')).toHaveText('Your insurance card');
    await expect(wallet.locator('#wallet-items li[data-item=phq2]'))
      .toContainText("This wallet can't answer this. The website will be told it wasn't answered.");
  });

  // #973 patient test, findings 2 and 4: the website's library closes this
  // tab the moment a reply arrives, so the reason is held until OK.
  for (const [code, words] of [
    ['sample_only', 'only the sample records'],
    ['no_records', 'no records on your account yet'],
    // Without the list of sets to pick from (the picker:
    // careagents-checkin-wallet-pick.spec.ts).
    ['several_records', 'records from more than one place'],
    ['records_not_ready', "records aren't ready to share yet"],
    ['records_disconnected', 'Your records are disconnected'],
    ['not_your_records', 'Check-in shares your own records'],
    ['unavailable', "couldn't read your records just now"],
  ]) {
    test(`a can't-answer reason waits for OK: ${code}`, async ({ page }) => {
      await serveRelay(page, { error: code }, code === 'unavailable' ? 502 : 409);
      const wallet = await checkIn(page, { items: REQUEST.items.slice(0, 6) });
      await expect(wallet.locator('#wallet-done-text')).toContainText(words);
      await expect(wallet.locator('#wallet-ok')).toBeVisible();
      await wallet.waitForTimeout(1000);
      expect(await page.evaluate(() => (window as any).__result)).toBeUndefined();
      await wallet.locator('#wallet-ok').click();
      await expect.poll(() => page.evaluate(() => (window as any).__result),
        { timeout: 15_000 }).toBeDefined();
      const r = JSON.stringify(await page.evaluate(() => (window as any).__result));
      expect(r).toContain("couldn't share records this time");
      expect(r).not.toContain(code);
    });
  }

  test('a date reads as words', async ({ page }) => {
    await page.goto('/');
    const got = await page.evaluate(async () => {
      const m = await import('/static/checkin_answer.js');
      return ['1985-07-04', '2025-10-01T09:30:00-05:00', '1985-07', '1985',
        'soon', null].map((v) => m.day(v));
    });
    expect(got).toEqual(['4 July 1985', '1 October 2025', 'July 1985', '1985',
      '', '']);
  });

  test('the code screen can send a new code or go back to the email',
    async ({ page }) => {
      await page.goto('/');
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'),
        page.evaluate(() => { window.open('/checkin/wallet'); }),
      ]);
      await blockThirdParty(wallet);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.setViewportSize({ width: 375, height: 812 });
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'signin-375.png') });
      }
      const email = uniqueEmail();
      await wallet.locator('#signin-email').fill(email);
      await wallet.locator('#signin-email-btn').click();
      await expect(wallet.locator('#signin-code')).toBeVisible();
      await expect(wallet.locator('#signin-code-note')).toContainText(email);
      if (process.env.CHECKIN_SHOTS) {
        await wallet.setViewportSize({ width: 375, height: 812 });
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'code-375.png') });
      }
      // A new code at once is held by the resend cooldown, and says so.
      const resend = wallet.waitForRequest((r) =>
        r.url().endsWith('/api/auth/email') && r.method() === 'POST');
      await wallet.locator('#signin-resend').click();
      await resend;
      await expect(wallet.locator('#signin-code-note'))
        .toContainText('You can ask for a new one in');
      await wallet.locator('#signin-other-email').click();
      await expect(wallet.locator('#signin-start')).toBeVisible();
      await expect(wallet.locator('#signin-code')).toBeHidden();
      await expect(wallet.locator('#signin-code-input')).toHaveValue('');
    });

  test('waiting for a request that never comes offers a next step',
    async ({ page }) => {
      await page.goto('/');
      const [wallet] = await Promise.all([
        page.waitForEvent('popup'),
        page.evaluate(() => { window.open('/checkin/wallet?wait=300'); }),
      ]);
      await blockThirdParty(wallet);
      await signInInTab(wallet);
      await expect(wallet.locator('#wallet-waiting')).toBeVisible();
      await expect(wallet.locator('#wallet-waiting-late')).toBeVisible();
      await expect(wallet.locator('#wallet-waiting-late'))
        .toContainText('start check-in again');
      if (process.env.CHECKIN_SHOTS) {
        await wallet.setViewportSize({ width: 375, height: 812 });
        await wallet.screenshot({ fullPage: true,
          path: path.join(process.env.CHECKIN_SHOTS, 'waiting-late-375.png') });
      }
    });

  // The records are the account's own now, not the synthetic test patient,
  // so the wallet adds no test marker of its own (#960's note: the
  // real-record export drops it). The wallet passes a record's own tag
  // through, but the engine's export does not keep one today.
  test('the wallet tags no Bundle as test data', async ({ page }) => {
    await page.goto('/');
    const tags = await page.evaluate(async ({ request, bundle }) => {
      const m = await import('/static/checkin_answer.js');
      const res = m.buildResponse(request,
        m.matchItems(request, [], m.prepareRecords(bundle)),
        Object.fromEntries(request.items.map((i: any) => [i.id, true])));
      return res.artifacts.map((a: any) =>
        ((a.value.meta && a.value.meta.tag) || []).map((t: any) => t.code));
    }, { request: REQUEST, bundle: FIXTURE });
    expect(tags.length).toBe(6);
    for (const t of tags) expect(t).toEqual([]);
  });

  // FHI-55: the organizers' form-phq2.json request, as published. The
  // questions come from its inline Questionnaire; the answers go back to
  // the check-in page as one QuestionnaireResponse and nowhere else.
  const PHQ2 = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
    'fixtures', 'checkin-request-form-phq2.json'), 'utf8'));
  const CANONICAL = PHQ2.items[1].content.questionnaireCanonical;
  const phq2 = (wallet: Page) => wallet.locator('#wallet-items li[data-item=phq2]');
  const done = (page: Page) => expect.poll(
    () => page.evaluate(() => (window as any).__result), { timeout: 15_000 });

  test('fills in the PHQ-2 sent inline and sends one QuestionnaireResponse',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const req = structuredClone(PHQ2);
      req.items[1].content.questionnaireCanonical = `${CANONICAL}|1`;
      const wallet = await checkIn(page, req);
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      const form = phq2(wallet);
      await expect(form.locator('legend')).toHaveText([
        '1. Little interest or pleasure in doing things',
        '2. Feeling down, depressed, or hopeless']);
      // Nothing picked, and the item starts at "Don't share".
      await expect(form.locator('fieldset input:checked')).toHaveCount(0);
      await expect(form.locator('.checkin-choice input[value=decline]')).toBeChecked();

      // Chosen with a question unanswered: said, not sent in part.
      await form.locator('.checkin-choice input[value=share]').check();
      await form.locator('fieldset').nth(0).getByLabel('Several days').check();
      await wallet.locator('#wallet-send').click();
      await expect(wallet.locator('#wallet-review-err')).toContainText('1 question in');
      await expect(wallet.locator('#wallet-review')).toBeVisible();

      await form.locator('fieldset').nth(1).getByLabel('Nearly every day').check();
      await wallet.locator('#wallet-send').click();
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.requestStatus).toEqual([
        { item: 'patient', status: 'declined' },
        { item: 'phq2', status: 'fulfilled' }]);
      const [art] = r.json.artifacts;
      expect(art.fulfills).toEqual(['phq2']);
      expect(art.value.resourceType).toBe('QuestionnaireResponse');
      expect(art.value.questionnaire).toBe(`${CANONICAL}|1`);
      expect(art.value.item.map((i: any) => [i.linkId, i.answer[0].valueCoding.code]))
        .toEqual([['phq2-1', 'LA6569-3'], ['phq2-2', 'LA6571-9']]);
      // The relay was asked for the Patient only: the answers stay in the tab.
      expect(asked).toEqual([{ resource_types: ['Patient'] }]);
    });

  test('the form can be declined', async ({ page }) => {
    await serveRelay(page);
    const wallet = await checkIn(page, PHQ2);
    await phq2(wallet).locator('fieldset').nth(0).getByLabel('Not at all').check();
    // Chosen, not merely where it started: a click, which check() skips on
    // a radio that is already on.
    await phq2(wallet).locator('.checkin-choice input[value=decline]').click();
    await wallet.locator('#wallet-send').click();
    await done(page).toMatchObject({ status: 'completed' });
    const r = await page.evaluate(() => (window as any).__result);
    expect(r.json.artifacts).toEqual([]);
    expect(r.json.requestStatus.find((s: any) => s.item === 'phq2').status)
      .toBe('declined');
  });

  // QA #1030: on a phone the Send button is far below the top of the card.
  // toContainText passes whether or not the message can be seen. #1061
  // patient test: on a long form the message alone did not say which
  // question, so the blank one is marked, scrolled to and focused, and the
  // message (announced, role=alert) gives the count.
  test('at phone width, the unanswered question is marked, on screen and focused',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, PHQ2);
      await wallet.setViewportSize({ width: 375, height: 812 });
      await expect(wallet.locator('#wallet-review')).toBeVisible();
      const form = phq2(wallet);
      await form.locator('.checkin-choice input[value=share]').check();
      await form.locator('fieldset').nth(0).getByLabel('Several days').check();
      await wallet.locator('#wallet-send').click();
      const msg = wallet.locator('#wallet-review-err');
      await expect(msg).toHaveAttribute('role', 'alert');
      await expect(msg).toContainText('1 question in');
      await expect(msg).toContainText('still needs an answer. The first one is highlighted.');
      await expect(msg).toContainText("or choose Don't share for it.");
      const blank = form.locator('fieldset').nth(1);
      await expect(blank).toHaveAttribute('aria-invalid', 'true');
      await expect(form.locator('fieldset').nth(0)).not.toHaveAttribute('aria-invalid', 'true');
      await expect(blank).toBeInViewport({ timeout: 2_000 });
      await expect(blank.locator('input').first()).toBeFocused();
      // Answering it clears the mark.
      await blank.getByLabel('Not at all').check();
      await expect(blank).not.toHaveAttribute('aria-invalid', 'true');
    });

  // #1030 product decision: answers given while the item still sits at its
  // starting "Don't share" are not dropped without a word. The person picks.
  test('answered but never chose Share: asked, on screen, then a real choice',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, PHQ2);
      await wallet.setViewportSize({ width: 375, height: 812 });
      const form = phq2(wallet);
      await form.locator('fieldset').nth(0).getByLabel('Several days').check();
      await form.locator('fieldset').nth(1).getByLabel('Not at all').check();
      await wallet.locator('#wallet-send').click();
      const msg = wallet.locator('#wallet-review-err');
      await expect(msg).toBeInViewport({ timeout: 2_000 });
      await expect(msg).toContainText("but chose Don't share. Choose Share to send your answers, or Don't share to leave them out.");
      expect(await page.evaluate(() => (window as any).__result)).toBeUndefined();
      // Don't share, chosen this time: declined, nothing sent.
      await form.locator('.checkin-choice input[value=decline]').click();
      await wallet.locator('#wallet-send').click();
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.find((s: any) => s.item === 'phq2').status)
        .toBe('declined');
    });

  // QA #1030 (ab1d524a): the message above says "or Don't share to leave
  // them out". By keyboard, Space on the Don't share that is already on
  // fires no click; the keydown counts it as the choice it is.
  test('answered but never chose Share: Don\'t share chosen by keyboard unblocks Send',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, PHQ2);
      const form = phq2(wallet);
      await form.locator('fieldset').nth(0).getByLabel('Several days').check();
      await form.locator('fieldset').nth(1).getByLabel('Not at all').check();
      await wallet.locator('#wallet-send').click();
      await expect(wallet.locator('#wallet-review-err')).toContainText("but chose Don't share");
      await form.locator('.checkin-choice input[value=decline]').focus();
      await wallet.keyboard.press('Space');
      await wallet.locator('#wallet-send').focus();
      await wallet.keyboard.press('Enter');
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.find((s: any) => s.item === 'phq2').status)
        .toBe('declined');
    });

  // QA #1030 round 2: only Space or Enter on the option, or a pointer, is a
  // choice. Moving focus onto it, or any other key there, is not: Send
  // stays blocked and nothing is sent.
  test('answered but never chose Share: Tab and other keys are not a choice',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, PHQ2);
      const form = phq2(wallet);
      await form.locator('fieldset').nth(0).getByLabel('Several days').check();
      await form.locator('fieldset').nth(1).getByLabel('Not at all').check();
      // Tab from the last answer lands on the Don't share that is on.
      await form.locator('fieldset').nth(1).getByLabel('Nearly every day').focus();
      await wallet.keyboard.press('Tab');
      await expect(form.locator('.checkin-choice input[value=decline]')).toBeFocused();
      for (const key of ['Shift', 'a', 'Escape', 'Home', 'End', 'Tab']) {
        await form.locator('.checkin-choice input[value=decline]').focus();
        await wallet.keyboard.press(key);
      }
      await wallet.locator('#wallet-send').focus();
      await wallet.keyboard.press('Enter');
      await expect(wallet.locator('#wallet-review-err')).toContainText("but chose Don't share");
      expect(await page.evaluate(() => (window as any).__result)).toBeUndefined();
    });

  test('the share choice is its own labelled control, apart from the answers',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, PHQ2);
      const choice = phq2(wallet).locator('.checkin-choice');
      await expect(choice.locator('.checkin-choice-label'))
        .toHaveText('Send your answers to the website?');
      await expect(choice).toHaveAttribute('role', 'radiogroup');
      await expect(choice.locator('fieldset')).toHaveCount(0);
      await expect(choice.locator('input[value=decline]')).toBeChecked();
    });

  test("a form's words are shown as text, never as markup", async ({ page }) => {
    await serveRelay(page);
    const req = structuredClone(PHQ2);
    const q = req.items[1].content.questionnaire;
    q.item[1].text = '<img src=x onerror="window.__pwned=1">Interest';
    q.item[1].answerOption[0].valueCoding.display = '<b>Not at all</b>';
    const wallet = await checkIn(page, req);
    const form = phq2(wallet);
    await expect(form.locator('legend').first())
      .toHaveText('1. <img src=x onerror="window.__pwned=1">Interest');
    await expect(form.getByText('<b>Not at all</b>').first()).toBeVisible();
    await expect(form.locator('img, b')).toHaveCount(0);
    expect(await wallet.evaluate(() => (window as any).__pwned)).toBeUndefined();
  });

  // #1030 security F3 follow-up: a heading of the website's own is said to
  // be its, isolated and capped, never in the wallet's voice.
  test("a form's title is shown as the website's, isolated", async ({ page }) => {
    await serveRelay(page);
    const req = structuredClone(PHQ2);
    req.items[1].title = 'CareAgents verified clinic';
    const wallet = await checkIn(page, req);
    const heading = phq2(wallet).locator('.checkin-item-title');
    // Its label inline, on the same line as the title (QA ruling: every
    // piece of website text carries its own visible label).
    await expect(heading).toHaveText('From the website: \u2068CareAgents verified clinic\u2069');
    await expect(heading.locator('.checkin-website-label')).toHaveText('From the website:');
    await expect(heading.locator('.checkin-website-name')).toHaveText(
      '\u2068CareAgents verified clinic\u2069');
    await expect(heading).toHaveClass(/checkin-website-title/);
    // And the lines under it carry theirs.
    await expect(phq2(wallet).locator('.checkin-website-says')).toHaveText('The website asks:');
    await expect(phq2(wallet).locator('.checkin-from-website')).toHaveCount(1);
    // Ours stays ours.
    await expect(wallet.locator('#wallet-items li[data-item=patient] .checkin-item-title'))
      .toHaveText('Your name, birth date and contact details');
  });

  test("a long website title is capped at 80, the isolate intact", async ({ page }) => {
    await serveRelay(page);
    const req = structuredClone(PHQ2);
    req.items[1].title = `CareAgents ${'x'.repeat(200)}‮evil`;
    const wallet = await checkIn(page, req);
    const name = phq2(wallet).locator('.checkin-item-title .checkin-website-name');
    await expect(name).toHaveText(/^\u2068[^\u2069]{80}\u2069$/);
    expect(await name.textContent()).not.toContain('evil');
  });

  test('a form too large to trust is answered unsupported', async ({ page }) => {
    await serveRelay(page);
    const req = structuredClone(PHQ2);
    const q = req.items[1].content.questionnaire;
    for (let n = 0; n < 60; n++)
      q.item.push({ ...structuredClone(q.item[1]), linkId: `extra-${n}` });
    const wallet = await checkIn(page, req);
    await expect(phq2(wallet)).toContainText("This wallet can't answer this. The website will be told it wasn't answered.");
    await expect(phq2(wallet).locator('fieldset')).toHaveCount(0);
    await wallet.locator('#wallet-send').click();
    await done(page).toMatchObject({ status: 'completed' });
    const r = await page.evaluate(() => (window as any).__result);
    expect(r.json.requestStatus.find((s: any) => s.item === 'phq2').status)
      .toBe('unsupported');
  });

  // #1049: an eye-care Verifier's SPEED form, its questions in two groups
  // (the Verifier's published request structure, with made-up wording).
  const SPEED = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
    'fixtures', 'checkin-request-form-speed.json'), 'utf8'));
  const speed = (wallet: Page) => wallet.locator('#wallet-items li[data-item=speed]');

  test('fills in the grouped SPEED form and nests the answers by group',
    async ({ page }) => {
      const asked = await serveRelay(page);
      const wallet = await checkIn(page, SPEED);
      const form = speed(wallet);
      // Each group's heading, then its own note, then its questions.
      await expect(form.locator('.checkin-group-title')).toHaveText([
        'How often', 'How bad']);
      await expect(form.locator('.checkin-group').nth(0).locator('.checkin-group-note'))
        .toHaveText('0 Never · 1 Rarely · 2 Often · 3 Always');
      for (const [g, word] of [[0, 'how often'], [1, 'how bad']] as const) {
        await expect(form.locator('.checkin-group').nth(g).locator('legend')).toHaveText(
          ['A', 'B', 'C', 'D'].map((s) => `Symptom ${s}, ${word}`));
      }
      await expect(form.locator('fieldset input:checked')).toHaveCount(0);

      await form.locator('.checkin-choice input[value=share]').check();
      const sets = form.locator('fieldset.checkin-question');
      await expect(sets).toHaveCount(8);
      for (let n = 0; n < 7; n++) await sets.nth(n).locator('input').first().check();
      // One left: said, not sent in part.
      await wallet.locator('#wallet-send').click();
      await expect(wallet.locator('#wallet-review-err')).toContainText('1 question in');
      await sets.nth(7).getByLabel('Very severe').check();
      await sets.nth(1).getByLabel('Always').check();
      await wallet.locator('#wallet-send').click();
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.requestStatus).toEqual([
        { item: 'patient', status: 'declined' },
        { item: 'speed', status: 'fulfilled' }]);
      const qr = r.json.artifacts[0].value;
      expect(qr.questionnaire).toBe(SPEED.items[1].content.questionnaireCanonical);
      expect(qr.item.map((g: any) => [g.linkId, g.answer,
        g.item.map((i: any) => [i.linkId, i.answer[0].valueCoding.code])])).toEqual([
        ['frequency', undefined, [['frequency-dryness', 'frequency-0'],
          ['frequency-soreness', 'frequency-3'], ['frequency-burning', 'frequency-0'],
          ['frequency-fatigue', 'frequency-0']]],
        ['severity', undefined, [['severity-dryness', 'severity-0'],
          ['severity-soreness', 'severity-0'], ['severity-burning', 'severity-0'],
          ['severity-fatigue', 'severity-4']]]]);
      expect(JSON.stringify(qr)).not.toContain('ordinalValue');
      expect(asked).toEqual([{ resource_types: ['Patient'] }]);
    });

  // The Verifier's contact lens trial form (the fixture is its structure
  // exactly): its single choices are answered; its free text, numbers and
  // pick-several are each shown, in place, as not answered here, and left
  // out of what is sent. It used to be unsupported whole.
  test('the contact lens form answers its choices and leaves out the rest, saying so',
    async ({ page }) => {
      await serveRelay(page);
      const req = JSON.parse(fs.readFileSync(path.join(__dirname, '..',
        'fixtures', 'checkin-request-form-cl-trial.json'), 'utf8'));
      const wallet = await checkIn(page, req);
      await wallet.setViewportSize({ width: 375, height: 812 });
      const form = wallet.locator('#wallet-items li[data-item=eyeClTrial]');
      await expect(form.locator('.checkin-group')).toHaveCount(5);
      const skipped = ['trial-lens', 'days-worn', 'hours-worn', 'hours-comfortable',
        'problems', 'concerns', 'improvement'];
      const q = req.items[1].content.questionnaire;
      const textOf = (id: string) => q.item.flatMap((g: any) => [g, ...(g.item || [])])
        .find((i: any) => i.linkId === id).text;
      await expect(form.locator('.checkin-skipped-text')).toHaveText(skipped.map(textOf));
      await expect(form.locator('.checkin-skipped-why')).toHaveCount(7);
      for (const why of await form.locator('.checkin-skipped-why').allTextContents()) {
        expect(why).toBe("This page can't answer the next question. Nothing is sent for it.");
      }
      // In its place: the lens question first in its group, before its choices.
      const wear = form.locator('.checkin-group').nth(1);
      await expect(wear.locator('.checkin-skipped, fieldset').first())
        .toHaveClass(/checkin-skipped/);
      // Nothing to type into, and every choice a radio button.
      await expect(form.locator('input:not([type=radio]), textarea, select')).toHaveCount(0);
      expect(await wallet.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await wallet.screenshot({ path: test.info().outputPath('cl-trial-375.png'), fullPage: true });

      await form.locator('.checkin-choice input[value=share]').check();
      const sets = form.locator('fieldset.checkin-question');
      await expect(sets).toHaveCount(19);
      // #1061 patient test: three left blank on a long form. Each is
      // marked, the message gives the count, and the first is on screen
      // and focused, wherever Send is.
      for (let n = 0; n < 16; n++) await sets.nth(n).locator('input').first().check();
      await wallet.locator('#wallet-send').click();
      await expect(wallet.locator('#wallet-review-err')).toContainText(
        'still need an answer. The first one is highlighted.');
      await expect(wallet.locator('#wallet-review-err')).toContainText('3 questions in');
      await expect(form.locator('fieldset[aria-invalid=true]')).toHaveCount(3);
      await expect(form.locator('fieldset.checkin-missing')).toHaveCount(3);
      await expect(sets.nth(16)).toHaveAttribute('aria-invalid', 'true');
      await expect(sets.nth(16)).toBeInViewport();
      await expect(sets.nth(16).locator('input').first()).toBeFocused();
      await wallet.screenshot({ path: test.info().outputPath('cl-trial-missing-375.png') });
      for (let n = 16; n < 19; n++) await sets.nth(n).locator('input').first().check();
      await expect(form.locator('fieldset[aria-invalid=true]')).toHaveCount(0);
      await wallet.locator('#wallet-send').click();
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.requestStatus.find((s: any) => s.item === 'eyeClTrial').status)
        .toBe('fulfilled');
      const qr = r.json.artifacts[0].value;
      expect(qr.status).toBe('completed');
      expect(qr.item.map((g: any) => g.linkId)).toEqual(
        ['safety', 'wear', 'vision', 'comfort', 'impression']);
      const sent = qr.item.flatMap((g: any) => g.item.map((i: any) => i.linkId));
      expect(sent).toHaveLength(19);
      for (const id of skipped) expect(sent).not.toContain(id);
    });

  // QA #1052: the grouped form, declined after some answers, sends a
  // declined status and none of the answers given.
  test('the grouped SPEED form can be declined after answering some of it',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, SPEED);
      const form = speed(wallet);
      await form.locator('.checkin-choice input[value=share]').check();
      const sets = form.locator('fieldset.checkin-question');
      for (let n = 0; n < 5; n++) await sets.nth(n).locator('input').first().check();
      await form.locator('.checkin-choice input[value=decline]').click();
      await wallet.locator('#wallet-send').click();
      await done(page).toMatchObject({ status: 'completed' });
      const r = await page.evaluate(() => (window as any).__result);
      expect(r.json.artifacts).toEqual([]);
      expect(r.json.requestStatus.find((s: any) => s.item === 'speed').status)
        .toBe('declined');
    });

  // QA #1052: each group is a named group for a screen reader, holding only
  // its own questions.
  test("each SPEED group is named by its heading and holds only its questions",
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, SPEED);
      const form = speed(wallet);
      for (const [name, word] of [['How often', 'how often'], ['How bad', 'how bad']]) {
        const g = form.getByRole('group', { name, exact: true });
        await expect(g).toHaveCount(1);
        await expect(g.locator('legend')).toHaveText(
          ['A', 'B', 'C', 'D'].map((s) => `Symptom ${s}, ${word}`));
      }
    });

  // QA #1052: on a phone (375px), the grouped form fits the screen, every
  // option is a full-size tap target, and a question left blank in the
  // FIRST group (not only the last) stops the send with the message on screen.
  test('at phone width, the grouped form fits and a blank in the first group blocks Send',
    async ({ page }) => {
      await serveRelay(page);
      const wallet = await checkIn(page, SPEED);
      await wallet.setViewportSize({ width: 375, height: 812 });
      const form = speed(wallet);
      await expect(form.locator('.checkin-group-title').first()).toBeVisible();
      expect(await wallet.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      for (const el of await form.locator(
        '.checkin-group-title, .checkin-group-note, fieldset.checkin-question').all()) {
        const box = await el.boundingBox();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(375);
      }
      const heights = await form.locator('fieldset.checkin-question label')
        .evaluateAll((ls) => ls.map((l) => l.getBoundingClientRect().height));
      expect(heights.length).toBe(4 * 4 + 4 * 5);
      for (const h of heights) expect(h).toBeGreaterThanOrEqual(44);
      await wallet.screenshot({ path: test.info().outputPath('speed-375.png'), fullPage: true });

      await form.locator('.checkin-choice input[value=share]').check();
      const sets = form.locator('fieldset.checkin-question');
      for (let n = 1; n < 8; n++) await sets.nth(n).locator('input').first().check();
      await wallet.locator('#wallet-send').click();
      const msg = wallet.locator('#wallet-review-err');
      await expect(msg).toContainText('1 question in');
      // The blank question itself is taken to, far above Send (#1061).
      await expect(sets.nth(0)).toBeInViewport({ timeout: 2_000 });
      await expect(sets.nth(0)).toHaveAttribute('aria-invalid', 'true');
      await expect(sets.nth(0).locator('input').first()).toBeFocused();
      expect(await page.evaluate(() => (window as any).__result)).toBeUndefined();
    });
});
