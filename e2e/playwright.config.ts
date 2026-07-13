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
    // The app factory no longer creates tables at boot (schema is managed by
    // explicit Alembic migrations). Initialize the DB, then serve — both
    // processes share one absolute SQLite path so the server sees the tables.
    command: 'cd .. && uv run flask --app main init-db && uv run python main.py',
    url: 'http://localhost:5000/r6/fhir/health',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      // Explicit testing env keeps the app off the fail-closed production path.
      APP_ENV: 'testing',
      STEP_UP_SECRET: 'e2e-test-secret-not-for-production',
      SQLALCHEMY_DATABASE_URI: 'sqlite:////tmp/e2e-healthclaw.db',
      FLASK_ENV: 'testing',
    },
  },
});
