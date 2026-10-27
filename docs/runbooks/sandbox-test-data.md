# Sandbox test data and synthetic SMART Health Links

Two scripts give HealthClaw and CareAgents testing records in the shape a
real EHR serves, and a way to make a SMART Health Link without the engine's
link store:

- `scripts/sandbox_test_patient.py` pulls one test patient from a public,
  unauthenticated vendor FHIR sandbox and writes it as a FHIR R4
  `collection` Bundle, optionally ingesting it into a local engine.
- `scripts/ktc_shl_link.py` encrypts a test bundle and stores it on the CMS
  Kill the Clipboard test harness, then prints a `shlink:/`.

Both work only with test data. The puller tags every resource HTEST, and the
link script refuses any bundle that has an entry without that tag.

## Pull a sandbox patient

```bash
uv run python scripts/sandbox_test_patient.py --source oracle --out oracle.json
uv run python scripts/sandbox_test_patient.py --source smart --labs --out smart.json
```

| Source | Server | Default patient |
| --- | --- | --- |
| `oracle` | Oracle Health open sandbox, `https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d` (the tenant id comes from Oracle's R4 overview) | `12742400` |
| `smart` | SMART Health IT open R4 sandbox, `https://r4.smarthealthit.org` (Synthea) | `bb7fb8e1-1a76-4328-92ea-3852f34846e6` |

Use `--patient <id>` to pull a different patient.

The bundle holds the check-in types: Patient, Condition, AllergyIntolerance,
MedicationRequest, Immunization and Coverage (when the sandbox has one). Add
`--labs` to also pull `category=laboratory` Observations. The script searches
each type once with `_count=20` and keeps at most 20 results, because Oracle
ignores `_count` on Condition: it returned 1503 Conditions for one patient,
and ingest accepts at most 500 entries. On Oracle, Condition is also filtered
by `category=problem-list-item`. SMART's Conditions have no category, so that
filter is not used there.

Oracle times out (504) on large searches. The script retries a 504 or a
timeout once. If the second try also fails, the script leaves that type out,
prints `search failed`, and exits 3. Patient `12724066` fails this way on
Condition and Immunization even with the filters, which is why it is not the
default.

Every resource gets the HL7 v3 ActReason `HTEST` coding on `meta.tag`.
`contained` resources are dropped, because the link script refuses any
nested resource (Oracle serves an untagged PractitionerRole inside
AllergyIntolerance). Any `#id` reference to a dropped one goes too, and an
element left empty by that is removed. The script prints the dropped
contained resources as counts per type. Nothing else in the resource
changes. The vendor's own `display` and `text`
are kept exactly as served, because these bundles exist to test our
redaction. The script prints counts per type, never resource content.

### Ingest into a local engine

```bash
uv run flask --app main init-db
INTERNAL_TOKEN_MINT_SECRET=<local secret> KTC_EXPORT_ENABLED=true \
  PUBLIC_BASE_URL=http://localhost:5099 uv run flask --app main run --port 5099

INTERNAL_TOKEN_MINT_SECRET=<same secret> uv run python scripts/sandbox_test_patient.py \
  --source smart --out smart.json --ingest http://localhost:5099 --tenant sandbox-smart-test
```

`--ingest` posts `{"bundle": ...}` to `POST /r6/fhir/internal/ingest-bundle`
with `X-Tenant-Id` and `X-Internal-Secret`. The secret is read from
`INTERNAL_TOKEN_MINT_SECRET` and is never a command-line flag. The tenant
must match `^sandbox-(oracle|smart)-[a-z0-9-]+$`, and the engine URL must be
https (plain http only for `localhost` and `127.0.0.1`). Both are checked
before the sandbox is read. No request the script makes follows a redirect,
and proxy and CA-bundle environment variables are ignored. The script
prints the counts and error codes from the engine's response.

To run the export afterwards, mint a token with
`POST /r6/fhir/internal/step-up-token` using `purpose` `ktc` or `checkin`
and all six types, then call `POST /r6/fhir/$share-bundle` with that
`profile`, the same types and the sandbox `patient_id`.
`tests/test_ktc_export.py` shows both calls.

### What the export does with sandbox records today

Neither sandbox puts `meta.profile` on any resource. The check-in and ktc
exports only send rows that claim an allow-listed US Core profile
(`r6/checkin.py` `CLAIMS_BY_TYPE`), so an ingested sandbox patient exports
nothing. ktc answers 422 ("no exportable Patient record"), and checkin
answers 200 with an empty bundle. To exercise the redaction, stamp a US Core
claim on a copy of the bundle before you ingest it. Do not change the script
to do this, because the unclaimed records are the real vendor shape.

## Make a synthetic SMART Health Link

```bash
uv run python scripts/ktc_shl_link.py smart.json
uv run python scripts/ktc_shl_link.py smart.json --ttl 600 --label "Run 3"
```

This is a Python port of `prototype/scripts/ktc-shl-link.mjs` in
FHIR-IQ/rxgenie. The script:

1. Refuses the bundle (exit 2, nothing sent) unless it is a Bundle with at
   least one entry, and every entry's resource has the HTEST coding on
   `meta.tag` or `meta.security`. The check is `r6/ktc.py` `is_test_data`.
   It also refuses a resource that nests another one (`contained`, a nested
   Bundle), and any key outside the Bundle's `resourceType`, `id`, `type`,
   `meta`, `timestamp` and `entry` or an entry's `resource` and `fullUrl`.
   What it sends is rebuilt from the checked resources only, as
   `{"resourceType": "Bundle", "type": "collection", "entry": [{"resource":
   ...}]}`, so the harness gets exactly what was checked.
2. Encrypts the rebuilt bundle as a compact JWE: `alg` `dir`, `enc` `A256GCM`, `cty`
   `application/fhir+json`, with the protected header segment as AAD and a
   fresh 32-byte key. The crypto is `r6/shl.py`, the same code the engine
   uses for `deliver: link`.
3. POSTs `{"jwe": ...}` to `https://pshd-shl.exe.xyz/shl` and reads back the
   link id. A redirect is not followed and fails the run.
4. Prints a U-flag `shlink:/` with the file URL, the key, an `exp` of at
   most one hour (`--ttl` accepts 1 to 3600 seconds) and a label (default
   "Synthetic test records", 80 characters at most).

The `.mjs` encodes the payload with standard base64 (`btoa`). The script
uses base64url, as the SHL spec and `r6/shl.py` do.

Things to know about the harness:

- The harness never receives `exp`. It is only a hint inside the payload. In
  a live check (2026-10-26) the harness served the same file on a second
  fetch, so treat a link as readable by anyone who holds it for as long as
  the harness keeps the file.
- Only the key in the shlink can decrypt the file. The script prints the
  shlink once and stores it nowhere.

## Tests

`tests/test_sandbox_scripts.py` covers both scripts with no network access:

- the HTEST refusal: an untagged entry, the wrong system or code, an empty
  bundle and a non-Bundle are all refused, and nothing is posted;
- a JWE round-trip through `r6/shl.py` `decrypt`;
- the header, IV and tag sizes and the AAD;
- the shlink payload: flag, `exp`, label and URL;
- the puller's bounded searches, the 504 retry, the 20-per-type cut, and
  vendor text kept verbatim.
