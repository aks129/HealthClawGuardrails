# Beta program — plan

Internal. How we get from "a lot of working code" to "people actually using
this." Companion to [beta-tester-guide.md](beta-tester-guide.md) (the
user-facing doc).

---

## 1. The honest situation

The concern that prompted this — *"is this just an AI code-builder marathon to
nowhere?"* — deserves a direct answer.

**The building is not the problem. The evidence:**

- Guardrail conformance is **Grade A, 7/7, verifiable live** on prod right now
  (`GET /r6/fhir/$conformance`). Scope, stated in the report itself: a
  self-test of the guardrail layer against synthetic data — not a HIPAA
  assessment, third-party audit, or pentest of the deployment ([#186](../../../issues/186)).
- The own-data path is **proven end to end**: a real Epic record, 250 resources,
  Fasten connect → verify → export → webhook → ingest → agent read →
  `$interpret` + `$care-gaps`, all on prod.
- CareAgents is live, the connector marketplace ships 7 sources, the MCP server
  is published in the registry, and 14 skills are live on ClawHub.
- There is one real clinician user (Gigi) already onboarded.

**What's actually blocking users, from our own tracking:**

| Blocker | Type | Owner |
| --- | --- | --- |
| Consumer directory listings (Claude Connectors, ChatGPT Apps) | **All technical prereqs done.** Blocked on: buy Claude Team plan, OpenAI org verification | Human, hours |
| 2 outreach emails (HBO reply, Fasten) | Drafted, unsent | Human, minutes |
| 9 ecosystem touchpoints | Sent ~Jul 4-5, awaiting maintainer response | **Waiting — do not push** |
| Consent at connect-real-records (#167) | Small code change | Engineering, hours |
| `/nophi` promises an unimplemented control (#166) | Two-line removal, or implement | Engineering, minutes |
| FTC HBNR applicability (#168) | Legal determination | Counsel |

**So the bottleneck is a handful of human actions, not more features.** That is
worth sitting with before writing another line of code. The marathon produced a
real thing; what it hasn't produced is the twenty minutes of account setup and
two emails that would put it in front of people.

The most useful thing this plan can do is stop recommending building.

---

## 2. What actually works — evidence

Researched from primary sources (GitHub API, founder posts, HN threads).
Confidence is flagged; folklore is called out as folklore.

### The uncomfortable finding: launches mostly don't work

Four data points, all verified:

- **OpenClaw** (383,460 stars, 80,545 forks, ~368 contributors, repo created
  2025-11-24). Steinberger announced to **50,000 X followers and it landed
  flat.** What worked was a public Discord run as a **showcase** — building
  where people could watch the agent actually do things.
- **LangChain** posted to HN twice in late 2022 and got **3 points and 2
  points.** It broke out three months later riding the post-ChatGPT wave, in a
  submission *someone else* posted.
- **Ollama's** Show HN (284 pts) was **not their biggest hit** — later release
  posts hit 607 and 633. The launch was a starting gun, not the race. They also
  timed it **two days after Llama 2 shipped**, riding someone else's wave.
- **Medplum** got 251 points at launch and then **nothing above 15 points** ever
  again.

**Implication for Aug 18:** the webinar is not distribution. Treated as a launch
it will land like Steinberger's. Treated as a *showcase* — plus a reason to
follow up with every pending ecosystem contact — it's worth a lot.

### Medplum is our closest comparable — steal from it directly

Open-source FHIR platform, YC, same "engine" layer, launched Nov 2022. Four
moves, all verified from their Launch HN:

1. **They shipped a running demo application, not a README.** `foomedical.com`,
   full source public, plus a video. People replied "checking things out today"
   instead of asking what it did.
2. **Two names for the same product, by audience.** Cody Ebberson said so
   publicly: *"Among technical decision makers, the Firebase analogy clicks much
   faster. Among traditional healthcare administrators, 'EHR' or 'API first EHR'
   or 'Headless EHR' resonate more."* We have exactly this problem — "guardrails
   for AI agents on FHIR" lands with engineers and means nothing to a clinician.
3. **They absorbed the ugly, relationship-heavy work** for early customers —
   lab and e-prescribe network access — as a wedge, intending to productize
   later. Our analogue is obvious: do the record-connection work *for* our first
   testers rather than waiting for the connector to be self-serve.
4. **They named competitors generously** (Redox, HAPI FHIR, Google Health API).
   Ollama did the same with llama.cpp. It costs nothing and buys standing.

And the metric lesson: **their case studies lead with time-to-launch** ("initial
build in 16 weeks"). We should decide now what our equivalent single number is.
"Connect your records and get a guardrailed answer in X minutes" is probably it.

### Immich — the model that fits us best

Immich ran **no formal beta program at all.** No invite list, no cohort. Instead:
a permanent, prominent **"this is not production software" warning banner**
sustained for ~3.5 years, and shipping in the open. Alex Tran, June 2022, asked
whether it was safe for family photos — *while his own wife used it daily*:

> "I would still advise you to treat the application in the beta stage."

That honesty didn't repel users; it grew to 108,000 stars. And retiring the
banner at v2.0.0 became a **celebration event** (2,156 reactions).

This matches our ethos better than a cohort program does — this is a project
whose whole pitch is truthful failure paths. **An honest, prominent beta warning
is more on-brand and far cheaper than a gated program.**

One concrete tactic worth copying: Immich indexes its Discord publicly via
**Answer Overflow**, so support answers are searchable from Google. It fixes
chat's biggest weakness.

### Release cadence is the engagement mechanism

- **Home Assistant**: monthly releases, and **the last week before each release
  is beta week** — opt-in in settings, a dedicated `#beta` channel, a beta-week
  forum thread per release, and release-party livestreams.
- **OpenClaw**: 23 stable releases in ~3 months, betas every 2-3 days.

But Home Assistant also documented the failure mode, and it's the one that
applies to a solo maintainer. Their biweekly cadence burned out both developers
and community:

> "We were sprinting while we had to run a marathon; it wasn't sustainable."

They moved to monthly. **Monthly is the right target here, not weekly.**

### Our unfair advantage — use it

Most health-AI products ask you to trust a claim. We can **show the receipts
live**: the conformance endpoint grades itself A–F in public, the audit trail is
inspectable, and the human gate is demonstrable — *watch the AI try to approve
its own action and fail.* Almost nobody in this space can do that on camera.

That is the showcase, and it is a demonstration rather than a deck — exactly
what the HIMSS audience was told to expect.

---

## 3. Gates before we recruit anyone

Do not invite people to connect real medical records until these are closed.
This is not bureaucratic caution — each one is a promise we'd otherwise be
breaking.

**Hard gates (block real-record recruitment):**

- [ ] **#166** — remove or implement `/nophi`. We currently advertise a privacy
      control that does not exist. Two-line fix available today.
- [ ] **#167** — consent + terms at the connect-real-records moment.
- [ ] **#168** — FTC HBNR determination, and reconcile the privacy policy. The
      policy currently frames the hosted instance as a reference/demo; recruiting
      real-record testers contradicts that unless it's updated **first**.
- [ ] Verify **delete actually works** end to end before the guide promises it.

**Soft gates (block *scaling*, not starting):**

- [ ] Signup is currently **wide open** — no invite, no waitlist, no allowlist.
      Fine for 10 invited testers who are told the URL; not fine for a public
      launch. Decide before any wide announcement.
- [ ] Chat rate limit is 20 turns / 10 min, in-memory. It resets on restart and
      is per-worker, so real LLM spend exposure is higher than the number
      suggests. Bound it before opening the doors.
- [ ] **#154** — Playwright gives no e2e signal, so the consumer flows testers
      will hit are unverified by CI.

**Track 1 (synthetic records) has none of these gates.** Anyone can be invited to
the sample-record experience today. That's the move while the gates close.

---

## 4. Program design

### Cohort: start at 10, not 100

Ten engaged testers who reply beat a hundred who signed up once. At ten you can
personally read every piece of feedback and reply the same day — which is the
single thing that keeps early testers engaged.

**Cohort 1 (~10, invited, synthetic-only).** Recruit now; no gates block this.
People who will tell you the truth: clinician colleagues, health-IT peers, the
HL7/FHIR connectathon contacts, Gigi's colleagues.

**Cohort 2 (~10-15, invited, real records).** Only after the hard gates close.
These are the people who prove the product, and the ones who carry the most risk
— pick people you can call.

**Cohort 3 (open).** After directory listings land and the soft gates close.

### Channel: GitHub Discussions, and no Discord

The research changed my recommendation here, so it's worth showing the reasoning.

**Plausible — a two-person team — deliberately ran GitHub Discussions and never
opened a Discord**, with an explicit no-support-guarantee for self-hosters. Uku's
stated reason was survival: *"early on we tried to help each and every support
request from self-hosters and it quickly turned out an impossible task."* That is
our situation exactly, minus one person.

**The one real dataset on OSS chat channels** (DoltHub, 3+ years) is sobering:
15.5k GitHub stars produced **1,894 Discord joins (~12%), about half active in
30 days** — and it worked only because they staff a greeter who welcomes every
joiner, many times a day. Their conclusion: *"People don't like to just talk to
the maintainers of the project, they also want to meet other users."* A chat
channel survives as a **staffed support desk**; it dies when framed as a
self-sustaining community. We cannot staff a greeter.

**Cal.com's free Slack silently auto-deleted years of community knowledge**
before they migrated. **LangChain ran the most famous dev Discord of the LLM era
and has since moved to Slack + Forum, explicitly telling people not to ask
questions in chat.** Two independent projects concluding chat was the wrong
place for support.

If chat ever happens, copy **Immich's Answer Overflow** trick so answers are
searchable from Google — that fixes chat's fatal flaw.

### The warning most relevant to a solo maintainer

Nadia Eghbal's *Working in Public* is the load-bearing source here. Her finding:
most consequential modern projects are **"stadiums"** — high user growth, low
contributor growth — and the usual advice actively harms maintainers:

> "Pushing a larger number of people to make open source contributions, or
> expecting maintainers to foster a sense of community participation, can be
> counterproductive, as it requires the maintainers to spend more time on
> reviews and discussions."

So: **optimize for users, not contributors.** Do not measure success by
contributor count or stars. (On stars — a peer-reviewed ICSE 2026 study found
~6M suspected *fake* GitHub stars, and a survey of 791 developers found the
modal star is a **bookmark**. Stars are a credibility asset, not a user count.)

The documented failure mode for projects like this is not indifference — it is
**maintainer burnout**: core-js, colors.js, actix-web, and xz, where burnout was
literally the attack surface. Home Assistant named it directly: *"We were
sprinting while we had to run a marathon."*

Guard the cadence accordingly. Monthly, not weekly.

### The single highest-leverage artifact: adversarial positioning

Both Plausible and Cal.com won on the same move — define yourself against a
dominant incumbent, not by describing what you are:

- *"Why you should stop using Google Analytics on your website"* took Plausible
  from 27,300 visitors in 15 months to **48,000 in one week**, 166 new trials
  (more than the prior four months combined), doubled GitHub stars, and moved
  brand search from 10 to 121/week.
- Cal.com's entire pitch was six words: *"An open source Calendly alternative."*

Our version writes itself, and we have the receipts to back it where nobody else
does. Something in the shape of **"Why you shouldn't let a chatbot read your
medical records — and what to do instead."** That is the post, and the live
conformance grade is the proof.

Both founders also **under-sold on HN** — founder-submitted, no promotional
text, no gaming. For a project whose entire claim is trustworthiness, a gamed
launch would undercut the pitch itself.

### Timeline realism — the antidote to "marathon to nowhere"

**Plausible took 324 days to reach $400 MRR.** Fourteen months of near-flat
growth, building in public the whole time. Their self-hosted build shipped 16
months *after* the cloud beta, with a beta exit bar of "5 people running it
smoothly."

That is what this phase looks like from the inside. The feeling of building
toward nothing is the normal texture of month 8, not evidence of failure.

One caution against over-correcting: Justin Kan coined *"first-time founders are
obsessed with product, second-time founders are obsessed with distribution"* —
and then **publicly retracted it** after Atrium died with $75.5M raised. His
revised lesson: distribution doesn't save a product people don't want. The goal
is users who'd be upset if it disappeared, not traffic.

### The feedback loop

- A **standing question**, not "any feedback?" — ask "where did you hesitate or
  distrust it?" That's the question the tester guide leads with, and it surfaces
  the trust gaps that kill health products.
- **Reply to everything within 24h**, even just "seen, filed as #N."
- **Ship something visible weekly**, and tell testers which of their reports it
  came from. Naming the reporter is the whole retention mechanism.
- **Never ask for their health data.** Reports describe shape, not values.

### Where to find testers — free channels first

Paid recruitment was researched and is **not worth it yet**:

| Channel | Cost (20-50) | Verdict |
| --- | --- | --- |
| Warm network (Gigi's colleagues, HL7 connectathon, HIMSS attendees) | $0 | **Start here** |
| User Interviews | ~$3.5k-8.9k | Later, if warm runs dry |
| Rare Patient Voice | ~$12.4k+ | Overkill now |
| Inspire / PatientsLikeMe | ~$15k-50k (quote-only) | No |

Two findings make paid recruitment a poor fit *today*. The patient-community
platforms **sell insights to a sponsor, not testers who go authenticate into
your app** — asking members to connect live medical records is off-menu and
triggers privacy review at all of them. And the self-serve platforms bill **per
session, not per tester**, so a multi-week beta with three touchpoints can bill
3× what you budgeted.

We already have the better channel: a clinician partner with colleagues, a
connectathon network, CMS/HL7 contacts, and a webinar audience on Aug 18. Those
people are pre-qualified for exactly the trust question this product asks.

### What to measure

Vanity metrics will lie to you here. Signups are meaningless if nobody returns.

- Did they come back a second time? (the only early metric that matters)
- Did they connect a real source after trying synthetic? (the trust conversion)
- How many reported a *wrong answer*? (low numbers mean they aren't really using
  it, not that it's correct)
- Time from report → shipped fix.

**Do not reach for the standard playbook at this size** — most of it doesn't
apply, and some of it is folklore:

- **The Sean Ellis / Superhuman 40% PMF survey needs ~40 *respondents*** to be
  directionally valid — at a 30% response rate that's a 130-user beta. Below
  that it doesn't apply on its own terms. It's also asymmetric: **below 40% is
  informative, above 40% is not** (it measures problem-solution fit and
  false-positives easily).
- **The famous activation numbers are folklore.** Facebook's "7 friends in 10
  days" traces only to conference talks — the internal instruction was reportedly
  10 in 14. Twitter's "30 follows" was never said by Josh Elman; his actual essay
  reports 7 visits/month. Don't set targets from them.
- **Activation regression needs sample we don't have.**

What works at 10-50 users instead: **watch nearly every session** (PostHog's own
pre-PMF advice is session recordings first, and explicitly *not* funnel
optimization — "it encourages premature optimization, or worse, hides bigger
problems"), interview weekly, and judge by **pull vs push** — when someone keeps
using it, is it because they want it or because you asked them to? Rob Snyder's
bar is finding a "Hell Yes" user in the first 5-10. Nielsen's model says 5 users
surface ~85% of usability problems, so small N is genuinely sufficient here.

And do outreach as a human, not on a timer. PostHog found a shared channel per
customer got **20× the response rate of email**.

---

## 5. Ecosystem — convert, don't expand

The instinct to reach out to Medplum, Fasten, and others is right in direction
and wrong in timing.

**We already contacted them.** Medplum PR #9746 + issue #9616 comment, a comment
on Fasten #207, plus 7 more touchpoints — all opened ~Jul 4-5, **all still
awaiting maintainer response**. Our own pacing rule is explicit: *do not fire
new external contributions into cold trackers until some respond — saturation
reads as spam.*

Firing another round now makes us look like a bot to exactly the people whose
respect we need. Health IT is a small world with long memories.

**So: convert the pending nine rather than open a tenth.**

- **Send the two drafted emails.** The HBO reply (Bo + Jason Choe) and the
  Fasten one. A direct human email to someone who knows you outperforms a cold
  PR comment by a wide margin, and both are already written.
- **Health Samurai is warm and unblocked** — they *asked* for the joint blog
  post. That's the one ecosystem relationship with active reciprocal interest.
  Finish it; it's the highest-yield item on this list.
- **Use the webinar as the forcing function.** A talk with a live demo gives you
  a legitimate, non-spammy reason to follow up with every pending contact:
  "we're presenting this Aug 18, thought you'd want to see it." That converts
  cold threads warm without a new ask.
- **Then** open new touchpoints, one per day max, from the prepped wave-2 drafts.

**On Medplum specifically:** they're an open-source FHIR platform — the same
"engine" layer we sit on top of. The natural collaboration is the same story
we're telling Health Samurai (store → guard → advise), so the Aidbox post is
effectively a template. Wait for the existing PR to get a response first.

---

## 6. What I'd do this week

In order, highest leverage first:

1. **Ship #166** (remove the `/nophi` promise). Minutes. It's a live false
   assurance.
2. **Send the two drafted emails.** Minutes. Already written.
3. **Invite 5-10 people to Cohort 1 (synthetic).** No gates block this. Use the
   tester guide.
4. **Do the human gates** — Claude Team plan, OpenAI org verification. Hours,
   and they unblock the directory listings that produce actual strangers.
5. **Close #167** (consent) and get the #168 determination.
6. **Finish the Health Samurai post.**

Notably, only one of those six is writing code.

---

## Related

- [beta-tester-guide.md](beta-tester-guide.md) — the user-facing guide
- [healthcare-ai-advisors-roadmap.md](healthcare-ai-advisors-roadmap.md) — where the product is going
- Issues: #166, #167, #168 (beta gates) · #154 (e2e signal)
