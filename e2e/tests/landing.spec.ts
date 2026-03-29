import { test, expect } from '@playwright/test';

test.describe('Landing page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('has correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/HealthClaw Guardrails/);
  });

  test('navbar has Home and Health Data Dashboard links', async ({ page }) => {
    const nav = page.locator('#main-nav');
    await expect(nav.getByRole('link', { name: 'Home' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Health Data Dashboard' })).toBeVisible();
  });

  test('hero title is visible', async ({ page }) => {
    await expect(page.locator('h1.hero-title')).toBeVisible();
    await expect(page.locator('h1.hero-title')).toContainText('AI agents');
  });

  test('Try the Live Dashboard button navigates to dashboard', async ({ page }) => {
    await page.getByRole('link', { name: 'Try the Live Dashboard' }).click();
    await expect(page).toHaveURL('/r6-dashboard');
  });

  test('PHI before/after section is visible', async ({ page }) => {
    await expect(page.locator('.phi-compare')).toBeVisible();
  });

  test('guardrail pipeline cards show all 6 layers', async ({ page }) => {
    await expect(page.getByText('PHI Redacted')).toBeVisible();
    await expect(page.getByText('$validate Gate')).toBeVisible();
    await expect(page.getByText('Audit Trail')).toBeVisible();
  });

  test('audience cards are visible', async ({ page }) => {
    await expect(page.getByText('AI Agent Developer')).toBeVisible();
    await expect(page.getByText('Patient / Consumer')).toBeVisible();
    await expect(page.getByText('Health System / Payer')).toBeVisible();
  });

  test('discovery endpoint links are present in footer', async ({ page }) => {
    await expect(page.locator('a[href="/r6/fhir/metadata"]')).toBeVisible();
    await expect(page.locator('a[href="/r6/fhir/health"]')).toBeVisible();
  });
});
