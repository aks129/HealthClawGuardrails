import { defineConfig } from '@playwright/test';

// QA for the Aidbox example, and the recording of it — the same run.
//
// No `webServer`: this suite tests a stack that is ALREADY up
// (`docker compose up -d` + `./scripts/seed-aidbox.sh`), because what is
// under test is the composition, not the Flask app. A config that started
// its own server would pass with Aidbox absent, which is the one outcome
// this suite exists to rule out. It fails fast with a named reason instead.
//
// Video is on for every test rather than on failure: the artifact IS the
// deliverable here, and a recording that only exists when something breaks
// is not one you can send anybody.
export default defineConfig({
  testDir: '.',
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  reporter: [['list']],
  use: {
    // 720p, and the page is laid out for it. Anything narrower turns the
    // side-by-side comparison — the whole point of step 1 — into a stack.
    viewport: { width: 1280, height: 720 },
    video: { mode: 'on', size: { width: 1280, height: 720 } },
    trace: 'retain-on-failure',
  },
  outputDir: './artifacts',
});
