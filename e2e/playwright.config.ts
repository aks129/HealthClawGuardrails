import { defineConfig, devices } from '@playwright/test';

// Overridable so the suite can run where :5000 is taken (macOS AirPlay
// Receiver binds it by default). CI keeps the default.
const PORT = process.env.E2E_PORT || '5000';

// CareAgents — a second, separate Flask app under test (issue #233). Own
// port + own SQLite; the spec (tests/careagents.spec.ts) overrides baseURL
// with CARE_BASE_URL. Its dev-mode mail stub (careagents/mail.py) logs each
// sign-in code to stderr instead of sending email, so the server's stderr is
// captured to CARE_LOG and the spec reads the code back from there — the
// whole auth journey runs with no email provider, no LLM key and no
// HealthClaw.
export const CARE_PORT = process.env.CARE_E2E_PORT || '5101';
export const CARE_BASE_URL = `http://localhost:${CARE_PORT}`;
export const CARE_LOG = '/tmp/e2e-careagents.log';
const CARE_DB = '/tmp/e2e-careagents.db';

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

  webServer: [
    {
      // The app factory no longer creates tables at boot (schema is managed by
      // explicit Alembic migrations). Initialize the DB, then serve — both
      // processes share one absolute SQLite path so the server sees the tables.
      // seed-demo-history loads the multi-year BP data the demo-tenant
      // walkthrough asserts. Without it that spec fails on a missing
      // Condition, which reads as a broken guardrail rather than an
      // unseeded fixture — the spec ran green locally against a hand-
      // seeded server and red in CI for exactly that reason.
      command: 'cd .. && uv run flask --app main init-db && uv run flask --app main seed-demo-history && uv run python main.py',
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
    {
      // CareAgents (issue #233). No init step: careagents.models.make_engine
      // runs create_all() at boot. Fresh DB + log each boot; stderr goes to
      // CARE_LOG so the spec can read the dev-mode sign-in codes.
      command:
        `rm -f ${CARE_DB} ${CARE_LOG} && cd .. && ` +
        `uv run flask --app careagents.wsgi run --port ${CARE_PORT} 2>> ${CARE_LOG}`,
      // Liveness, NOT /healthz. `/healthz` is readiness, and since the durable
      // agent-run control plane landed it also requires a live worker, which
      // it learns by asking HealthClaw — which this harness deliberately pins
      // to a dead port two entries below. So /healthz correctly answers 503
      // here forever, Playwright waited the full 60s for a 200 that could
      // never come, and every e2e run failed at boot with "Timed out waiting
      // 60000ms from config.webServer" — no spec ever executed. The landing
      // page is the honest "the server is up" signal for a boot gate.
      url: `${CARE_BASE_URL}/`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        // Development mode: fail-closed production checks off, dev session
        // secret, mail stub active. Mirrors tests/test_careagents.py.
        CARE_ENV: 'development',
        CARE_DATABASE_URL: `sqlite:///${CARE_DB}`,
        // A dead local port: the browser journey under test never touches
        // HealthClaw, and anything that accidentally tries must fail fast
        // rather than reach the real records service.
        HEALTHCLAW_BASE: 'http://127.0.0.1:9',
        CARE_ORIGIN: CARE_BASE_URL,
        CARE_RP_ID: 'localhost',
        // Explicitly blank so a developer shell with a real Resend key still
        // gets the stderr mail stub — codes must never leave the machine.
        RESEND_API_KEY: '',
      },
    },
  ],
});
