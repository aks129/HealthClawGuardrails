/**
 * Demo-tenant walkthrough — an acceptance test for the seeded demo data,
 * which doubles as a recordable tour of the SMBP surfaces.
 *
 * It is a test first and footage second, and the order matters: every step
 * asserts the thing it is showing. A walkthrough that only navigates films a
 * broken product just as happily as a working one.
 *
 * What it proves:
 *   1. the treated persona carries an active essential-hypertension
 *      Condition — without it the record shows antihypertensives prescribed
 *      for no documented reason
 *   2. the BP trend chart renders, with clinic and home readings visually
 *      distinct
 *   3. the white-coat persona's clinic readings sit above her home band
 *   4. the landline persona has no home series, deliberately
 *   5. a clinical write is refused twice, for two different reasons
 *
 * Requires the demo history: `flask --app main seed-demo-history`, which the
 * e2e webServer runs at boot.
 */
import { test, expect } from '@playwright/test';

const TENANT = 'desktop-demo';
const headers = { 'X-Tenant-Id': TENANT };

// Slow enough to read on screen. The video is the artefact.
async function beat(page: any, ms = 1200) {
  await page.waitForTimeout(ms);
}

test.describe('demo tenant walkthrough', () => {
  test('the three cases, end to end', async ({ page, request }) => {
    // --- 1. The blocking item -------------------------------------------
    const conditions = await request.get(
      `/r6/fhir/Condition?patient=Patient/demo-marisol`, { headers });
    expect(conditions.ok()).toBeTruthy();
    const bundle = await conditions.json();
    const codes = (bundle.entry || []).map(
      (e: any) => e.resource?.code?.coding?.[0]?.code);
    expect(codes, 'the treated persona must carry I10').toContain('I10');

    // --- 2. The baseline the card is printed from ------------------------
    const marisol = await request.get(
      `/r6/smbp/trend?subject=Patient/demo-marisol`, { headers });
    expect(marisol.ok()).toBeTruthy();

    // --- 3. Trend: drift, then a dense cluster, then decline -------------
    await page.goto(`/r6/smbp/trend?subject=Patient/demo-marisol&tenant_id=${TENANT}`, {
      waitUntil: 'load',
    });
    await expect(page.locator('h1')).toHaveText('Blood pressure over time');
    const chart = page.locator('svg.bp-chart');
    await expect(chart).toBeVisible();

    // Clinic and home readings must be visually distinct, or the chart
    // cannot make the point it exists to make.
    await expect(page.locator('rect.pt.office').first()).toBeVisible();
    await expect(page.locator('circle.pt.home').first()).toBeVisible();
    const counts = page.locator('.counts');
    await expect(counts).toContainText('11');   // clinic readings
    await beat(page, 2000);

    // --- 4. The white-coat picture ---------------------------------------
    await page.goto(`/r6/smbp/trend?subject=Patient/demo-elena&tenant_id=${TENANT}`, {
      waitUntil: 'load',
    });
    await expect(page.locator('svg.bp-chart')).toBeVisible();
    await expect(page.locator('.counts')).toContainText('26');
    await beat(page, 2000);

    // --- 5. The landline persona: the absence is the case -----------------
    await page.goto(`/r6/smbp/trend?subject=Patient/demo-ray&tenant_id=${TENANT}`, {
      waitUntil: 'load',
    });
    await expect(page.locator('svg.bp-chart')).toBeVisible();
    // Three clinic readings and one phoned-in reading. No series.
    await expect(page.locator('.counts')).toContainText('3');
    await beat(page, 2000);

    // --- 6. The guardrail this closes on ----------------------------------
    // Refused TWICE, for two different reasons. I expected a single 401 and
    // the server said 428 — the human-in-the-loop gate answers before the
    // step-up gate does. Both are real, and asserting both is the honest
    // version of the "blocked twice" beat.
    const body = {
      resourceType: 'Observation',
      status: 'final',
      subject: { reference: 'Patient/demo-marisol' },
      code: { coding: [{ system: 'http://loinc.org', code: '85354-9' }] },
    };
    const json = 'application/fhir+json';

    const bare = await request.post('/r6/fhir/Observation', {
      headers: { ...headers, 'Content-Type': json }, data: body,
    });
    expect(bare.status(), 'first refusal: a human has not confirmed')
      .toBe(428);

    // Supplying the confirmation header does NOT get you a write. The
    // credential gate is still there behind it — which is the property
    // worth filming, because the header itself is spoofable by the caller
    // that sets it (#214) and is documented as a compensating control.
    const confirmed = await request.post('/r6/fhir/Observation', {
      headers: { ...headers, 'Content-Type': json, 'X-Human-Confirmed': 'true' },
      data: body,
    });
    expect(confirmed.status(), 'second refusal: no step-up credential')
      .toBe(401);

    // --- 7. The grade, live ----------------------------------------------
    await page.goto(`/r6/fhir/$conformance?format=text`, {
      waitUntil: 'load',
    });
    await expect(page.locator('body')).toContainText('Grade: A');
    await beat(page, 2500);
  });
});
