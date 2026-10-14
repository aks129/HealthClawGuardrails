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
 *     out, nothing on file -> unavailable, a coded "no known allergy" never
 *     sent, unchosen -> declined, a form -> unsupported, and a response the
 *     vendored library's own pre-flight check finds clean.
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

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', '..', 'careagents', 'checkin',
    'us_core_test_patient.json'), 'utf8'));

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

  test('an allergen named in words is still an allergy', async ({ page }) => {
    await page.goto('/');
    for (const text of ['Penicillin', 'Nickel', 'Nuts', 'Known latex sensitivity']) {
      const one = structuredClone(FIXTURE);
      one.entry.find((e: any) => e.resource.resourceType ===
        'AllergyIntolerance').resource.code = { text };
      const r = await answer(page, one, ALL);
      expect(r.status.allergies, text).toBe('fulfilled');
    }
  });

  test('every Bundle it sends is tagged as test data', async ({ page }) => {
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
    for (const t of tags) expect(t).toEqual(['HTEST']);
  });
});
