# Design system

Two surfaces, two committed looks. Neither is a default, and neither should
drift toward one. Read this before generating any UI, and change this file
first if the design needs to change — a component that disagrees with this
page is a bug in one of them.

**This file documents what ships.** Every value below was read out of a
stylesheet, not invented. If you change a token, change it here in the same
commit.

**What this file does NOT own.** The Content-Security-Policy. That lives in
`app.py` and this file describes it. The two disagreed for months (see §
*Constraints*), which is the specific failure this note exists to prevent.

---

## The two surfaces

| | **CareAgents** — the patient app | **HealthClaw** — the project site |
| --- | --- | --- |
| Who | A person on a phone, often anxious, often not technical | Developers, health-IT people, standards folk |
| Where | `careagents/static/careagents.css`, `careagents/templates/` | `static/css/healthclaw.css`, `templates/` |
| Look | Warm editorial. Cream paper, ink, clay | Swiss editorial. Paper, ink, one ultramarine |
| Feeling to aim for | Calm, unhurried, legible at arm's length | Precise, dense, credible |
| Radius | `18px` — soft on purpose | `2px` — near-square on purpose |
| Depth | One shadow | **No shadows.** Rules and ground shifts only |

They are allowed to look unrelated. One is a health product; the other is
documentation for an engine. Forcing a shared skin on both would make the
patient app feel like a developer tool, which is the failure we care about.

What they *do* share: self-hosted type, a 16px reading floor, focus rings that
are never removed, and motion that stops when the reader asks it to.

---

## Why Swiss, for the project site

The brief was to match the best of what wins on Awwwards. Looking there
honestly is what produced this direction, including one inconvenient finding.

**The healthcare category is the wrong place to look.** Its most-cited entry,
[Possible Health](https://www.awwwards.com/sites/possible-health), is an
Honorable Mention from **May 2014** — WordPress, parallax, 7.4/10. It is not a
model for anything shipping in 2026.

**The typography category is the right one.** The most decorated recent winner,
[MONOLOG](https://www.awwwards.com/sites/monolog) (Typography Honors, Developer
Award, Site of the Day, July 2026), is two colours — `#080807` and `#DDDDD5` —
with typography as the primary element, built on GSAP and Three.js.

What makes it win is the two-colour discipline and the typographic hierarchy.
The Three.js is the part we cannot ship and do not need. Every hard constraint
below points away from the WebGL; none of them points away from the type.

So the direction is the **International Typographic Style**: a strict grid,
hierarchy from size and weight alone, hairline rules instead of shadows, one
grotesque plus one monospace, and tabular figures on every number. It is the
positioning choice for a product that has to read as rational and trustworthy,
and it needs zero JavaScript, so it survives the CSP, the webviews, and
`prefers-reduced-motion` intact.

---

## CareAgents tokens

```css
--cream:     #FBF6EE;   /* page */
--paper:     #F4ECDF;   /* raised surface */
--card:      #FFFDF8;   /* card */
--ink:       #22190E;   /* body text */
--ink-soft:  #5E5240;   /* secondary text */
--clay:      #C2532E;   /* primary action */
--clay-deep: #A03F1F;   /* links, hover */
--sage:      #5F7D62;   /* support, success */
--hairline:  #E4D7C2;   /* borders */
--shadow:    0 2px 24px rgba(84, 62, 36, .10);
--radius:    18px;
```

- **Display:** `"Fraunces", Georgia, serif` at weight 560, `letter-spacing:
  -0.015em` — h1, h2, wordmark, chat name.
- **Body:** `"Public Sans", -apple-system, sans-serif`, `line-height: 1.55`.
- One radius (`18px`) and one shadow. Do not introduce a second of either.
- The page background is a single soft radial warm-up at top right over
  `--cream`. That is the only gradient in the product.

## HealthClaw tokens

`static/css/healthclaw.css`. Two grounds share the sheet: `.surface-paper` is
the site, `.surface-console` is the operational dashboards, which keep the dark
look they already had and are not part of this system beyond the chrome.

```css
--paper:       #FAF9F5;   /* page — warm, never pure white */
--paper-sunk:  #F1EFE8;   /* alternating section band */
--paper-raise: #FFFFFF;   /* card */
--ink:         #14140F;   /* body text, and the primary button */
--ink-soft:    #55534B;   /* secondary text */
--ink-faint:   #76746A;   /* metadata, captions */
--rule:        #DFDCD2;   /* hairline */
--rule-firm:   #14140F;   /* the 2px rule under a section head */

--signal:      #1B2FBF;   /* THE accent. One. */
--signal-deep: #131F8A;
--signal-wash: #ECEEFA;

--pass: #1B6B45;   --warn: #7E5000;   --fail: #A82318;
```

**Why ultramarine.** Green, amber and red already *mean* pass, caution and fail
on this site. An accent drawn from those hues would make a decorative chip read
as a status claim. The accent has to sit outside the semantic set, and it is
also the deliberate opposite of the cyan-on-navy this site used to wear, which
is the single most common AI-developer-tool palette on the web.

**Semantic colours are darker than their dark-mode ancestors** because they now
sit on light paper and have to clear WCAG AA against it.

### Type

```css
--sans: 'Archivo', 'Helvetica Neue', Helvetica, Arial, sans-serif;
--mono: 'Fragment Mono', ui-monospace, 'SF Mono', Menlo, monospace;
```

- **Archivo** is a grotesque cut for headlines and dense body copy. Variable,
  300–800, so hierarchy costs no extra request.
- **Fragment Mono** carries data, codes, labels and every number.
  Deliberately **not** JetBrains Mono: every developer-tool site already uses
  it, and identifiers here should read as record-keeping, not as an IDE.
- Both are vendored (`static/fonts/`). So are Fraunces and Public Sans
  (`careagents/static/fonts/`). Re-vendor with
  `scripts/vendor_frontend_assets.py`.
- Numbers are `tabular-nums` everywhere. Two readings of the same lab test must
  not render at different widths.

Scale, hierarchy from size and weight only — no colour, no italics, no caps:

| role | size | weight |
| --- | --- | --- |
| hero | `clamp(2.5rem, 6.2vw, 4.75rem)` | 500 |
| section title | `clamp(1.625rem, 3.2vw, 2.375rem)` | 500 |
| card title | `1.1875rem` | 600 |
| lede | `1.1875rem` | 400 |
| body | `1.0625rem` (17px) | 400 |
| metadata, eyebrow | `0.75rem` mono, `0.09em` tracking | 400 |

### Spacing and grid

An 8pt scale, `--s1` 4px through `--s9` 144px. Every gap in the design is one
of these. Page max-width `1240px`; reading measure `64ch`.

---

## Banned

- **Fonts:** Inter, Roboto, Open Sans, Lato, Montserrat, or a bare
  `system-ui` stack as the *primary* face. `-apple-system` stays in the
  fallback chain — that is performance, not laziness.
- **Purple gradients**, neon glows, and "AI-looking" iridescence. The one
  gradient we have is CareAgents' background warm-up, documented above. The
  project site has **no** gradients and **no** glows; the `--cyan-glow` and
  `.hero-glow` it used to carry were exactly the tell this system removes.
- **Generic vector illustrations** and stock photography of smiling people
  holding tablets. Use real screenshots, real records (synthetic), or nothing.
- **Lucide / Heroicons defaults** dropped in unchanged. Font Awesome survives
  on the dashboards only, vendored, and is tracked debt rather than a choice.
- **Emoji as UI iconography.** Emoji are fine in persona identity (the
  personas ship with 🌿 🎯 ☀️) and in prose. They are not buttons. The project
  site's pipeline and audience cards used emoji as icons; they are numerals
  now, which also carry the ordering the pipeline actually has.
- **Inline `style=` attributes.** `templates/index.html` carried roughly two
  hundred of them, which is why no visual fact had an owner and the page drifted
  by construction. If you need a value, add a token or a component.
- **A second palette declared inside a page.** `skills.html` redefined
  `--cyan`, `--bg-card`, `--border` and `--gray` locally, shadowing the site's
  own, and asked for a font `base.html` had never loaded.
- **Uneven padding.** If the top is 20px, the bottom is 20px, unless there is
  a stated optical reason.

---

## Constraints that outrank taste

These are not stylistic preferences. Violating them breaks the product.

1. **The CSP is `default-src 'self'`, and that is now true.**
   `app.py::_CONTENT_SECURITY_POLICY` is the only definition. This file
   describes it; `scripts/check_table_stakes.py` parses it. Neither restates it.

   It is worth knowing why: this file claimed the strict policy for months
   while `app.py` allowed four CDN hosts, because the strict version had
   silently broken styling and fonts on every deploy, and the person who fixed
   the deploy did not own the two documents describing it. The checker had a
   *third* answer, exempting exactly two Google hosts. Three owners, three
   answers. Everything is vendored now, so the claim and the code agree.

   `frame-src` is the one remaining third party and it is load-bearing: the
   connect page embeds the Fasten Stitch widget, and TEFCA identity
   verification may navigate that frame to CLEAR or ID.me.

2. **In-app webviews are a first-class target.** People open CareAgents inside
   Telegram, Instagram and iMessage. Issue #224 exists *because* those webviews
   break native `prompt()`. Assume the webview lacks something before assuming
   it works.
3. **Phone first, one-handed, possibly outdoors.** Minimum tap target 44px.
   Body text never below 16px — iOS zooms the whole page on a smaller input
   font, which breaks the layout.
4. **Respect `prefers-reduced-motion`.** Someone reading a lab result they are
   frightened of should not have to sit through a transition.
5. **Contrast:** WCAG AA minimum, AA-large for display type. `--ink-soft` on
   `--cream` is the lightest permitted body combination; do not go lighter.
6. **Focus rings are never removed.** Keyboard users are not an edge case on a
   page that leads to a health record. `:focus { outline: none }` with a
   border-colour change as the substitute is a removal.

---

## Motion

Restraint is the house style, and it is a decision rather than a shortfall.

- Transitions exist to explain a state change. If nothing changed state,
  nothing moves.
- 120–200ms, ease-out. Nothing above 300ms. The one exception is the 260ms
  section entrance on the project site.
- Every interactive element has a defined hover **and** focus-visible state.
- **No** scroll-jacking, parallax, or smooth-scroll libraries. Native scroll
  is what a phone user expects and what a webview reliably gives us.
- Anything that reveals content on scroll must also reveal it when the
  observer is missing or motion is reduced. Content that never appears because
  the mechanism revealing it did not run is the worst version of this bug.

---

## Copy in the interface

Interface copy follows [docs/constitution.md](docs/constitution.md), plus two
rules specific to a health product:

- **Never state a clinical fact the records do not support**, and never
  present a partial list as complete. This is enforced in code
  (`careagents/personas.py` `SAFETY_CORE`), not just in style.
- **Say what a control does, not how it feels.** "Delete these records
  permanently" — not "Manage your data". A person deciding whether to trust
  us reads the button label, not the paragraph above it.

---

## Where we depart from general anti-slop advice

Recorded so nobody re-adds these thinking they were an oversight.

- **No WebGL hero, no Lenis or Locomotive scroll, no skeuomorphism.** Common
  advice for making a marketing site memorable, and it is what the Awwwards
  winner above is built from. Wrong here: hostile to webviews, discarded under
  `prefers-reduced-motion`, and a person checking whether they are due for a
  screening is not an audience to impress with a shader. The discipline that
  makes those sites win is available to us; the shader is not, and the
  discipline is the part that was doing the work.

  Note the honest version of this argument. It used to rest partly on the CSP
  blocking script CDNs, which was not true at the time it was written. The
  conclusion survives on the other three reasons; the retired one is recorded
  here rather than quietly dropped.

- **No "cheeky", "abrasive" or "dark comedic" brand voice.** Standard advice
  for escaping corporate blandness. In a product people open when they are
  worried, it reads as not taking them seriously. Our voices are Calm Guide,
  Straight Shooter and Sunny Coach — the range is *warmth*, not *edge*, and
  capability is identical across all three.

The goal these were meant to serve — do not look generated — is met by the
committed typography, two non-default palettes, and prose written to the
constitution's rules.
