// Video-recording config for the demo-tenant walkthrough. Separate from
// playwright.config.ts so the normal e2e suite keeps its own webServer and
// stays fast — this one records, runs one spec, and talks to a server that
// is already up and seeded.
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: 'demo-tenant-walkthrough.spec.ts',
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  reporter: 'list',
  use: {
    baseURL: process.env.DEMO_BASE_URL || 'http://localhost:5099',
    viewport: { width: 1280, height: 800 },
    video: { mode: 'on', size: { width: 1280, height: 800 } },
    trace: 'off',
  },
  outputDir: 'demo-artifacts',
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
