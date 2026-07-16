# CareAgents Accounts — real identity, biometric login, your health hub

**One line:** turn CareAgents from an anonymous sample-tenant demo into a
secure personal account — biometric (passkey) sign-in, verified real-records
connection, and one home to manage all your health connections, agents, and
surfaces (web, Telegram, …).

Decisions made under the session /goal directive and logged here.

## What changes

Today careagents mints an anonymous `ca-<hex>` tenant per browser. This adds an
**account** that owns everything, gated behind real identity:

- **Account** = email-verified identity + one or more **passkeys** (WebAuthn:
  Face ID / Touch ID / Windows Hello / security keys). No passwords, ever.
  Phishing-resistant biometric is the headline login.
- **Two identity tiers, deliberately distinct:**
  1. *Account identity* — you control this email + this device biometric.
  2. *Health identity (verified provider)* — you connect **real records**
     through **Fasten Connect** (the provider's own OAuth2 + login), and
     Fasten's HMAC-verified `patient.connection_success` webhook proves you
     control that patient portal. That is the "OAuth2 with a verified
     provider" anchor; careagents never sees the provider password.
- **The account owns and manages:** connections (data sources), agents, and
  surfaces. Everything the chat/forms-rail does is now scoped to *your* account.

## Data model (careagents' own SQLite — NO PHI, ever)

PHI lives only in HealthClaw tenants (redacted + audited). careagents stores
identity + pointers.

```
Account(id, email, email_verified_at, created_at, last_login_at)
Passkey(id, account_id, credential_id, public_key, sign_count, name, created_at)
Connection(id, account_id, kind[sample|fasten], tenant_id, label,
           status[active|pending|error], provider, connected_at)
Agent(id, account_id, connection_id, name, persona, created_at)
Surface(id, account_id, agent_id, kind[web|telegram|imessage],
        handle, status[active|pending], bound_at)
EmailToken(id, email, code_hash, purpose[verify|login], exp, used)   # magic link/code
```

- Each **Connection** maps 1:1 to a HealthClaw tenant (`ca-<hex>` still, now
  owned). `sample` seeds instantly; `fasten` starts the Stitch widget with
  `external-id=<tenant>` and flips `pending → active` on the verified webhook.
- **Agents** read one connection's records. **Surfaces** route a conversation
  with an agent to a channel (web today, Telegram real, iMessage roadmap).

## Auth flows

**Sign up:** enter email → Resend sends a 6-digit code → verify → immediately
prompted to **create a passkey** (biometric). Account is now yours.

**Sign in:** passkey (biometric) — one tap, no email round-trip. Fallback:
email code (for a new device before adding a passkey there). Session is a
signed cookie bound to `account_id` (rotated on login; Secure+HttpOnly+SameSite).

**Add a device:** sign in with an email code on the new device, then register
another passkey there. Multiple passkeys per account.

WebAuthn via `py_webauthn` 3.0.0. RP id = the site host (`careagents.cloud`);
challenge stored in the (short-lived) server session.

## Screens (taste-forward, same warm brand)

1. **/ (landing)** — unchanged story, CTA now "Get started" → auth.
2. **/auth** — email → code → passkey. Two inputs total, delightful.
3. **/home (dashboard)** — the hub:
   - *Your agents* — cards; "New agent" (name + voice + which connection).
   - *Your connections* — "Sample records" (instant) and "Connect real
     records" (Fasten, verified). Status pills (active/pending). Adding a
     Fasten connection opens the provider picker in a new tab; a live pending
     card resolves to active when the records land.
   - *Your surfaces* — Web (always on); **Telegram** ("Connect Telegram" shows
     a one-time code + deep link to the bot); iMessage/app marked *coming soon*.
4. **/chat?agent=<id>** — the existing streaming chat + forms rail, now scoped
   to the agent's connection tenant, behind auth.

## Surfaces

- **Web** — the PWA (add manifest + service worker so it installs to a phone
  home screen; that is the "app" surface without an app store).
- **Telegram** — real. "Connect Telegram" issues a one-time binding code; the
  user opens `t.me/<bot>?start=<code>`; the OpenClaw bot's `/start` calls the
  existing `POST /r6/fhir/internal/bind-telegram` (careagents brokers with the
  mint secret) to bind `chat_id → the agent's tenant`. From then the agent is
  reachable in Telegram, guardrailed identically.
- **iMessage** — needs a Mac/BlueBubbles/Sendblue relay; modeled in the schema,
  shipped as *coming soon* (honest, not faked).

## Security posture

- **No PHI in careagents storage** — only tenant ids + metadata; every record
  access still goes through HealthClaw redaction/audit/step-up. The mint secret
  and tokens stay server-side.
- **Passkeys** — no password to phish or leak; biometric stays on-device
  (WebAuthn never transmits it).
- **Fasten** — careagents never handles provider credentials; the verified
  webhook is HMAC-checked by HealthClaw before a connection goes active.
- **Session** — signed, rotated on login, Secure/HttpOnly/SameSite=Lax; account
  scoping enforced on every connection/agent/surface (a foreign id reads as 404,
  same pattern as the review relay).
- **Fail-closed** production config extended: `CARE_SESSION_SECRET`,
  `HEALTHCLAW_MINT_SECRET`, an LLM key, `RESEND_API_KEY`, and a writable data
  dir all required in prod.
- careagents' DB holds emails (PII, not PHI) — file-permission locked (0600),
  on the VPS only.

## Acceptance (works end to end)

1. Sign up with email → verify code → register passkey → land on /home.
2. Sign out, sign in with the passkey (biometric) alone.
3. From /home: create an agent on a fresh **sample** connection; chat with it
   (guardrailed, tool-grounded); run the intake form → signed PDF.
4. Start a **Fasten** real-records connection: pending card appears, provider
   picker opens; (webhook-driven activation verified with a simulated
   HMAC-verified success in tests / a real portal live).
5. "Connect Telegram" issues a code + deep link; binding maps the chat to the
   agent's tenant.
6. All routes 401/redirect without a session; foreign ids 404. careagents unit
   tests (WebAuthn + Fasten + Resend + mint faked) green; full repo suite green;
   `scripts/careagents_smoke.py` still passes the anonymous-compatible path or a
   new authed smoke covers it.

## Out of scope for this pass (phase 3)

iMessage relay wiring; caregiver/consent (managing another person's records);
billing; provider-directory search; standing approvals; multiple emails per
account; account deletion UX (export/delete endpoint stubbed, full flow later).
