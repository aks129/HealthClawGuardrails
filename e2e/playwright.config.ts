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

// A THIRD app: the same CareAgents code with CARE_REAL_RECORDS=allowlist
// (#553, council ruling 2026-09-02 D3). The switch is read once, at
// create_app(), so one process can only ever be in one mode — proving in a
// browser that an allowlisted account sees different tiles from a stranger
// needs its own server, its own SQLite and its own log.
export const CARE_ALLOW_PORT = process.env.CARE_ALLOW_E2E_PORT || '5102';
export const CARE_ALLOW_BASE_URL = `http://localhost:${CARE_ALLOW_PORT}`;
export const CARE_ALLOW_LOG = '/tmp/e2e-careagents-allow.log';
const CARE_ALLOW_DB = '/tmp/e2e-careagents-allow.db';
// Fixed, and synthetic. The allowlist is server env, so the address the spec
// signs in with has to be known before the server boots — it cannot be the
// per-test unique address the other specs use. `.test` is reserved (RFC 2606)
// and can never be a real mailbox; nothing is ever sent anyway (the dev mail
// stub logs to stderr).
export const CARE_ALLOW_EMAIL = 'e2e-allowlisted@example.test';

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
        // The beta's real-record switch, pinned CLOSED for this server (#553,
        // D3). Blank resolves to `off` exactly as unset does; the *unset*
        // default is pinned by pytest (test_careagents.py::
        // test_real_records_switch_defaults_to_off_and_parses_the_allowlist),
        // and pinning it here stops a developer shell that exports
        // CARE_REAL_RECORDS=on from quietly running the browser suite against
        // the open path while its name still says closed.
        CARE_REAL_RECORDS: '',
        CARE_REAL_RECORDS_ALLOWLIST: '',
      },
    },
    {
      // CareAgents again, in allowlist mode — see CARE_ALLOW_* above.
      command:
        `rm -f ${CARE_ALLOW_DB} ${CARE_ALLOW_LOG} && cd .. && ` +
        `uv run flask --app careagents.wsgi run --port ${CARE_ALLOW_PORT} ` +
        `2>> ${CARE_ALLOW_LOG}`,
      url: `${CARE_ALLOW_BASE_URL}/`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        CARE_ENV: 'development',
        CARE_DATABASE_URL: `sqlite:///${CARE_ALLOW_DB}`,
        // Dead port, for the same reason as the entry above.
        HEALTHCLAW_BASE: 'http://127.0.0.1:9',
        CARE_ORIGIN: CARE_ALLOW_BASE_URL,
        CARE_RP_ID: 'localhost',
        RESEND_API_KEY: '',
        CARE_REAL_RECORDS: 'allowlist',
        CARE_REAL_RECORDS_ALLOWLIST: CARE_ALLOW_EMAIL,
        // Fasten key present and the wearables sidecar declared wired, so the
        // ONLY thing between an account and a live real-record tile here is
        // the allowlist. Without these two, `catalog()` would answer "soon"
        // for its own reasons ("not configured on this deployment") and the
        // allowlist test would pass while proving nothing. The key is never
        // used: nothing in the spec starts a Fasten flow.
        FASTEN_PUBLIC_KEY: 'e2e-not-a-real-fasten-key',
        CARE_WEARABLES_ENABLED: '1',
      },
    },
  ],
});
