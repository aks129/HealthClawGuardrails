// Manual QA harness — `node tests/tools/prod_watch_alarm_sim.js` from the repo
// root. Not run by CI (this repo's gate is pytest + ruff); it exists because
// the alarm logic in .github/workflows/prod-watch.yml is JavaScript that
// otherwise only ever executes in production, against real issues.
//
// It extracts the REAL github-script body from the workflow — no paraphrase —
// and drives it against a fake octokit and a fake filesystem across every
// status.json state: absent, empty, truncated, schema-drifted, and correct,
// crossed with every exit code. Each row prints what happens to each of the
// two alarms, so "does a stale build actually raise the alarm, and does a
// closed alarm mean somebody observed a pass?" is answered by running it.
const realFs = require('fs');
const path = require('path');
const os = require('os');

// --- extract the script block verbatim from the workflow ---------------------
const yml = realFs.readFileSync('.github/workflows/prod-watch.yml', 'utf8');
const lines = yml.split('\n');
const start = lines.findIndex(l => l.trim() === 'script: |');
if (start < 0) throw new Error('script block not found');
const body = [];
for (let i = start + 1; i < lines.length; i++) {
  const l = lines[i];
  if (l.trim() === '') { body.push(''); continue; }
  const m = l.match(/^ {12}(.*)$/);
  if (!m) break;
  body.push(m[1]);
}
const SCRIPT = body.join('\n');

// --- fakes -------------------------------------------------------------------
function makeEnv({ exit, statusJson, resultTxt = 'RESULT', openIssues = [] }) {
  const log = [];
  const files = { 'result.txt': resultTxt };
  if (statusJson !== undefined) files['status.json'] = statusJson;
  const fs = {
    readFileSync(p, enc) {
      const key = path.basename(p);
      if (!(key in files)) { const e = new Error('ENOENT ' + key); e.code = 'ENOENT'; throw e; }
      return files[key];
    },
  };
  const issues = openIssues.map((t, i) => ({ number: 100 + i, title: t }));
  const github = {
    rest: {
      search: {
        issuesAndPullRequests: async ({ q }) => ({ data: { items: issues.slice() } }),
      },
      issues: {
        create: async ({ title }) => { log.push(['create', title]); },
        createComment: async ({ issue_number, body }) => {
          log.push(['comment', issue_number, body.split('\n')[0].slice(0, 40)]);
        },
        update: async ({ issue_number, state }) => { log.push(['update', issue_number, state]); },
      },
    },
  };
  const context = {
    serverUrl: 'https://github.com', repo: { owner: 'o', repo: 'r' }, runId: 1,
  };
  return { log, fs, github, context, exit };
}

async function runScript(env) {
  const require_ = (m) => (m === 'fs' ? env.fs : require(m));
  process.env.EXIT = env.exit === undefined ? '' : String(env.exit);
  const fn = new Function('require', 'github', 'context', 'process',
    `return (async () => { ${SCRIPT} })();`);
  await fn(require_, env.github, env.context, process);
  return env.log;
}

const OUTAGE = 'prod-watch: production checks failing';
const STALE = 'prod-watch: deployed build is stale';

function statusOf({ ok, asserted, buildOk }) {
  return JSON.stringify({ ok, checks: [], build: { deployed: 'x', asserted, ok: buildOk } });
}

const cases = [
  // name, exit, status.json content, pre-open issues
  ['healthy, nothing pinned (informational)', '0', statusOf({ ok: true, asserted: false, buildOk: null }), [OUTAGE, STALE]],
  ['healthy, build current', '0', statusOf({ ok: true, asserted: true, buildOk: true }), [OUTAGE, STALE]],
  ['healthy, build STALE', '2', statusOf({ ok: false, asserted: true, buildOk: false }), []],
  ['healthy, build STALE, stale issue already open', '2', statusOf({ ok: false, asserted: true, buildOk: false }), [STALE]],
  ['OUTAGE + build stale (the F1 scenario)', '1', statusOf({ ok: false, asserted: true, buildOk: false }), [STALE]],
  ['OUTAGE, healthz unreachable so build unasserted', '1', statusOf({ ok: false, asserted: false, buildOk: null }), [STALE]],
  ['--- RECOVERY: does a live outage issue ever close again? ---'],
  ['recovered, build current, outage issue open', '0', statusOf({ ok: true, asserted: true, buildOk: true }), [OUTAGE]],
  ['recovered, nothing pinned, outage issue open', '0', statusOf({ ok: true, asserted: false, buildOk: null }), [OUTAGE]],
  ['recovered but build STALE, outage issue open', '2', statusOf({ ok: false, asserted: true, buildOk: false }), [OUTAGE]],
  ['recovered but build stale, both issues open', '2', statusOf({ ok: false, asserted: true, buildOk: false }), [OUTAGE, STALE]],
  ['--- status.json MISSING ---'],
  ['missing file, exit 0', '0', undefined, [OUTAGE, STALE]],
  ['missing file, exit 1 (non-hex origin/main guard path)', '1', undefined, [OUTAGE, STALE]],
  ['missing file, exit 2', '2', undefined, [OUTAGE, STALE]],
  ['--- status.json CORRUPT ---'],
  ['empty file, exit 2 (stale detected!)', '2', '', [OUTAGE, STALE]],
  ['truncated json, exit 2 (stale detected!)', '2', '{"ok": false, "build": {"asse', [OUTAGE, STALE]],
  ['valid json, no build key, exit 2', '2', JSON.stringify({ ok: false, checks: [] }), [OUTAGE, STALE]],
  ['valid json, build key renamed asserted->assert', '2', JSON.stringify({ ok: false, build: { assert: true, ok: false } }), [OUTAGE, STALE]],
  ['valid json, asserted is string "true"', '2', JSON.stringify({ ok: false, build: { asserted: 'true', ok: false } }), [OUTAGE, STALE]],
  ['json is null literal', '2', 'null', [OUTAGE, STALE]],
  ['--- ODD EXIT CODES (script never ran / was killed) ---'],
  ['exit 127 (uv/python not found), no status.json', '127', undefined, [OUTAGE, STALE]],
  ['exit 137 (OOM-killed mid-run), no status.json', '137', undefined, [OUTAGE, STALE]],
  ['exit 2 from uv itself (usage error), no status.json', '2', undefined, [OUTAGE, STALE]],
  ['EXIT unset/empty, no status.json', '', undefined, [OUTAGE, STALE]],
];

(async () => {
  for (const c of cases) {
    if (c.length === 1) { console.log('\n' + c[0]); continue; }
    const [name, exit, statusJson, open] = c;
    const env = makeEnv({ exit, statusJson, openIssues: open });
    let log;
    try { log = await runScript(env); }
    catch (e) { console.log(`  ${name}\n      THREW: ${e.message}`); continue; }
    const outage = log.filter(l => l[0] === 'create' && l[1] === OUTAGE).length
      || log.filter(l => l[0] === 'comment' && l[1] === 100 + open.indexOf(OUTAGE)).length;
    const summarize = (title) => {
      const n = 100 + open.indexOf(title);
      const created = log.some(l => l[0] === 'create' && l[1] === title);
      const commented = log.some(l => l[0] === 'comment' && l[1] === n);
      const closed = log.some(l => l[0] === 'update' && l[1] === n && l[2] === 'closed');
      if (closed) return 'CLOSED';
      if (created) return 'FIRED(new issue)';
      if (commented) return 'FIRED(comment)';
      return 'untouched';
    };
    console.log(`  ${name.padEnd(52)} exit=${String(exit).padEnd(4)} outage=${summarize(OUTAGE).padEnd(16)} stale=${summarize(STALE)}`);
  }
})();
