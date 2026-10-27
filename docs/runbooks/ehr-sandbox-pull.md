# Runbook: pulling an Epic or Oracle sandbox patient

`scripts/ehr_sandbox_pull.py` reads one patient from the Epic or Oracle
(Cerner) public sandbox and seeds it into a sandbox tenant. It uses SMART
Backend Services: a `client_credentials` grant with an ES384 client
assertion. Use it for test data only. It never reaches a production EHR,
and pointing it at one needs a new design review.

## One-time setup

1. **Private key.** A P-384 key in PEM, mode 0600, on the machine that runs
   the script. The default path is `~/.healthclaw/ehr-sandbox-es384-1.pem`.
   Keep a copy in the owner's password manager. Never put the key in the
   repo or on the engine's environment.
2. **Public key.** `deploy/ehr-sandbox-jwks.json` holds the public half
   (kid `healthclaw-ehr-sandbox-es384-1`). The engine serves it at
   `https://app.healthclaw.io/.well-known/healthclaw-ehr-client-jwks.json`.
   Both vendor apps are registered against that URL, so do not move it.
3. **Client ids.** Put them in `~/.healthclaw/ehr-sandbox.json` (mode 0600):

   ```json
   {"epic_client_id": "...", "oracle_client_id": "..."}
   ```

   Or set `EPIC_SANDBOX_CLIENT_ID` / `ORACLE_SANDBOX_CLIENT_ID`. The
   environment wins over the file. The ids are not secret, but they stay out
   of this public repo.
4. **Seed secret.** Export `INTERNAL_TOKEN_MINT_SECRET` with the engine's
   internal secret. The script reads it only from the environment, never
   from the command line.

## Run

```sh
uv run python scripts/ehr_sandbox_pull.py --vendor epic \
    --patient <sandbox patient FHIR id> --tenant sandbox-epic-<name>
uv run python scripts/ehr_sandbox_pull.py --vendor oracle \
    --patient <sandbox patient FHIR id> --tenant sandbox-oracle-<name>
```

`--engine-url` defaults to `https://app.healthclaw.io`. For a local engine,
pass `--engine-url http://localhost:5099` after `uv run flask --app main
init-db`. The URL must be https; plain http is accepted only for
`localhost` and `127.0.0.1`, because the seed secret goes with the request.

`--dry-run` pulls and writes the export but does not seed. It needs no
`INTERNAL_TOKEN_MINT_SECRET`; it still checks `--engine-url`.

The tenant must match `^sandbox-(epic|oracle)-[a-z0-9-]+$`. The script
refuses anything else before it sends a request.

The script prints counts by type (kept and dropped), the export file name
and the number of resources the engine created (`created unknown` if the
engine's answer has no count). A dropped type that is not a plain
letters-only name is counted as `other`, never printed. It never prints resource
content, the vendor bearer token or the seed response. It keeps no token on
disk. The transaction Bundle goes to `exports/` (gitignored). Never commit a
pulled bundle: the vendor terms have not been checked, and this repo is
public.

## What it reads

The Patient, then these searches with `patient=<id>`: AllergyIntolerance,
Condition (`category=problem-list-item`), MedicationRequest
(`_include=MedicationRequest:medication`), Immunization, Coverage
(`_include=Coverage:payor`) and Observation (`laboratory` and
`vital-signs`). Only Patient, AllergyIntolerance, Condition,
MedicationRequest, Medication, Immunization, Coverage, Organization and
Observation are kept. The seed path does not filter types, so this list is
the only filter.

Epic gets no `scope`. Oracle gets SMART v1 `system/<Type>.read` scopes for
the eight types its app is registered for.

## What it changes in each record

- Adds the HL7 v3 ActReason `HTEST` coding to `meta.tag`, with system and
  code only and no display. Every kept entry gets it, `_include`d ones too.
  The marker lets a KTC link carry these records without
  `KTC_LINK_REAL_RECORDS`. That is honest only because the script can reach
  nothing but the sandbox URLs.
- Strips `meta.source` and any record-source tag in our own system.

Nothing else changes. Records are stored as received; the engine redacts
`display` and `text` on the way out.

## Guardrails in the code

- The token and FHIR URLs are exact strings, never read from the
  environment. `authorization.cerner.com` also serves production tenants.
  Production differs from the code sandbox by one token (`fhir-ehr` vs
  `fhir-ehr-code`).
- The HTTP session ignores proxy and CA-bundle environment variables. No
  request follows a redirect. A paging `next` link is followed only under the
  same https FHIR base, and never with a `.` or `..` segment or a
  percent-encoded dot or slash. Each search stops at 20 pages or 2000
  entries. A search cut off by either cap prints
  `<vendor>: <Type> truncated at <N>`; the pull still seeds and exits 0, so
  read the notice before you treat the tenant as complete.
- FHIR reads wait up to 120 s. Oracle's problem-list search has taken more
  than 30 s for one sandbox patient.
- The assertion header has `alg`, `typ` and `kid` only (no `jku`). The
  claims are `iss` = `sub` = client id, `aud` = the exact token URL, a
  unique `jti` and `exp` five minutes out.

## Rotating the key

1. Generate `-2`. Add its public JWK next to `-1` in
   `deploy/ehr-sandbox-jwks.json` through a PR, and deploy it.
2. Sign with `-2` and get a token from both vendors. The script's `KID`
   constant names `-1` today, so this step changes it. Test the vendor's
   JWKS cache lag; do not assume it.
3. Remove `-1` through a PR.

If a key is compromised, remove its kid at once and generate a new key.
Never reuse this key or these client ids for production.

## Troubleshooting

- `token: HTTP 400` or `401` from Epic just after registration: the sandbox
  sync lags. Retry later before you debug.
- `GET: HTTP 403` from a search: the app is not registered for that type.
- `seed: HTTP 403`: `INTERNAL_TOKEN_MINT_SECRET` is wrong.
- `refused a URL outside the sandbox FHIR base` or `refused a next link off
  the FHIR base`: the vendor sent a link the script does not trust. Do not
  widen the check. Report it.
