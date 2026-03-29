import { test, expect } from '@playwright/test';

test.describe('Landing page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('has correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/HealthClaw Guardrails/);
  });

  test('navbar has Home and Health Data Dashboard links', async ({ page }) => {
    await expect(page.getByRole('link', { name: 'Home' })).toBeVisible();
    // Scope to navbar to avoid strict-mode conflict with hero CTA button
    const nav = page.locator('nav');
    await expect(nav.getByRole('link', { name: /Health Data Dashboard/ })).toBeVisible();
  });

  test('hero title is visible', async ({ page }) => {
    await expect(page.locator('h1.hero-title')).toContainText('HealthClaw Guardrails');
  });

  test('Try the Dashboard button navigates to dashboard', async ({ page }) => {
    await page.getByRole('link', { name: /Health Data Dashboard/ }).first().click();
    await expect(page).toHaveURL('/r6-dashboard');
  });

  test('PHI before/after section is visible', async ({ page }) => {
    await expect(page.locator('.phi-compare')).toBeVisible();
  });

  test('6-step story section shows all steps', async ({ page }) => {
    await expect(page.getByText('1. Read Patient Record')).toBeVisible();
    await expect(page.getByText('2. Propose Write')).toBeVisible();
    await expect(page.getByText('6. Commit + Audit Trail')).toBeVisible();
  });

  test('feature cards are visible', async ({ page }) => {
    await expect(page.getByText('Security Patterns')).toBeVisible();
    await expect(page.getByText('12 MCP Tools')).toBeVisible();
    await expect(page.getByText('Clinical Safety')).toBeVisible();
  });

  test('discovery endpoint links are present', async ({ page }) => {
    await expect(page.locator('a[href="/r6/fhir/metadata"]')).toBeVisible();
    await expect(page.locator('a[href="/r6/fhir/health"]')).toBeVisible();
  });
});
