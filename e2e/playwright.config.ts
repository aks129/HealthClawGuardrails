import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  // Serial execution — shared SQLite instance, tests create resources
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://localhost:5000',
    trace: 'on-first-retry',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    command: 'uv run python ../main.py',
    url: 'http://localhost:5000/r6/fhir/health',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      STEP_UP_SECRET: 'e2e-test-secret-not-for-production',
      FLASK_ENV: 'testing',
    },
  },
});
