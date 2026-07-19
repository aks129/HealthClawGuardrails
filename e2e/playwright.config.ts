import { defineConfig, devices } from '@playwright/test';

// Overridable so the suite can run where :5000 is taken (macOS AirPlay
// Receiver binds it by default). CI keeps the default.
const PORT = process.env.E2E_PORT || '5000';

export default defineConfig({
  testDir: './tests',
  // Serial execution — shared SQLite instance, tests create resources
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  retries: process.env.CI ? 2 : 0,
  // CI gets BOTH the inline annotations and an HTML report — the report is
  // what the failure-artifact upload ships, and with only the 'github'
  // reporter no report directory was ever produced (issue #154: every red
  // run ended with "No files were found ... e2e/playwright-report/").
  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never' }]]
    : 'list',

  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: 'on-first-retry',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    // The app factory no longer creates tables at boot (schema is managed by
    // explicit Alembic migrations). Initialize the DB, then serve — both
    // processes share one absolute SQLite path so the server sees the tables.
    command: 'cd .. && uv run flask --app main init-db && uv run python main.py',
    url: `http://localhost:${PORT}/r6/fhir/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      // Explicit testing env keeps the app off the fail-closed production path.
      APP_ENV: 'testing',
      STEP_UP_SECRET: 'e2e-test-secret-not-for-production',
      SQLALCHEMY_DATABASE_URI: 'sqlite:////tmp/e2e-healthclaw.db',
      FLASK_ENV: 'testing',
      PORT,
    },
  },
});
