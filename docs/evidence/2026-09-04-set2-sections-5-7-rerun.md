# Set-2 pack §5, §6 and §7 — re-run by someone other than their author

**Date:** 2026-09-04 · **Repo at:** `89b42fb` · **Issue:** #602, item 1 ·
**Continues:** #530 and the §3/§4 re-run in #601

Sections 5, 6 and 7 of `docs/evidence/2026-08-16-set2-connectors.md` rested on
scripts that lived only in an uncommitted scratch directory. The directory is
gone. Three scripts have been rewritten from the pack's own transcripts and
committed; one section turned out never to have had a script at all.

## The finding

**Every measurement §5 and §6 recorded reproduces exactly**, against the tree
the pack measured. Run against `2b7872d`, the new scripts print §5's three
cases and §6's two rows with the same values the pack records — including R1's
four lines value for value, down to `checks.upstream` reading `not_configured`
and `r6_resources Patient rows = 1`. Not character for character: this script
also prints the HTTP status of the write and the read, which the pack's
transcript does not show.

At `89b42fb` three of those measurements differ. The same script against both
trees is what says each difference is a fix rather than drift:

| what changed | fixed by |
|---|---|
| `hapi` sends the credential it used to drop (R5) | #512 |
| a half-configured upstream reports degraded, not healthy (R1) | #513 |
| an unknown kind refuses to boot instead of 500-ing per request (R6) | #518 |

**§7 is the different one.** It named no script — it says only "resolved today
via the registry HTTP API" — so there was no transcript to turn back into one,
and one was written from the section's own table instead. It answers the
question §7 said could not be answered. §7 recorded its four digests as "the
baseline for the next run"; this is that run, and both version-pinned images
resolve to the same digest they did on 2026-08-16 while **both unpinned tags
have moved**.

## What each section needed, and whether it was reachable

Determined before anything was run, because a section that cannot be reached
here is worth more marked than approximated.

| Section | Missing script | What a re-run needs | Reachable here |
|---|---|---|---|
| §5 cases 1–2 | `registry-contract.sh` | the app booted locally; one read from `server.fire.ly` | yes |
| §5 case 3 (R1) | `halfconfig.sh` | the app booted locally, nothing else | yes |
| §6 | `auth_probe.py` | the app booted locally, against a loopback recorder | yes |
| §7 | **none named** | anonymous manifest reads from `ghcr.io` and Docker Hub | yes |
| §2 `medplum` | `medplum-qa.sh` | a running Medplum and client credentials | **no — not attempted** |

**The four-script framing in #602 and #601 is off by two.** Both say sections
5, 6 and 7 rest on four uncommitted scripts. In fact: `registry-contract.sh`
and `halfconfig.sh` are §5 (case 3 being the one the pack files as register
entry R1), `auth_probe.py` is §6, §7 had no script, and `medplum-qa.sh` is
**§2**, not §5–7. §2 is out of scope here and stays unverified: it needs a
running Medplum and a client credential, and this machine has neither.

## Method

Each script was written from the pack's transcript, not recovered — which is
itself the test of whether the transcript describes something reproducible.

Then each was run twice:

- against `89b42fb`, the current tree;
- against `2b7872d`, the tree the pack says its live runs used, exported with
  `git archive` and booted with **this** checkout's interpreter.

`git diff --stat 2b7872d 89b42fb -- uv.lock pyproject.toml` is empty, so the
dependency set is identical across the two and the only variable between the
runs is source code. That is measured, not assumed.

Every guard in the three scripts was checked by mutation: one line changed in
a copy, and the guard that is supposed to catch it observed going red. The
mutants are reproducible — each transcript carries the `sed` that made it and
a diff of the line it changed.

## §5 — the registry contract, asserted against a booted app

`scripts/connector-registry-contract.py`. Transcripts:
`docs/evidence/2026-09-04-set2-rerun/registry-contract-run.txt` and
`…-at-2b7872d-run.txt`.

One script where the pack had two. `registry-contract.sh` and `halfconfig.sh`
boot the same app and ask the same endpoint; two near-copies of one boot
sequence is how the Aidbox walkthrough's status codes drifted from its own
README (#499).

| Case | §5 as written, 2026-08-16 | at `2b7872d` today | at `89b42fb` today |
|---|---|---|---|
| 1 — `FHIR_UPSTREAM_URL` beats `MEDPLUM_BASE_URL` | resolved to `server.fire.ly/R4`, kind `generic`, software `Firely Server` | **same** | **same** |
| 2 — unknown kind | app boots; `/r6/fhir/health` → 500; `ValueError: … is not one of …` | **same** (message in the app's log) | **differs** — the process exits 1 before binding a port, with the same message |
| 3 — half-configured, health (R1) | HTTP 200, `status healthy`, `mode upstream`, `checks.upstream not_configured` | **same** | **differs** — HTTP 503, `status degraded`, `mode local`, `checks.upstream misconfigured` |
| 3 — half-configured, write (R1) | create returns an id, `_source` is `None`, 1 local `Patient` row | **same** | **same** |

Case 1 asserts on `software`, which is the Firely server naming itself in its
own CapabilityStatement — the upstream confirming which server was reached
rather than our configuration restating itself. It reported `Firely Server`
and `fhir_version 4.0.1` on both trees today.

Case 2's assertion is the registry's own message, not "the app did not start".
A port collision also stops the app starting, and would otherwise read as this
guard holding.

**R1's second half is unchanged and correct.** #513 fixed the *reporting*: a
named upstream that could not be built is now visible to a container
healthcheck and to any orchestrator probe. Writes still land in the proxy's own
SQLite, which is the right fallback — the defect was never where the data went,
it was that the health page said everything was fine.

The pack cites `is_proxy_enabled()` at `r6/routes.py:1980`. It now lives at
`r6/fhir_proxy.py:775`, split against `upstream_intended()`.

## §6 — what the proxy actually sends, per kind

`scripts/connector-auth-probe.py`. Transcripts:
`docs/evidence/2026-09-04-set2-rerun/auth-probe-run.txt` and
`…-at-2b7872d-run.txt`.

Identical credentials in both cases; only the kind changes. That is the
comparison §6 makes, and this run keeps to it.

| kind | §6 as written, 2026-08-16 | at `2b7872d` today | at `89b42fb` today |
|---|---|---|---|
| `hapi` | `Authorization: None` (no credential sent) | **same** | **differs** — `Basic` |
| `generic` | `Authorization: Basic` | **same** | **same** |

So R5 — the highest-severity finding of that pass — is real, was real on the
tree the pack measured, and is closed on the wire at HEAD by #512.

**One reconstruction decision.** §6's transcript does not say which request the
proxy was made to send. This script sends the health check, because
`/r6/fhir/health` reaches the upstream through `FHIRUpstreamProxy.healthy()` on
the same client that carries `basic_auth`, needs no step-up token, and creates
nothing. A different choice of request would exercise the same `basic_auth`
property; recording the choice rather than leaving it implicit is the point.

The recorder decodes the Basic header and asserts it carries the configured
client id. Without that, any `Basic` at all would satisfy the `hapi` row.

## §7 — image pins

`scripts/image-pin-digests.sh`. Transcript:
`docs/evidence/2026-09-04-set2-rerun/image-pins-run.txt`.

The script reads the image refs out of
`examples/aidbox-healthclaw-guardrails/docker-compose.yaml` rather than
hard-coding them, so a change to the compose file changes what is measured; the
baseline digests are hard-coded, because they are a record of one day and must
not follow anything.

Digests are written out in full. An abbreviated one is a place for a
transcription error to hide, in a document whose subject is measurements being
restated wrongly.

`ghcr.io/aks129/healthclaw-guardrails:1.10.0` — a version tag. **Not
re-pushed:** identical to 2026-08-16.

    sha256:57b345e0c8f6a6bf88690084f57fe863bd02882ade0e2c7b70002baa4c0e225b

`ghcr.io/aks129/healthclaw-mcp-server:1.10.0` — a version tag. **Not
re-pushed:** identical to 2026-08-16.

    sha256:d37c997ea15c73c715cdc2b90ea01aa6a49dc34cb6897913ce1691d8d012cd53

`healthsamurai/aidboxone:edge` — not pinned; a moving tag with
`pull_policy: always`. **Moved.**

    2026-08-16  sha256:42e4e8e10d9d42b54bf3f4602b3f584e06acc70738cdcf280ce7634bdb5e58b3
    today       sha256:90c4e72811765b6c420387d79f6033069fd0b045281e7f491be7b8cf6b3c3952

`postgres:18` — floating at the patch level. **Moved.**

    2026-08-16  sha256:06cad38a5d9f5d24b4d83d86def30795d5e4b757fedbf5281172b576dedcd941
    today       sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280

A manifest digest depends on the `Accept` header the request sends — a
multi-arch index and a single-platform manifest have different digests — so the
script prints the header it used, and the two identical rows confirm the header
matches what 2026-08-16 must have sent.

**R9 is no longer a prediction.** The compose file's comment explains why the
guardrails image is pinned, in the same file that runs Aidbox off a moving tag
with `pull_policy: always`. Nineteen days later that tag points somewhere else,
and `postgres:18` does too. Nothing was pulled, so this says the tags moved and
nothing about what is inside them.

**One consequence worth a decision, not fixed here.** The floating-tag
exemption in `tests/test_aidbox_example_tells_the_truth.py` justifies itself
with "the example is verified end to end against `edge` as of 2026-08-16, and
against nothing else". `edge` no longer resolves to the image that run used, so
the exemption's own reason has expired. The test was not edited; this is for
whoever owns that call.

§7's two supporting observations were re-read rather than restated. Both ghcr
repositories publish the same tag list as 2026-08-16, `latest` still resolves to
the same digest as `1.10.0` for both, and `1.10.0` still has no `1.10` alias.

## What this run does NOT cover

- **`aidbox` and `medplum`, in any respect.** Not attempted. `medplum-qa.sh`
  is §2's script, needs a running Medplum and a client credential, and neither
  exists here. §2 stays as 2026-08-16 left it.
- **A connector against a server that requires a credential.** #602's item 2.
  Untouched. §6 shows the header leaving the proxy against a recorder on
  loopback; a recorder that demanded the credential would still not be the
  upstream-holds-a-credential property, so none was built. Every walkthrough
  performed to date still runs against anonymous sandboxes.
- **The MCP step, the pinned images running, and the `qa/demo.spec.ts`
  recording.** No container was started. §7 read manifests; it pulled nothing,
  so nothing here says the 1.10.0 images behave as the source does. The
  2026-08-16 pack stays **EVIDENCE PARTIAL**.
- **Whether §5 case 1 exercises anything about `medplum` beyond precedence.**
  It sets `MEDPLUM_CLIENT_ID`/`_SECRET` to `must-not-be-used` and asserts they
  were not used. It says nothing about the Medplum connector working.
- **Any version string this run did not read.** The pack's HAPI and Firely
  build numbers, the app's own version: not measured here, so not repeated
  here.

## Footprint

Reads only, and only three hosts were contacted:

- `server.fire.ly/R4/metadata` — one read per §5 case-1 boot, across both
  runs, the mutation checks, and the iterations while the script was being
  written. No writes, no search, nothing created.
- `hapi.fhir.org/oauth2/token` — one 404 from mutation M1, which drops
  `FHIR_UPSTREAM_URL` so `MEDPLUM_BASE_URL` builds an OAuth2 client and points
  it there. `hapi.fhir.org/baseR4` itself was never asked for anything.
- `ghcr.io` and `registry-1.docker.io` — manifest and tag-list reads, with an
  anonymous pull token. **No image was pulled.**

**Nothing was created, changed or deleted on any public server, and no
container, service or deployment was started, stopped or rebuilt.** §5 case 3's
synthetic Patient landed in a SQLite file in a temporary directory, deleted
when the script exited. The Flask processes were ephemeral, bound to loopback
on unused ports, and stopped at the end of each case. The `2b7872d` export and
the mutants live in a temporary directory outside the checkout — several tests
here walk every `*.py` under the repository root, and a second copy of
`r6/access.py` inside it turns `test_the_checked_flag_is_set_in_exactly_one_place`
red.
