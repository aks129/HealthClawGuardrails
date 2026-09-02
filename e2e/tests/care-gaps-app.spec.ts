/**
 * Care Gaps MCP App — the page must not say "0 Due" over a patient it never
 * looked at (#538), plus the client half of #535.
 *
 * Verified live 2026-08-31: a tenant with more than one Patient gets
 * `unevaluated: "ambiguous-patient"` from $care-gaps and a note that nothing
 * was examined. The page dropped the note and drew a "0 Due" tile over six
 * cards saying "date of birth unknown" — artefacts of the call, not facts
 * about the person. The Python guards (tests/test_mcp_app_care_gaps.py) pin
 * the source's shape; this is the only place the render is watched paint.
 *
 * What it proves:
 *   1. the seeded demo tenant really does answer ambiguous-patient — asserted
 *      against the API first, so a fixture change reads as a fixture change
 *      and not as a page regression
 *   2. the page shows the engine's own note, no "0 Due", and no per-rule cards
 *   3. Enter in the tenant box submits, and a 401 is explained as a spelling
 *      or credential problem, not as a missing step-up token
 *
 * Requires the demo history: `flask --app main seed-demo-history`, which the
 * e2e webServer runs at boot.
 */
import { test, expect } from '@playwright/test';

const TENANT = 'desktop-demo';
const headers = { 'X-Tenant-Id': TENANT };

function param(parameters: any, name: string) {
  const p = (parameters.parameter || []).find((x: any) => x.name === name);
  return p ? JSON.parse(p.valueString) : null;
}

test.describe('care-gaps MCP App', () => {
  test('an unevaluated tenant shows the engine note, not "0 Due"', async ({ page, request }) => {
    // --- 1. The fixture, checked at the API --------------------------------
    const res = await request.post('/r6/fhir/Patient/$care-gaps', {
      headers: { ...headers, 'Content-Type': 'application/json' }, data: {},
    });
    expect(res.ok()).toBeTruthy();
    const consumer = param(await res.json(), 'consumerSummary');
    expect(consumer.unevaluated, 'fixture: desktop-demo must hold more than one Patient')
      .toBe('ambiguous-patient');
    expect(consumer.unevaluated_note).toContain('Nothing was examined');

    // --- 2. The page -------------------------------------------------------
    await page.goto(`/r6/fhir/mcp-apps/care-gaps/?tenant_id=${TENANT}`, {
      waitUntil: 'load',
    });
    const box = page.locator('.consumer');
    await expect(box).toBeVisible();
    await expect(box.locator('h3')).toHaveText('Not evaluated');
    await expect(box).toContainText('Nothing was examined');

    // No count on this path is a count of anything. "0 Due" is the false
    // statement the engine's docstring warns about.
    const due = page.locator('.stat.due .num');
    await expect(due).toBeVisible();
    await expect(due).not.toHaveText('0');
    await expect(due).toHaveText('—');
    await expect(due).toHaveAttribute('aria-label', 'not evaluated');

    // Six "date of birth unknown" cards described a record nobody opened.
    await expect(page.locator('.card')).toHaveCount(0);
    await expect(page.locator('#content')).toContainText('No screenings were evaluated.');
  });

  test('Enter submits, and a 401 is explained without "step-up"', async ({ page }) => {
    // The e2e server runs with READ_AUTH_ENABLED unset, so a misspelled
    // tenant reads here as an empty tenant (200, no-patient). Production
    // runs with it on and answers 401 — the same 401 for "no such tenant"
    // and "not yours", by design (existence disclosure). The intercept
    // reproduces that production answer for the misspelled tenant only; what
    // is under test is how the page explains it, and that Enter sent it.
    await page.route(
      (url) => url.pathname.endsWith('/Patient/$care-gaps'),
      async (route) => {
        if (route.request().headers()['x-tenant-id'] !== 'desktop-dmo') {
          return route.continue();
        }
        await route.fulfill({
          status: 401,
          contentType: 'application/fhir+json',
          body: JSON.stringify({
            resourceType: 'OperationOutcome',
            issue: [{ severity: 'error', code: 'security',
                      diagnostics: "Read access to tenant 'desktop-dmo' requires authentication" }],
          }),
        });
      });

    await page.goto('/r6/fhir/mcp-apps/care-gaps/', { waitUntil: 'load' });
    const input = page.locator('#tenant-input');
    await input.fill('desktop-dmo');
    await input.press('Enter');

    const err = page.locator('#err');
    await expect(err).toBeVisible();
    await expect(err).toContainText('HTTP 401');
    await expect(err).toContainText('spelled correctly');
    await expect(err).not.toContainText('step-up');
  });
});
