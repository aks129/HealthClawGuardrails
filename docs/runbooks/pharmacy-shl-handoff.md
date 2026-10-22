# Runbook: sharing records with a pharmacy by text (RX Genie)

A CareAgents user texts PHARMACY, replies YES, and the engine makes a
one-use SMART Health Link of their records. CareAgents POSTs the link to a
pharmacy's intake URL. The pharmacy fetches the encrypted file from the
engine once. The link and the records never go by text.

The first receiver is the RX Genie test bench (https://rxgenie.us), a
proof of concept for the CMS Health Tech Ecosystem pharmacy work group. It
holds synthetic data only.

## How it works

1. PHARMACY (whole message). CareAgents answers one fixed question naming
   the pharmacy and what would be shared, and stores an offer row
   (`ca_pharmacy_shares`) that expires in 10 minutes. The sample connection,
   a disconnected connection and a paused account are refused here.
2. YES. The offer is answered once. CareAgents mints a `ktc` token and calls
   `$share-bundle` with `profile: ktc, deliver: link` for the tenant's one
   Patient and the six ktc types (Patient, Condition, AllergyIntolerance,
   MedicationRequest, Immunization, Coverage, plus the payor Organization by
   reference).
3. The engine profiles the bundle as the ktc export always does, encrypts it
   (JWE, alg `dir`, enc `A256GCM`, no `zip`), stores only the ciphertext in
   `shl_links`, and answers `{link_id, shlink, exp}`. The shlink payload is
   `url`, `key`, `flag: U`, `exp` and the fixed label "Shared health records".
4. CareAgents POSTs `{"shlink", "purpose": "Pharmacy visit", "consent":
   "Patient replied YES by text"}` to the intake URL, without following
   redirects, and texts a fixed sentence: sent, or could not send.
5. The pharmacy GETs `<PUBLIC_BASE_URL>/shl/<link_id>?recipient=...`. The
   engine serves the JWE once (by default), then answers 404. The last use
   drops the ciphertext. Expired rows are deleted on the next issue.

NO, no answer within 10 minutes, or a bare YES with no open offer shares
nothing. A bare YES or NO with no open offer goes to the assistant.

## Safety rules the code enforces

- **Test data only for now.** The engine refuses a link (403
  `ktc_not_test_data`) unless every exported record carries the HL7 v3
  ActReason `HTEST` marker. The Sendblue line is not under a BAA, and the
  link is made from a text conversation. `KTC_LINK_REAL_RECORDS` lifts this
  on the engine; set it only once the BAA exists. The shipped synthetic test
  patient (`careagents/checkin/us_core_test_patient.json`) carries the
  marker. The sample tenant (`demo-patient-rivera`) does not, and it also
  claims no US Core profiles, so it cannot be shared this way.
- **No link or record by text, log or row.** Replies are fixed sentences.
  Logs carry status codes and outcome codes. The consent row holds ids,
  times, the pharmacy host, the decision and an outcome code.
- **The person's word decides.** PHARMACY, YES and NO are read before any
  model turn. No tool call can start a share.
- **Off by default** on both sides: `CARE_PHARMACY_SHARE` on CareAgents and
  `KTC_LINK_ENABLED` on the engine.

## Settings

Engine (app.healthclaw.io):

| Variable | Default | Meaning |
|---|---|---|
| `KTC_LINK_ENABLED` | off | The ktc export's link answer only. Set this one. |
| `KTC_EXPORT_ENABLED` | off | The ktc export's plaintext bundle answer (and the link). Leave off: the design keeps plaintext in the engine. |
| `PUBLIC_BASE_URL` | unset | The engine's public https origin. The file URL is built on it and must fit in 128 characters with `/shl/` and a 43-character id. Unset or not https: 503 `ktc_link_unavailable`. |
| `SHL_LINK_TTL_SECONDS` | 3600 | Link lifetime, 60 to 86400. |
| `SHL_LINK_MAX_USES` | 1 | Fetches per link, 1 to 5. |
| `KTC_LINK_REAL_RECORDS` | off | Allow links of records without `HTEST`. Leave off until the BAA. |

CareAgents (careagents.cloud):

| Variable | Default | Meaning |
|---|---|---|
| `CARE_PHARMACY_SHARE` | off | The PHARMACY / YES / NO flow. `on` or `off`; anything else refuses to boot. |
| `CARE_PHARMACY_INTAKE_URL` | `https://rxgenie.us/pharmacy/api/intake` | Where the link is POSTed. Must be https on a DNS name, no userinfo, query or fragment; with the flag on, a bad value refuses to boot. |
| `CARE_PHARMACY_NAME` | `RX Genie Demo Pharmacy` | The name the texts use. |

Migration `0012_shl_links` adds the engine table. The CareAgents table is
created at boot like the others.

## Turning it on against rxgenie.us

1. Let RX Genie fetch from the engine's host. RX Genie fetches SHL files
   only from allowlisted or registered hosts. Either register at
   https://rxgenie.us/partner/connect (organization, contact email, client
   id, a JWKS URL, the SHL host, and the synthetic-data box), or add the
   host to RX Genie's own `SHL_ALLOWED_HOSTS`. The form fetches the JWKS
   when you submit and needs at least one EC P-256, EC P-384 or RSA public
   key in it. HealthClaw serves no JWKS today, so one has to be published
   first. The key matters only for RX Genie's backend-services token flow,
   not for this handoff. A registration lasts 90 days.
2. Engine: run the migration, set `KTC_LINK_ENABLED` and `PUBLIC_BASE_URL`.
3. CareAgents: set `CARE_PHARMACY_SHARE=on`.
4. Test with an account whose connection holds the synthetic test patient.
   Text PHARMACY, then YES. RX Genie's /pharmacy page should show the
   received records; check-in runs its network query.

## Known limits

- The YES reply waits on three calls (mint, link, intake POST, at most
  about 70 seconds in the worst case). Sendblue retries are deduplicated, so
  a slow call does not share twice.
- A link is not revoked when the intake POST fails. It expires within the
  hour, works once, and its key was only in CareAgents' memory and the
  request to the pharmacy.
- HELP does not mention PHARMACY yet.
- The design's PR 3 replaces the ktc bundle answer with the link answer and
  uses 15-minute links. This adds `deliver: link` beside the bundle answer
  and defaults to one hour; both are one setting or one follow-up away.
