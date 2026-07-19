# CareAgents Beta — tester guide

Thanks for trying this. This guide tells you what you're getting into, what's
protected, what isn't, and what we're asking from you.

We'd rather you trust this because you understand it than because we told you
to. So this document is blunt about the limits.

---

## What this is

CareAgents lets you connect your own health records and talk to an AI agent
about them — on the web, or by text.

Underneath it sits **HealthClaw Guardrails**, an open-source safety layer
between AI agents and health data. The guardrails run on the server, not in
the app, so they apply no matter which surface you use and cannot be turned off
by the AI:

- **Redaction on reads** — identifiers are stripped before data reaches the model
- **An audit trail on everything** — every record access is logged
- **A human gate on actions** — the AI can *propose* a real-world action, but it
  cannot execute one. Only you can approve it, through a separate step the AI's
  own tools cannot reach.

All of it is public: <https://github.com/aks129/HealthClawGuardrails>. You can
read exactly what happens to your data. You can also run the whole thing
yourself instead of using our hosted version.

## What this is *not*

Read this part twice.

- **Not medical advice.** It's decision support. It can be wrong, and it can be
  confidently wrong. Do not make a medical decision because an AI agent said so.
- **Not your doctor, and not a covered entity under HIPAA.** This works under
  your individual **right of access** to your own records — you're using a tool
  to reach your own data. That's a different legal posture than a hospital
  or insurer, and it means the protections you may be assuming are *not* the
  ones that apply here.
- **Not an emergency service.** If you're having chest pain, trouble breathing,
  stroke symptoms, or thoughts of harming yourself — call 911 or your local
  emergency number. Don't ask the agent first.
- **Not a finished product.** This is a beta. Things break. That's why you're here.

---

## Two ways to test — please start with the first

### Track 1: synthetic records (zero risk)

Use the built-in sample patient. Nothing about you is involved. You can try
every feature, break things freely, and screenshot anything.

**Start here even if you intend to connect your own records.** It costs you ten
minutes and tells you whether this is worth your real data.

### Track 2: your own records (real data)

Connect a real provider, wearable, or health record source. This is where the
product actually proves itself — and where you should be deliberate.

Before you do:

- Understand where your data goes (next section).
- Know you can disconnect and delete.
- Don't put it on a screen you're sharing or recording. If you demo this to
  someone, **use the synthetic track.**

---

## Where your data goes

| Thing | Where it lives |
| --- | --- |
| Your health records | Behind the HealthClaw guardrails, scoped to your own private tenant |
| Your account (email, passkey) | CareAgents' own database |
| Which sources you connected | CareAgents' own database |
| **Your health records in CareAgents** | **Never. CareAgents stores no health data.** |

CareAgents holds an account and a pointer. The records themselves stay behind
the guardrails. That separation is deliberate and it's the thing to check us on.

**Your passkey/biometric never leaves your device.** Face or fingerprint unlock
happens locally; we receive a cryptographic signature, never your biometric.

**What the AI model sees:** redacted records, when a question requires them. Not
your whole file, not continuously.

### A real limitation, stated plainly

If you use the **Telegram or iMessage** surfaces: consumer chat apps are not
encrypted medical channels. Messages pass through Apple's or Telegram's
infrastructure under their terms, not under a healthcare agreement. You're
choosing to reach your own data over a consumer channel. That's a legitimate
choice — but make it knowingly. The web app doesn't have this exposure.

---

## Getting started

1. **Sign up** at [careagents.cloud](https://careagents.cloud) — email code, then
   add a passkey.
2. **Connect the sample records** first. Confirm things work.
3. **Create an agent.** Pick a name and a voice (Calm Guide, Straight Shooter,
   or Sunny Coach — this is tone only; capability is identical).
4. **Ask it something.** Try "what are my recent labs?" or "am I due for
   anything?"
5. **When you're ready**, connect a real source from the connect menu.

Sources marked **coming soon** aren't wired yet — that label is honest, not
sandbagging. Please don't spend time trying to make them work.

---

## What we're asking from you

Honest reactions, not politeness. Specifically:

**The most valuable thing you can send us is a moment of confusion or distrust.**
Where did you hesitate? What made you unsure whether to continue? Where did it
feel like it was hiding something? That's worth more than a feature request.

Also useful:

1. **Wrong answers.** If the agent says something incorrect about your health
   data, tell us — with the question you asked. This is the highest-severity
   category. Don't include the actual clinical values in the report (see below).
2. **Where it broke.** What you were doing, what you expected, what happened.
3. **Where it was useful.** Which question did it actually answer well? We need
   to know what to protect as much as what to fix.
4. **What you expected that didn't exist.**

### Reporting safely

**Never paste real health data, screenshots of your records, or access tokens
into a bug report, GitHub issue, or chat.** We do not need them and we do not
want them.

Report the *shape* of the problem: "I asked about my cholesterol trend and it
gave me a value from the wrong year." That's enough for us to reproduce it
against synthetic data.

If you think you've found a **security or privacy problem**, don't file it
publicly — see [SECURITY.md](../SECURITY.md).

### Where to send it

- **Bugs / features:** [GitHub issues](https://github.com/aks129/HealthClawGuardrails/issues)
- **Security or privacy:** [SECURITY.md](../SECURITY.md) — private disclosure
- **Anything else:** support@healthclaw.io

---

## Leaving

You can stop at any time and you don't owe us an explanation.

- **Disconnect a source** — stops new data flowing in.
- **Delete your data** — email support@healthclaw.io and we'll remove your
  tenant and confirm when it's done.
- **Just stop using it** — nothing will contact you.

If deleting is ever harder than connecting was, that's a bug. Tell us.

---

## What we owe you

- **We'll tell you when we break something** that affected you.
- **We won't sell or share your data.** There is no third party buying this.
- **We won't use your health records to train models.**
- **We'll answer you.** If you send feedback and hear nothing, that's a failure
  on our side — ping us again.
- **We'll be honest about what's real.** If something is half-built, it'll say
  so rather than pretending.

---

## Known limitations right now

Current as of the beta launch — kept honest deliberately:

- Some connectors are **coming soon** and labeled as such.
- The agent can be wrong about your records. Always check anything that matters
  against the source.
- Real-world actions (calls, forms, refills) are **early**. Everything requires
  your explicit approval, and some are not built yet.
- This is a small team. Response times vary.

---

## Questions worth asking us

If you're the kind of tester who wants to pressure-test the claims rather than
the features — please do. Good ones:

- "Show me the audit trail for what you just read."
- "What exactly did the model see when I asked that?"
- "What happens if I revoke access right now?"
- "Prove the AI can't approve its own action."

All of those have real answers, and if any answer disappoints you we want to
know before someone else finds out.

---

*This guide is versioned with the project. If something here doesn't match what
the product does, the guide is wrong and we want to hear about it.*
