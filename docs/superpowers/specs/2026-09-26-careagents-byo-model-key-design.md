# CareAgents: bring your own model key

Status: draft for owner review, 2026-09-26.
Scope: spec A of two. Spec B (platform credits) builds on this one.

## 1. Goal

A CareAgents user can power their assistant with their own model provider
account. They paste an API key once. From then on, their chat turns run on
their key, not the operator's.

Success means three things. The key works on the next turn. Nobody, including
us, can read it back through the product. A leaked database alone reveals
nothing usable.

## 2. Non-goals

- Platform credits, checkout and metering. That is spec B.
- Signing in with a consumer AI subscription. Those plans do not permit it
  for third-party apps. API keys only.
- Several active keys per account, or per-agent keys. One active key per
  account covers the need.
- Choosing among many models in the UI. An optional model name field is
  enough.

## 3. Today

The model is chosen per deployment. `careagents/config.py` reads
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and `OPENAI_BASE_URL` from the
environment. `Config.provider` picks Anthropic when its key is set. The
durable worker passes that one `Config` to `llm.complete` for every account.

There is no per-account secret storage, and no encryption at rest.

## 4. Providers at launch

| Provider | Base URL | Real records |
|---|---|---|
| `anthropic` | fixed: `https://api.anthropic.com` | allowed |
| `openai` | fixed: `https://api.openai.com/v1` | allowed |
| `openai_compatible` | user supplied, https only | sample data only |

The real-records column applies the rule from #833 per credential. A key on
a host outside the vetted set still works, but only on sample connections.
An operator can open a named host with `CARE_REAL_RECORDS_MODEL_HOSTS`, as
today.

## 5. Storage

New table `ca_model_credentials`. It holds account data, not health data, so
it belongs in CareAgents.

| Column | Notes |
|---|---|
| `id` | `cred_…` |
| `account_id` | foreign key, indexed |
| `provider` | one of the three above |
| `base_url` | only for `openai_compatible` |
| `model` | optional; provider default when empty |
| `ciphertext`, `nonce` | the encrypted key |
| `key_version` | which master key encrypted it |
| `last4` | the only part ever shown again |
| `status` | `active`, `invalid` or `revoked` |
| `created_at`, `validated_at`, `last_used_at` | timestamps |
| `last_error` | a short code such as `rejected` or `out_of_credit` |

At most one `active` row per account, enforced by a partial unique index.
Replacing a key revokes the old row and deletes its ciphertext.

## 6. Encryption

One small module, `careagents/secretbox.py`, is the only code that touches
ciphertext.

- Algorithm: AES-256-GCM from the `cryptography` package already pinned.
- Nonce: 96 random bits per encryption.
- Associated data binds each ciphertext to its row:
  `careagents:model-key:v1|{account_id}|{credential_id}|{provider}|{base_url}`.
  A ciphertext copied to another row fails to decrypt.
- Master keys come from one secret, `CAREAGENTS_KEY_ENCRYPTION_KEYS`, in the
  form `v2:<base64>,v1:<base64>`. The first encrypts; any listed key decrypts.
- Rotation: add a new first key, run a re-encrypt script, then drop the old key.
- If the secret is missing or malformed, the app still boots. Saving a key is
  refused, and existing keys are unusable, so turns fall back to the operator
  default. The health check reports the feature as disabled.

Moving to a cloud key service later changes only this module's internals.
The table does not change.

## 7. Onboarding flow

A "Your AI" panel in account settings.

1. Choose a provider. For `openai_compatible`, enter the base URL.
2. Paste the key. Optionally name a model.
3. Confirm presence. That is a fresh passkey assertion, or a new email code
   for an account with no passkey. The existing consent flow already has this
   gate; reuse it.
4. The server checks the key with one minimal call carrying no health data.
   The call has a short timeout.
5. On success the key is encrypted and stored. The panel shows the provider,
   model and last four characters.

Removing a key needs no fresh check, since it only reduces access. Replacing
one repeats steps 1 to 5.

## 8. Using the key

The worker already knows each run's agent, and so its account. Per run it:

1. loads the account's active credential, if any;
2. decrypts the key in memory;
3. builds a per-run model setting from it and calls `llm.complete`;
4. drops the plaintext when the turn ends.

With no active credential, the turn uses the operator's default, as today.
Spec B replaces that default with credits.

The Anthropic client is built with an explicit base URL. Otherwise the SDK
reads `ANTHROPIC_BASE_URL` from the environment and could send a user's key
to another host.

## 9. Failure handling

| Event | What happens | What the person sees |
|---|---|---|
| Provider rejects the key (401/403) | status becomes `invalid` | "Your AI key was rejected. Update it in Settings." |
| Provider says out of credit | `last_error` set, key kept | "Your AI provider account is out of credit." |
| Rate limited | existing retry path | existing rate-limit text |
| Real-record connection, key off the vetted list | turn refused before any call | "This key can only be used with sample records." |
| Master key missing | operator default used | nothing new |

The person's message distinguishes their provider from ours. Spec B will
need that distinction.

## 10. Security controls

- **Never returned.** No endpoint, page or export includes the key. Tests
  assert this for every account API response.
- **Never logged.** A logging filter masks key-shaped strings (`sk-…`,
  `sk-ant-…`) and the key in use for the current turn. Provider exceptions are reduced to status and error code before
  anything is logged or stored.
- **Never in run events.** Run events and tool-call records carry no key
  material. HealthClaw never receives the key.
- **Fresh presence** to add or replace a key, as in section 7.
- **Rate limit** on key validation, per account and per IP. It stops the
  endpoint being used to test stolen keys.
- **Base URL checks** for `openai_compatible`: https only, and the host
  must resolve to public addresses. The check runs again at call time,
  which closes DNS rebinding.
- **Deletion.** Account deletion deletes credentials. Revocation deletes
  ciphertext immediately.
- **Account event log.** Added, replaced, removed, rejected. Each entry has
  a timestamp, provider and last four only.

## 11. Rollout

Behind `CARE_BYO_KEYS`, off by default. Owner steps before turning it on:

1. Generate a 32-byte master key and set `CAREAGENTS_KEY_ENCRYPTION_KEYS` on
   both CareAgents web and worker.
2. Store a copy offline. Losing it makes every stored key unrecoverable;
   users would re-enter them.
3. Deploy web and worker from the same stage, as the runbook requires.

## 12. Testing

- Crypto: round trip, tampered ciphertext, swapped associated data, wrong
  master key, rotation across two versions.
- The database holds no plaintext key after save, found by searching the
  raw row.
- No account API response contains the key.
- Logs captured during save, use and failure contain no key.
- Add and replace fail without fresh presence.
- The worker calls a fake provider with the user's key, and with the
  operator key when none is set.
- A rejected key flips to `invalid` and shows the right message.
- Real-records rule per credential, including a lookalike host.
- Base URL checks: http, private IP, loopback, and a host that resolves
  private at call time.
- Mutation evidence for the associated-data binding, the fresh-presence
  gate and the log filter.

## 13. Open questions for the owner

- Should an account with no key and no credits still get the operator
  default once spec B ships? This spec assumes yes until then.
