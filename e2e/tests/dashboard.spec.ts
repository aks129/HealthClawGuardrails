import { test, expect } from '@playwright/test';

/**
 * /r6-dashboard — the live conformance report.
 *
 * This file was rewritten with the page. What it used to assert is worth
 * recording, because the suite was green the whole time the page was broken:
 *
 *   - Eleven tests checked that panels existed by element id. Every panel
 *     drove a POST to /r6/…, which the public host refuses with 405
 *     (api/index.py). An element being visible says nothing about whether
 *     clicking it does anything.
 *   - One test, 'Security Posture panel shows all enforced controls', walked
 *     nine hand-written rows — "Tenant Isolation", "PHI Redaction", "Audit
 *     Trail" — and asserted each was visible beside its green check. Nothing
 *     measured any of them. That test was enforcing the defect: it would have
 *     failed if someone had removed the unbacked claims, and passed through a
 *     total redaction failure.
 *
 * The page is server-rendered now, so most of its content is pinned in
 * tests/test_dashboard_reports_what_it_measured.py, which can also simulate a
 * failed measurement. What is left here is what only a browser can answer.
 */

test.describe('R6 Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/r6-dashboard');
  });

  test('has correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/Guardrail conformance/);
  });

  test('renders the grade with no JavaScript at all', async ({ browser }) => {
    // The point of server-rendering. The old page painted spinners first and
    // filled them from fetch(), so a reader with a slow or blocked script saw
    // an empty scorecard — indistinguishable from a deployment with nothing
    // to report.
    const ctx = await browser.newContext({ javaScriptEnabled: false });
    const page = await ctx.newPage();
    await page.goto('/r6-dashboard');
    await expect(page.locator('.cf-grade')).toHaveText(/^[A-F]$/);
    await expect(page.locator('.cf-figures')).toContainText('Properties passed');
    await ctx.close();
  });

  test('every property row carries a check tape', async ({ page }) => {
    const names = await page.locator('.cf-prop__name').count();
    const tapes = await page.locator('.cf-tape').count();
    expect(names).toBeGreaterThan(0);
    expect(tapes).toBe(names);
    // Each mark is one check, so the marks must outnumber the rows.
    const marks = await page.locator('.cf-tape__m').count();
    expect(marks).toBeGreaterThan(names);
  });

  test('a property opens to reveal its individual checks', async ({ page }) => {
    // Visibility, not count. A closed <details> keeps its children in the
    // DOM — they are rendered and merely not shown, which is also why the
    // check text is in the HTML source for search engines and for a reader
    // who saves the page.
    const first = page.locator('details.cf-prop').first();
    const checks = first.locator('.cf-check');
    await expect(checks.first()).toBeHidden();
    await first.locator('summary').click();
    await expect(checks.first()).toBeVisible();
    await expect(checks.first()).toContainText(/PASS|FAIL/);
  });

  test('the page ships no control that would 405 on the public host', async ({ page }) => {
    // MUTATION: add a <button onclick="fetch('/r6/...', {method:'POST'})">
    // to the template -> red. The read-only deployment refuses every write to
    // a stateful path, so a control there is dead by construction.
    const main = page.locator('#main');
    await expect(main.locator('form[method="post" i]')).toHaveCount(0);
    await expect(main.locator('button')).toHaveCount(0);
    await expect(main.locator('[onclick]')).toHaveCount(0);
  });

  test('states what the grade does not cover', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'What this grade does not cover' }))
      .toBeVisible();
    await expect(page.locator('.cf-limit')).not.toHaveCount(0);
  });

  test('names the host it is running on', async ({ page }) => {
    await expect(page.locator('.cf-host')).toContainText(/stateful|read-only/);
  });

  test('renders without console errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(e.message));
    await page.reload({ waitUntil: 'networkidle' });
    expect(errors).toEqual([]);
  });

  test('does not scroll sideways on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
