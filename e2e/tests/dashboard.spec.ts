import { test, expect } from '@playwright/test';

test.describe('R6 Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/r6-dashboard');
  });

  test('has correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/Health Data Dashboard/);
  });

  test('shows dashboard heading', async ({ page }) => {
    await expect(page.locator('h1')).toContainText('MCP Guardrail Patterns for Healthcare AI');
  });

  test('stat cards are rendered', async ({ page }) => {
    for (const id of ['stat-status', 'stat-version', 'stat-fhir', 'stat-resources', 'stat-operations', 'stat-mode']) {
      await expect(page.locator(`#${id}`)).toBeVisible();
    }
  });

  test('stat cards populate from API (spinner disappears)', async ({ page }) => {
    // r6-spinner is the initial loading state; should be replaced once JS fires
    await expect(page.locator('#stat-status .r6-spinner')).not.toBeAttached({ timeout: 5000 });
  });

  test('Run Full Demo button is visible', async ({ page }) => {
    await expect(page.getByRole('button', { name: /Run Full Demo/ })).toBeVisible();
  });

  test('Agent Guardrail Sequence panel shows 6-step tracker', async ({ page }) => {
    await expect(page.locator('#demo-loop-panel')).toBeVisible();
    await expect(page.locator('#btn-demo-loop')).toContainText('Run 6-Step Guardrail Demo');
    // All 6 step labels are present (hidden until demo runs)
    await expect(page.locator('.demo-step[data-step="1"]')).toBeAttached();
    await expect(page.locator('.demo-step[data-step="6"]')).toBeAttached();
  });

  test('Patient Explorer panel buttons are visible', async ({ page }) => {
    await expect(page.locator('#patient-panel')).toBeVisible();
    await expect(page.locator('#btn-load-patient')).toBeVisible();
    await expect(page.locator('#btn-search-patients')).toBeVisible();
    await expect(page.locator('#btn-patient-count')).toBeVisible();
  });

  test('MCP Agent Tool Loop panel is visible', async ({ page }) => {
    await expect(page.locator('#tools-panel')).toBeVisible();
    await expect(page.locator('#tool-input')).toBeVisible();
    await expect(page.locator('#btn-exec-tool')).toBeVisible();
  });

  test('Context Envelope Builder panel is visible', async ({ page }) => {
    await expect(page.locator('#context-panel')).toBeVisible();
    await expect(page.locator('#btn-ingest')).toBeVisible();
    await expect(page.locator('#btn-get-context')).toBeVisible();
  });

  test('HIPAA De-identification panel is visible', async ({ page }) => {
    await expect(page.locator('#deid-panel')).toBeVisible();
    await expect(page.locator('#btn-deidentify')).toBeVisible();
    // Side-by-side output containers exist
    await expect(page.locator('#deid-raw')).toBeAttached();
    await expect(page.locator('#deid-safe')).toBeAttached();
  });

  test('Human-in-the-Loop panel is visible', async ({ page }) => {
    await expect(page.locator('#hitl-panel')).toBeVisible();
    await expect(page.locator('#btn-hitl-demo')).toContainText('Run HITL Demo');
  });

  test('OAuth 2.1 + PKCE panel is visible', async ({ page }) => {
    await expect(page.locator('#oauth-panel')).toBeVisible();
    await expect(page.locator('#btn-oauth-demo')).toContainText('Run OAuth Flow');
  });

  test('R6 Ballot Resources section is visible', async ({ page }) => {
    await expect(page.locator('#permission-panel')).toBeVisible();
    await expect(page.locator('#stats-panel')).toBeVisible();
    await expect(page.locator('#subscription-panel')).toBeVisible();
    await expect(page.locator('#r6resources-panel')).toBeVisible();
  });

  test('Validate panel has pre-filled JSON textarea', async ({ page }) => {
    await expect(page.locator('#validate-panel')).toBeVisible();
    const value = await page.locator('#validate-input').inputValue();
    expect(value).toContain('resourceType');
    expect(value).toContain('Observation');
  });

  test('Live Audit Feed is visible with export button', async ({ page }) => {
    await expect(page.locator('.audit-feed')).toBeVisible();
    await expect(page.locator('#btn-export-audit')).toBeVisible();
  });

  test('Security Posture panel shows all enforced controls', async ({ page }) => {
    // Scope to the Security Posture panel to avoid strict-mode conflicts with
    // duplicate text elsewhere on the page (e.g. "Tenant Isolation" in the hero)
    const panel = page.locator('.r6-panel').filter({ hasText: 'Security Posture' });
    const controls = [
      'Tenant Isolation',
      'HMAC Step-up Tokens',
      'PHI Redaction',
      'Human-in-the-Loop',
      'OAuth 2.1 + PKCE',
      'Audit Trail',
      'ETag Concurrency',
      'Medical Disclaimer',
    ];
    for (const control of controls) {
      await expect(panel.getByText(control, { exact: true })).toBeVisible();
    }
  });

  test('Discovery endpoint links are present', async ({ page }) => {
    await expect(page.locator('a[href="/r6/fhir/metadata"]').first()).toBeVisible();
    await expect(page.locator('a[href="/r6/fhir/.well-known/oauth-authorization-server"]')).toBeVisible();
    await expect(page.locator('a[href="/r6/fhir/.well-known/smart-configuration"]')).toBeVisible();
    await expect(page.locator('a[href="/privacy"]')).toBeVisible();
  });
});
