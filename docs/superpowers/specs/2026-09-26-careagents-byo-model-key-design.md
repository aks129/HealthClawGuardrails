# CareAgents: bring your own model key

Status: draft for owner review, 2026-09-26. Reviewed by the CTO agent;
its required changes are applied.
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
- Custom or self-hosted endpoints. They carry the whole server-side request
  forgery surface, so they wait for their own issue and design.
- More than one key per account, or keys per agent.

## 3. Today

The model is chosen per deployment. `careagents/config.py` reads
`ANTHROPIC_API_KEY`, `ANTHROPIC_OAUTH_TOKEN`, `OPENAI_API_KEY` and
`OPENAI_BASE_URL` from the environment. The durable worker passes that one
`Config` to `llm.complete` for every account.

There is no per-account secret storage, and no encryption at rest.

## 4. Providers at launch

| Provider | Base URL, fixed by us |
|---|---|
| `anthropic` | `https://api.anthropic.com` |
| `openai` | `https://api.openai.com/v1` |

Both hosts are on the vetted list from #833, so a user's key may serve real
records wherever the account's real-records setting allows. The user cannot
change the base URL.

## 5. Storage

New table `ca_model_credentials`. It holds account data, not health data, so
it belongs in CareAgents.

| Column | Notes |
|---|---|
| `id` | `cred_…` |
| `account_id` | foreign key, unique: one key per account |
| `provider` | `anthropic` or `openai` |
| `model` | optional; provider default when empty |
| `ciphertext`, `nonce` | the encrypted key |
| `key_version` | which master key encrypted it |
| `last4` | the only part ever shown again |
| `status` | `active` or `invalid` |
| `created_at`, `validated_at` | timestamps |
| `last_error` | a short code such as `rejected` or `out_of_credit` |

Replacing or removing a key deletes the row. The account event log in
section 10 is the history.

## 6. Encryption

One small module, `careagents/secretbox.py`, is the only code that touches
ciphertext.

- Algorithm: AES-256-GCM from the `cryptography` package already pinned.
- Nonce: 96 random bits per encryption.
- Associated data binds each ciphertext to its row:
  `careagents:model-key:v1|{account_id}|{credential_id}|{provider}`.
  A ciphertext copied to another row fails to decrypt. Someone who can write
  the database cannot point a stored key at a different provider.
- Master keys come from one secret, `CAREAGENTS_KEY_ENCRYPTION_KEYS`, in the
  form `v1:<base64>`. More versions can be listed later; the first encrypts
  and any listed key decrypts. The re-encrypt script is written at the first
  real rotation.
- If the secret is missing or malformed, the app still boots. The "Your AI"
  panel says the feature is unavailable, and saving is refused.

Moving to a cloud key service later changes only this module's internals.

## 7. Onboarding flow

A "Your AI" panel in account settings.

1. Choose Anthropic or OpenAI.
2. Paste the key. Optionally name a model.
3. Confirm presence with a fresh passkey assertion that requires user
   verification. The challenge has its own session key, is used once, and
   the verified passkey must belong to the signed-in account. An account
   without a passkey is asked to add one first.
4. The server checks the key with one minimal call carrying no health data.
   The call has a short timeout.
5. On success the key is encrypted and stored. The panel shows the provider,
   model and last four characters.

Removing a key needs no passkey, since it only reduces access. The panel
reminds the person to also revoke the key at their provider.

## 8. Using the key

`llm.complete` stops reading model settings from `Config`. It takes a frozen
`ModelSetting(provider, base_url, api_key, model)` instead. There are two
ways to build one:

- from the account's credential, decrypted per turn; or
- from the operator's environment, for accounts with no credential row.

This matters because the Anthropic path prefers the operator's OAuth token
when one is set. A copied `Config` with only the key swapped would silently
use the operator's token. The Anthropic client is also built with an explicit
base URL, so `ANTHROPIC_BASE_URL` in the environment cannot redirect a user's
key.

The worker call path uses `ModelSetting`. The unused synchronous path,
`run_turn` in `careagents/agent.py`, is deleted rather than migrated.

The plaintext key lives only in the `ModelSetting` for one turn.

## 9. No silent fallback

If an account has a credential row, its turns run on that key or not at all.

| Event | What happens | What the person sees |
|---|---|---|
| Provider rejects the key (401/403) | status becomes `invalid` | "Your AI key was rejected. Update it in Settings." |
| Key is `invalid`, or decrypt fails, or the master key is missing | turn refused before any call | "Your AI key is unavailable. Check it in Settings." |
| Provider says out of credit | `last_error` set, key kept | "Your AI provider account is out of credit." |
| Rate limited | existing retry path | existing rate-limit text |

Only an account with no credential row uses the operator default. Falling
back quietly would send a person's data under terms they did not choose, at
the operator's cost.

## 10. Security controls

- **Never returned.** No endpoint, page or export includes the key.
- **Never in the model's context.** The key never enters the system prompt,
  messages or tool results. A model cannot leak what it was never given.
- **Never stored outside its row.** Run events, tool-call records, the
  transcript store and HealthClaw never receive it.
- **Never logged.** A logging filter masks key-shaped strings (`sk-…`,
  `sk-ant-…`). Provider exceptions are reduced to status and error code
  before anything is logged or stored.
- **Fresh presence** to add or replace a key, as in section 7.
- **Rate limit** on key validation, per account. It stops the endpoint being
  used to test stolen keys.
- **Deletion.** Removing a key or deleting the account deletes the row.
  Database backups keep the ciphertext until they expire. Rotating the master
  key is what makes those copies unreadable.
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
  master key, a second key version.
- The database holds no plaintext key after save, found by searching the
  raw row.
- No account API response contains the key.
- The key never appears in `system`, `messages` or tool results given to
  `llm.complete`, in run events, or in the transcript store.
- Logs captured during save, use and failure contain no key.
- Adding or replacing fails without a fresh, verified passkey from the same
  account.
- The worker calls a fake provider with the user's key. With no credential
  row it uses the operator setting. With an operator OAuth token set, a
  user's key still wins.
- Every row in section 9: each refusal happens before any provider call.
- Mutation evidence for the associated-data binding, the presence gate, the
  no-fallback rule and the log filter.

## 13. Later, not in this spec

- Custom endpoints, with pinned public addresses, port 443, no redirects and
  sample data only.
- A sealed-box split, so the web process can encrypt but only the worker can
  decrypt.
- Spec B, platform credits, which replaces the operator default for accounts
  with no key.
