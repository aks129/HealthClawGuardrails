import { test, expect } from '@playwright/test';

test.describe('FHIR API smoke tests', () => {
  test('health check returns 200 with status field', async ({ request }) => {
    const response = await request.get('/r6/fhir/health');
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    expect(body).toHaveProperty('status');
  });

  test('metadata returns R6 CapabilityStatement', async ({ request }) => {
    const response = await request.get('/r6/fhir/metadata');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.resourceType).toBe('CapabilityStatement');
    expect(body.fhirVersion).toBe('6.0.0-ballot3');
  });

  test('OAuth discovery returns PKCE S256 support', async ({ request }) => {
    const response = await request.get('/r6/fhir/.well-known/oauth-authorization-server');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty('authorization_endpoint');
    expect(body.code_challenge_methods_supported).toContain('S256');
  });

  test('SMART configuration returns token and auth endpoints', async ({ request }) => {
    const response = await request.get('/r6/fhir/.well-known/smart-configuration');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty('authorization_endpoint');
    expect(body).toHaveProperty('token_endpoint');
  });

  test('privacy policy returns compliance fields', async ({ request }) => {
    const response = await request.get('/r6/fhir/docs/privacy-policy', {
      headers: { 'X-Tenant-Id': 'e2e-test' },
    });
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty('medical_disclaimer');
    expect(body).toHaveProperty('data_protection');
  });

  test('Patient $validate returns OperationOutcome', async ({ request }) => {
    const response = await request.post('/r6/fhir/Patient/$validate', {
      headers: {
        'Content-Type': 'application/fhir+json',
        'X-Tenant-Id': 'e2e-test',
      },
      data: { resourceType: 'Patient', name: [{ family: 'Test' }] },
    });
    // 200 = valid, 422 = validation errors — both return OperationOutcome
    expect([200, 422]).toContain(response.status());
    const body = await response.json();
    expect(body.resourceType).toBe('OperationOutcome');
  });

  test('Patient search without tenant returns 400', async ({ request }) => {
    // Tenant isolation: every query requires X-Tenant-Id
    const response = await request.get('/r6/fhir/Patient');
    expect(response.status()).toBe(400);
  });

  test('Patient search with tenant returns Bundle', async ({ request }) => {
    const response = await request.get('/r6/fhir/Patient', {
      headers: { 'X-Tenant-Id': 'e2e-test' },
    });
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.resourceType).toBe('Bundle');
  });

  test('write without step-up token is rejected', async ({ request }) => {
    const response = await request.post('/r6/fhir/Patient', {
      headers: {
        'Content-Type': 'application/fhir+json',
        'X-Tenant-Id': 'e2e-test',
      },
      data: { resourceType: 'Patient', name: [{ family: 'Test' }] },
    });
    // 401 or 403 depending on auth scheme; either blocks the write
    expect([401, 403]).toContain(response.status());
  });
});
