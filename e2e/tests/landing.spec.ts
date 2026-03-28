import { test, expect } from '@playwright/test';

test.describe('Landing page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('has correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/MCP Guardrail Patterns/);
  });

  test('navbar has Home and R6 Dashboard links', async ({ page }) => {
    await expect(page.getByRole('link', { name: 'Home' })).toBeVisible();
    await expect(page.getByRole('link', { name: /R6 Dashboard/ })).toBeVisible();
  });

  test('hero title is visible', async ({ page }) => {
    await expect(page.locator('h1.hero-title')).toContainText('MCP Guardrail Patterns for Healthcare AI');
  });

  test('Watch the Demo button is visible', async ({ page }) => {
    await expect(page.locator('#btn-watch-demo')).toBeVisible();
    await expect(page.locator('#btn-watch-demo')).toContainText('Watch the Demo');
  });

  test('Try the Dashboard button navigates to dashboard', async ({ page }) => {
    await page.getByRole('link', { name: /Try the Dashboard/ }).click();
    await expect(page).toHaveURL('/r6-dashboard');
  });

  test('demo animation auto-plays — left panel fields appear', async ({ page }) => {
    // Demo fires 1s after load; each field has a 300ms stagger
    await expect(page.locator('#demo-left .demo-field.visible').first()).toBeVisible({ timeout: 6000 });
  });

  test('demo animation auto-plays — right redacted panel appears', async ({ page }) => {
    await expect(page.locator('#demo-right .demo-field.visible').first()).toBeVisible({ timeout: 6000 });
  });

  test('audit trail entry appears after demo', async ({ page }) => {
    await expect(page.locator('#demo-audit.visible')).toBeVisible({ timeout: 6000 });
    await expect(page.locator('#demo-audit')).toContainText('AuditEvent');
  });

  test('"try it yourself" link appears and points to dashboard', async ({ page }) => {
    await expect(page.locator('#demo-try-link.visible')).toBeVisible({ timeout: 6000 });
    await expect(page.locator('#demo-try-link a')).toHaveAttribute('href', /r6-dashboard/);
  });

  test('replay button re-runs animation', async ({ page }) => {
    // Wait for animation to finish
    await expect(page.locator('#demo-audit.visible')).toBeVisible({ timeout: 6000 });
    await page.locator('.demo-replay').click();
    // Fields should reset and re-animate
    await expect(page.locator('#demo-left .demo-field.visible').first()).toBeVisible({ timeout: 6000 });
  });

  test('comparison table shows project features', async ({ page }) => {
    const table = page.locator('.comparison-table');
    await expect(table).toBeVisible();
    await expect(table).toContainText('This Project');
    await expect(table).toContainText('PHI redaction on reads');
    await expect(table).toContainText('Human-in-the-loop');
  });

  test('6-step story section shows all steps', async ({ page }) => {
    await expect(page.getByText('1. Read Patient Record')).toBeVisible();
    await expect(page.getByText('2. Propose Observation')).toBeVisible();
    await expect(page.getByText('6. Commit + Audit Trail')).toBeVisible();
  });

  test('feature cards are visible', async ({ page }) => {
    await expect(page.getByText('Security Patterns')).toBeVisible();
    await expect(page.getByText('10 MCP Tools')).toBeVisible();
    await expect(page.getByText('Clinical Safety')).toBeVisible();
  });

  test('discovery endpoint links are present', async ({ page }) => {
    await expect(page.locator('a[href="/r6/fhir/metadata"]')).toBeVisible();
    await expect(page.locator('a[href="/r6/fhir/health"]')).toBeVisible();
  });
});
