# Council ruling — 2026-09-02

**Status:** RULED. Binding on the build queue until Cohort 1 reports (two weeks) or the
owner overrides an item in writing.

**What this is.** The founder's standing instruction is to reach HealthClaw 2.0 with real
users and testers, and to resolve flagged decisions with a council of industry and
engineering experts rather than waiting on him. Seven seats ruled independently from the
same brief (CTO, Product, QA, Interop, Privacy, Clinical, GTM), each with confidence and
a stated dissent risk. This document records the consensus, the dissents and how each was
settled, the STOP list, and the short list of things only the owner can do.

**How to read it.** Each decision has a ruling, the seats behind it, and what changes in
the repo. Confidence is the lowest confidence any ruling seat gave. Where seats disagreed,
the dissent is recorded under the ruling with the reason the majority position won.

The measurements every seat read are in `docs/2026-08-16-hard-truths.md`, the ratchet
pins in `tests/test_ratchets.py`, and the live checks in `scripts/prod_watch.py`. QA
re-ran the pins and the prod watch on the day of the ruling (13/13 pins, 12/12 prod
checks, build `89b42fb`) and reproduced #433, #534, #535 and #538 live.

---

## 1. The decisions

### D1 — Adopt the delivery process, with nine amendments (unanimous, H)

`docs/2026-08-16-delivery-process.md` moves from PROPOSED to binding. The 2026-08-16
measurement stands: eight defects found by running the system, zero by the 3,153-test
suite. The four-artifact evidence pack (run log, recording produced by the asserting run,
edge-case register, two sign-offs) is the only mechanism on the table that forces a run
before "done".

Amendments, each from a seat's own finding:

1. **Defect lane.** An issue with a live reproduction on a running surface skips SOW and
   PRD: build → QA on the running system → PR titled "candidate". No evidence pack. The
   tell that the lane is being abused is a "candidate" PR with no set label and no repro.
2. **Architecture review answers within one business day** or the item proceeds through
   the defect lane with the risk written into the PR.
3. **A run log is a committed script plus a transcript that a non-author re-ran from
   `main`.** A scratch file is not a run log. (#530 rests on uncommitted walkthroughs.)
4. **Any test that reaches the network says so in its docstring and is marked.** #433's
   root cause is a module docstring that says "No network" over a test that POSTs to
   production.
5. **Each sign-off names a person and a date.**
6. **Synthetic data satisfies the QA gate's "real data" clause until the Cohort 2 gates
   close** (D3). The real-data sign-off becomes a Cohort 2 artifact.
7. **A Cohort 1 tester's report is the end-user sign-off for set 3.** A tester who
   returns is the evidence, not a form.
8. **Issues close on a run log against the deployed build; feature sets close on the
   four-artifact pack.** "Done" is per set, not per PR.
9. **Strike the stale "CareAgents redeploy" blocker** from the first three moves. It is
   done (prod watch 12/12 on build `89b42fb`). The Docker stack and `.env` are not.

### D2 — The minimal beta-ready slice, in order (core unanimous, M on the tail)

The stranger's path is sign-in → connect → ask → approve, on a phone. The first three
screens were broken on the day of the ruling, verified live:

| # | Issue | Why it is in the slice | State |
|---|---|---|---|
| 1 | #534 | Terms/Privacy links on the sign-in page point at the raw Railway host | PR #540 open |
| 2 | #538 + #535 (client copy) | Care-gaps page shows "0 Due" over a patient nobody evaluated; a mistyped tenant reads as "requires authentication" | PR #541 open |
| 3 | #536 (hide) | The Telegram tile is a dead end (no webhook, no poller since June) | queued |
| 4 | #537 + #433 | The monitor watches a hostname nobody visits and cannot see Telegram; the one flaky test is a determinism defect | queued |
| 5 | Beta banner + refreshed #184 copy | A tester who is not told it is a beta files bugs as complaints | queued, ships with 3 |

**Dissent: where the action rail goes.** The CTO seat placed #528 → #520 → #215 second,
because the approve leg is the one thing in the consumer journey nobody outside the team
can exercise. Product, QA, GTM and Clinical placed it after the slice; GTM and Product
ruled a two-week stop on new action-rail surfaces. **Settled:** land the one-line #528
payload-immutability guard now (all seats agree it must precede any approve surface, and
it is cheap), then #520 and #215 after Cohort 1's first week of reports, or sooner if a
named design partner needs the approve leg. Clinical writes stay disabled on beta tenants
until #215 exists.

**Dissent: #219 and #264.** QA placed both in the slice (thread starvation at 10–30
users; two origins break passkeys). CTO: #219's premise is superseded by the durable
worker, re-measure with a 10-concurrent probe rather than build; #264 appears settled by
the cutover, verify over DoH and close. GTM: neither bites at 10 testers. **Settled:**
re-measure #219 (a probe, not a build) and take the cheap half only if the probe fails
(thread count, read timeout). #264: careagents.cloud is the sole origin; the Railway
hostname 308s every path except `/healthz`; verify DNS over DoH. Both ride the CareAgents
batch, neither gates invitations.

### D3 — Cohort 1 now, Cohort 2 behind seven preconditions (Privacy H, Clinical M)

Cohort 1 (synthetic records only) can start as soon as #538 is deployed. Cohort 2 (own
real records) waits on:

- **P1** Owner records in writing: "operate as if the FTC Health Breach Notification
  Rule applies." Stop treating #168 as blocked on counsel; counsel refines the posture,
  the posture is adopted now.
- **P2** Privacy policy v2 + Terms reconciled with the product as built, and a
  `CONSENT_VERSION` bump so every account re-consents.
- **P3** Incident-response runbook: who decides, who is notified, within what window.
- **P4** Account deletion proven end to end on production, #217 closed, and a written
  backup-retention statement.
- **P5** The `$populate` read bound (D10) closed.
- **P6** Subprocessor terms on file (hosting, LLM, email).
- **P7** At most 15 named adults, own records only, no dependents.

**Dissent: when Cohort 1 starts.** Clinical: not before #538 and #436 are deployed, since
both make the product state the opposite of what the engine said. Privacy, GTM and
Product: now. **Settled:** #538 gates invitations (it is in PR and Flask auto-deploys on
merge). #436 is the first fast-follow, not a gate; the tester guide carries one sentence
saying a screening may read "could not check" while it lands.

**Interim posture, in code, before the batch deploy:**

- `CARE_REAL_RECORDS = off | allowlist | on`. `off` renders the Fasten, wearable and
  direct-FHIR tiles as "coming soon" and refuses new-connection POSTs with 503. It gates
  new connections only; existing connections and refresh are unaffected. Production runs
  `allowlist` with the one clinician already onboarded, who stays as-is and is not counted
  against P7.
- A beta banner on the landing page and home, with two sentences added to the privacy
  page for accuracy: what a tester's synthetic tenant contains, and that deletion is
  immediate.
- No Cohort 2 invitations until P1–P7 are checked off in this document.

### D4 — MCP canonical resource and issuer (unanimous, H)

- Canonical resource: `https://mcp.healthclaw.io/mcp`. Issuer: `https://app.healthclaw.io`.
- #522 (repoint the dangling `mcp.healthclaw.io` record from Vercel to Railway) precedes
  any phase 1 production deploy. Until DNS answers from Railway, a PRM whose `resource`
  disagrees with the host it is fetched from is rejected by a conformant client and the
  partner reads that as our bug.
- Phase 1 merges now behind `MCP_CANONICAL_RESOURCE`: unset means PRM 404 and bare-Bearer
  behaviour unchanged; set means serve PRM only when the request Host is canonical, and
  build `resource_metadata` from the constant, never from `req.hostname`.
- Phase 1 closes #523, not #290. #290 closes on the spec's §8.4 end-user run with a
  successor issue filed in the same PR ("a hosted connector reaches a tenant": phase 3
  plus the consent spec).
- Interop's spec amendments P1-a..d, P2-a..f and P3-a land as a docs PR against
  `docs/specs/2026-08-16-mcp-authorization.md` before phase 2 is built.

**Dissent: build now or stop for two weeks.** GTM ruled a two-week stop on MCP-auth
building. CTO and Interop: phase 1 is small and inert without the flag. **Settled:**
phase 1 builds in the parallel lane (whoever is not on the beta slice), merges behind the
flag, and does not deploy until DNS. Phase 2 waits.

### D5 — Step-up token whitespace (unanimous, H)

Strip uniformly. The kernel already does (`r6/access.py:352-368`) and six merged slices
depend on it, so the ruling is retroactive and there is no behaviour change. The strip is
Python's `str.strip()`, which is Unicode-wide, not ASCII-only: a token padded with U+00A0,
U+2003 or U+3000 is admitted. That was measured, not assumed (review of PR #545). It is the
right behaviour for an authority-neutral token, and the pin records what ships rather than
what an earlier draft of this ruling wrongly claimed. Close #334 with one pin test in `tests/test_access_kernel.py` (padded valid token
admitted; padded garbage refused), a one-line comment at the strip citing #334, and a
§2.5 note in `docs/2026-08-03-access-kernel-spec.md`. The tenant header stays unstripped:
it is an identifier compared against a stored value, not an authority-neutral token.

### D6 / D7 — Telegram tile and canonical origin (unanimous)

Hide the Telegram tile as "coming soon" for the beta; do not service the bot. The tester
guide's "on the web, or by text" becomes "on the web". careagents.cloud is the sole origin
(D2). Verify #264 over DoH before closing.

### D8 — Blank resource ids (unanimous, H)

Refuse a blank id as `invalid_id` (`r6/fasten/ingester.py:401-405`, already the case);
an absent id gets a UUID (already the case); an integer id keeps coercing to a string
(pre-existing, live on the Fasten connector; CTO's dissent-risk); any other non-string
id (bool, float, object, list) is `invalid_id`, since `str()` of a bool or float passes
the id pattern today. Pin all four. Fabricating an id for a blank one turns an upsert into
an append and makes every re-ingest a duplicate row: the #509 defect shape.

### D9 — Vital-signs without `effective[x]` (Interop + Clinical, H)

Propose accepts and returns a `warning` OperationOutcome naming `effective[x]`. When the
approve/execute gate exists (#215), it refuses `vital-signs` without it. At 2.0, the
refusal extends to `laboratory`. The warning text says so now.

### D10 — `$populate` reads unbounded PHI (Interop, Privacy, Clinical: H, high priority)

Three seats independently called this a live non-negotiable violation. Bound it:

- Allowlist the `%patient` projection in `r6/sdc/populate.py` `_resolve_answer` and
  `expressions.py` `build_context`: `name.given`, `name.family`, `birthDate`, `gender`,
  `telecom` (phone, email), `address` (line, city, state, postalCode). Nothing else
  resolves.
- Auto-loaded clinical content goes through `apply_redaction`, then terminology labels by
  code. `is_deleted` rows are filtered in `r6/sdc/routes.py`.
- The route exits through the access kernel with the INTAKE profile so it is counted.
- Negative test: `%patient.identifier`, `%patient.photo`, `%resources.code.text` produce
  no answer and an OperationOutcome issue naming the `linkId`.
- Pull `questionnaire_populate` from the model-facing read tier for the beta, unless the
  demo kit depends on it (verify before removing; if it stays, answers carry a "from your
  records" marker).
- Cut the delivery PDF link (`r6/sdc/delivery.py`) from 7 days to 24 hours.

### D11 — User-facing vs ratchet split (consensus, M)

Roughly 70/30. The ratchet share for the fortnight is exactly three chunks: A5 (raw tenant
reads 5 → 0), B2 (`r6/agent_runs`, the last unaudited mutator package), and F5 with
#509-2 folded in. Run them in the parallel lane. A2–A3, A6–A8, B3+, C, D, E wait until
Cohort 1 reports. A tester-reported fix always jumps the queue. Ratchet PRs merge one per
day during tester week, after the prod watch is green on the previous one.

### D13 — #433 and #509-2 (unanimous, H)

Both today, no timebox. #433: `monkeypatch.setattr(prod_watch, "post", ...)` in the
`_isolate` fixture (`tests/test_prod_watch_build.py`) returning 200 `{"result": {}}`, and
correct the "No network" docstring. #509-2: `existing.is_deleted = False` in
`r6/context_builder.py` where the upsert lands on a tombstone, matching what the Fasten
ingester already does.

### D14 — Clinical honesty (Clinical, H)

- #538 has four layers. Page and contract test are in PR #541. Route half is #542: when
  the subject is unresolved, `results = []`, `summary["evaluated"] = False`, audit
  `evaluated=0`, and the existing tests that expect seven indeterminate rows change.
- #436: per-rule "could not check" line for indeterminates that are `applicable`; flip the
  pin in `tests/test_caregaps_report.py`.
- #458: the honesty string in `r6/curatr.py` and the bot's "Checked 1 of N".
- Rule register: split `bp-screening` (18–39 every 36 months, 40+ annually); A1c note
  reads "no diabetes diagnosis found in your connected records".
- #226 option-3 stopgap. Clinical writes disabled on beta tenants until #215.

### D17 — Cohort 1 shape (GTM, H)

Ten testers on synthetic records: six on the web, three on the hosted-connector demo URL,
one on the FHIR facade, none on Telegram. Invite fifteen. The Friday number is **returned
testers** (`last_login_at` on a later day than `created_at`), not sign-ups.

---

## 2. STOP list

Each seat named one thing to stop. All seven are adopted for two weeks.

1. No new kernel or audit migration slices beyond A5, B2, F5. The pins hold the line.
2. No new surfaces. Fix the ones a stranger hits.
3. No measurements a non-author cannot reproduce from `main`. Every "N/N" carries the
   command that produced it.
4. No absolute URLs built from `request.host_url` or `req.hostname` (eight more sites in
   `r6/routes.py`). Canonical hosts come from config.
5. Stop treating #168 as blocked on counsel. Adopt the posture; let counsel refine it.
6. No closing an honesty issue when the fix ends at the JSON. The page, the summary flag,
   the route and the audit line all have to agree.
7. No building on the MCP-auth or action-rail specs beyond the one-line #528 guard and the
   flagged phase 1.

---

## 3. Build queue (in order; parallel lane in brackets)

1. PR #540 (#534) and PR #541 (#538/#535) — open, reviewed, need the owner to arm merge.
2. #536 hide + tester-guide copy + beta banner + `CARE_REAL_RECORDS` switch + privacy
   sentences + #264 308 redirect: one CareAgents PR, one batched deploy.
3. #537 prod-watch retarget (careagents.cloud, tile state, Telegram `getMe`) + #433 fix.
4. #528 one-line payload-immutability guard.
5. D10 `$populate` bound.
6. #542 route half + #436 + rule-register changes + #458 string.
7. Pins: #334 close, #286 blank/non-string id, #509-2.
   [Parallel: F5 soft-delete, B2 `agent_runs` audit, A5 raw reads; MCP phase 1 behind
   the flag; MCP spec amendments docs PR.]
8. #219 probe (10 concurrent). Build only if it fails.
9. After Cohort 1 week 1: #520, #215.

---

## 4. Owner-only

Nothing on this list can be done by an agent. Each item names what is READY so the
owner's step is one action.

| Action | READY |
|---|---|
| Arm auto-merge on PR #540 and PR #541 | Both reviewed; the permission classifier refuses agents `gh pr merge --auto` |
| One batched CareAgents `railway up` (web and worker from the same stage) | After item 2 merges: staged dir, post-deploy smoke, rollback commit |
| Send Cohort 1 invitations (15) | Tester guide, beta banner, the returned-testers metric; names by role: 6 web, 3 connector demo, 1 facade |
| Tell the physician design partner not to record the care-gaps page until #538 is deployed | One sentence |
| Send the rule register to the physician advisor for initials | Register text in D14 |
| Approve "clinical writes disabled on beta tenants until #215" | Config flag |
| Record the P1 posture ("operate as if HBNR applies") | One paragraph; unblocks #168 |
| Name the breach decision-maker; stand up privacy@ / security@ | P3 runbook draft |
| Subprocessor terms on file; Railway backup-retention setting | P6 list |
| #522: repoint `mcp.healthclaw.io` from Vercel to Railway; set `MCP_CANONICAL_RESOURCE` | CNAME target; `curl 'https://dns.google/resolve?name=mcp.healthclaw.io&type=A'` |
| Branch protection: one required review | PUT payload |
| Docker Desktop + `examples/aidbox-healthclaw-guardrails/.env` + Aidbox licence | Compose command; the spec that records set 1 |
| `MCP_AUTH_TOKEN` for one QA run of the authenticated `tools/list` | Command |
| Deferred, recorded so no one mistakes a merge for permission: `MCP_OAUTH_ENABLED=true`, plan purchases, the two drafted outreach emails | — |

---

## 5. What was not decided

- Whether the beta includes Population C (FHIR-facade and MCP testers) beyond the one
  facade tester. If it does, #290/#523 move ahead of #536 in the slice.
- The #219 build, pending the probe.
- Anything about pricing, the webinar list, or the second cohort's size beyond P7.
