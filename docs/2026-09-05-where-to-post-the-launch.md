# Where to post the launch, ranked by fit

Source list: [PlacesToPostYourStartup](https://github.com/mmccaff/PlacesToPostYourStartup)
(CC0), 19 subreddits and 85 sites. This document is the cut of that list that
fits what we are: an open-source guardrail layer for health-data agents, and a
consumer app in a gated beta. Everything not listed here was read and set
aside, with the reason in the last section.

Status: a plan. Nothing here has been posted. The pacing rule and the two
gates below come first.

## Two gates and one rule, before any post

1. **Real-record beta stays closed until the FTC Health Breach Notification
   Rule decision is made** (#168). Every post below invites people to the
   synthetic track, or to the code. None invites anyone to connect their own
   records. `docs/2026-08-16-tester-program.md` explains why.
2. **The consumer directory listings are blocked on two human actions**
   (Claude connectors need a Team plan; the ChatGPT app needs org
   verification). A launch post that says "add it to your assistant" before
   those clear sends people to a dead end. Post the developer story first.
3. **One external post per day, and none into a channel that has not
   answered a previous one.** Nine ecosystem touchpoints from July are still
   waiting on maintainers. Saturation reads as spam.

## What we are posting

Two stories, two audiences.

| Story | Audience | One-line pitch | Call to action |
|---|---|---|---|
| **The guardrail layer** | developers, FHIR integrators, agent builders | An open-source layer between an AI agent and a patient's record: redaction on every read, an audit row on every access, step-up auth on writes, and a human gate no agent tool can reach. Conformance is verifiable live. | Clone it, run the conformance probe, open an issue. |
| **The consumer app** | people who want to ask an agent about their own record | Connect your records, ask, and approve what the agent proposes. Nothing executes without you. | Try the synthetic patient. Ten minutes, zero risk. |

Assets each post needs, none of which exist yet: a demo GIF of the human gate
refusing an unapproved write; a clean-clone `docker compose up` that a stranger
has verified; three fresh good-first-issues.

## Tier 1: post here first

High fit. Each one has an audience that reads what we are and can act on it.

| Place | Story | What to post | Notes |
|---|---|---|---|
| Show HN | guardrail | "Show HN: open-source guardrails between an AI agent and your health record" | Text post. Lead with the human gate and the live conformance endpoint. Answer every comment the same day. |
| Product Hunt | consumer | CareAgents, synthetic track | Needs the GIF and five screenshots. A Tuesday or Wednesday. |
| r/SideProject | consumer | the build-in-public story | Plain language; no acronyms in the title. |
| r/buildinpublic | both | the merge-queue and evidence-first process | This subreddit rewards honesty about what does not work yet. |
| r/indiehackers and Indie Hackers | consumer | "solo founder, guardrailed health agent, gated beta" | Indie Hackers wants a product page plus a milestone post. |
| The Changelog (ping) | guardrail | open-source news tip | Short, factual, link to the repo and the conformance page. |
| r/AlphaandBetausers | consumer | recruit synthetic-track testers | Exactly what the tester program needs: strangers with a phone. |
| BetaBound and BetaTesting | consumer | the same recruitment, on a form | Paid listing options exist; start with the free tier. |
| StackShare | guardrail | tool profile | Developer discovery; needs a logo and a one-paragraph description. |
| SaaSHub and AlternativeTo | both | product listings | Discovery by search. AlternativeTo needs a category; "health record agent" has few peers, so file under AI assistants. |

## Tier 2: post after the first responses

Good fit, lower reach or more preparation.

| Place | Story | Notes |
|---|---|---|
| r/Startups, r/Entrepreneur | consumer | Rules require a story, not a link. Tell the beta-program story. |
| r/SaaS, r/microsaas | guardrail | Only if a hosted tier exists to describe. Today it does not. |
| Launched, LaunchIgniter, Launching Next, Tiny Launch, Startup Stash, Startup Base, Startup Inspire, Startup Buffer, Startup Tabs, Killer Startups, SnapMunk, Postmake, Side Projectors, Startups.gallery, Awesome Indie, 10words, Website Hunt, Land-book | consumer | Submission forms. One afternoon fills all of them. Low individual reach; useful for backlinks and search. |
| AI Collection | both | An AI-tool directory; fits the assistant story. |
| Slant | guardrail | Only once there is a comparison to be in ("best MCP servers for health data"). |
| Crunchbase, F6S, Wellfound (was AngelList) | company | Company profiles, not launch posts. Fill them once, keep them current. |
| G2, Capterra, GetApp, Software Advice, CrozDesk, Discover Cloud | consumer | Review sites. They need customers who will leave reviews. Not yet. |
| Starter Story | company | A founder interview, after there is a revenue or user number to state. |
| Startup Ranking, StartupBlink, Startup Tracker, Startup Benchmarks, Tech Map | company | Databases. Register and move on. |

## Not a fit, and why

- **Mobile app review sites** (App Rater, Appoid, appPicker, Apps Listo,
  Apps Mamma, AppsThunder, Appvita, PreApps, Tapscape, Web App Rater, State of
  Tech): CareAgents is web and text, not an app-store app.
- **Regional outlets** (BuiltInChicago, Arctic Startup, Geek Wire, Inc 42,
  Next Big What): we are not in those regions.
- **r/Coupons, r/LadyBusiness, r/thesidehustle, r/shamelessplug,
  r/plugyourproduct, r/IMadeThis, r/IndieBiz**: audience mismatch, or
  low-signal link dumps.
- **r/RoastMyStartup, r/Design_Critiques**: worth a post on the landing page
  design, but they are feedback venues, not launch venues. Later.
- **Gust, Vator, Collaborizm, Loop, Netted, MakeUseOf, Startup Beat,
  Startup 88, Tech Pluto, eBool, All My Faves, All Startups, All Top
  Startups, Alternative.me, Getworm, Micro SaaS Examples, PitchWall,
  Saijo's Tools List, Simple Lister, SimilarSiteSearch, Beta Bound's paid
  tiers**: either fundraising channels, pay-to-list, or inactive. Skip.

## Health-specific channels not on that list

The source list is general. The channels that matter most for the guardrail
story are the ones already opened in July, tracked in the contribution plan
kept outside the public repository: the FHIR community chat, the HL7
connectathon track, the MCP registry, the awesome-lists, and the partner
repositories. The pacing rule applies to them first.

## How to measure

One sheet, one row per post: date, place, story, link, replies in 48 hours,
sign-ups to the synthetic track, issues opened. If a channel produces
nothing in a week, it is not a channel for us; do not post there again.
