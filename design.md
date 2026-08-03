# Design system

Two surfaces, two committed looks. Neither is a default, and neither should
drift toward one. Read this before generating any UI, and change this file
first if the design needs to change — a component that disagrees with this
page is a bug in one of them.

**This file documents what ships.** Every value below was read out of the
stylesheet, not invented. If you change a token, change it here in the same
commit.

---

## The two surfaces

| | **CareAgents** — the patient app | **HealthClaw** — the project site |
| --- | --- | --- |
| Who | A person on a phone, often anxious, often not technical | Developers, health-IT people, standards folk |
| Where | `careagents/static/careagents.css`, `careagents/templates/` | `templates/index.html`, `static/css/` |
| Look | Warm editorial. Cream paper, ink, clay | Dark technical. Deep navy, cyan, mono |
| Feeling to aim for | Calm, unhurried, legible at arm's length | Precise, dense, credible |

They are allowed to look unrelated. One is a health product; the other is
documentation for an engine. Forcing a shared skin on both would make the
patient app feel like a developer tool, which is the failure we care about.

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

## HealthClaw site tokens

```css
--bg: #0A0E17;  --bg-card: #111827;  --bg-hover: #1a2235;  --border: #1e293b;
--cyan: #22d3ee;  --green: #34d399;  --amber: #fbbf24;  --red: #f87171;
--white: #f1f5f9; --gray: #94a3b8; --gray-dim: #64748b; --gray-dark: #334155;
--font-sans: 'Outfit', sans-serif;
--font-mono: 'JetBrains Mono', monospace;
```

Cyan is the accent and the only glow. Green, amber and red carry meaning
(pass / caution / fail) and must not be used decoratively — a red chip on this
site means something failed.

---

## Banned

- **Fonts:** Inter, Roboto, Open Sans, Lato, Montserrat, or a bare
  `system-ui` stack as the *primary* face. `-apple-system` stays in the
  fallback chain — that is performance, not laziness.
- **Purple gradients**, neon glows, and "AI-looking" iridescence. The one
  gradient we have is documented above.
- **Generic vector illustrations** and stock photography of smiling people
  holding tablets. Use real screenshots, real records (synthetic), or nothing.
- **Lucide / Heroicons defaults** dropped in unchanged. If an icon set is
  added, pick one deliberately and record it here.
- **Emoji as UI iconography.** Emoji are fine in persona identity (the
  personas ship with 🌿 🎯 ☀️) and in prose. They are not buttons.
- **Uneven padding.** If the top is 20px, the bottom is 20px, unless there is
  a stated optical reason.

---

## Constraints that outrank taste

These are not stylistic preferences. Violating them breaks the product.

1. **The CSP is `default-src 'self'`.** No CDN scripts. Anything you add is
   self-hosted or inline. This alone rules out most drop-in animation
   libraries.
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

---

## Motion

Restraint is the house style, and it is a decision rather than a shortfall.

- Transitions exist to explain a state change. If nothing changed state,
  nothing moves.
- 120–200ms, ease-out. Nothing above 300ms.
- Every interactive element has a defined hover **and** focus-visible state.
  Focus rings are never removed; keyboard users are not an edge case here.
- **No** scroll-jacking, parallax, or smooth-scroll libraries. Native scroll
  is what a phone user expects and what a webview reliably gives us.

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
  advice for making a marketing site memorable; wrong here. Blocked by the
  CSP, hostile to webviews, and a person checking whether they are due for a
  screening is not an audience to impress with a shader.
- **No "cheeky", "abrasive" or "dark comedic" brand voice.** Standard advice
  for escaping corporate blandness. In a product people open when they are
  worried, it reads as not taking them seriously. Our voices are Calm Guide,
  Straight Shooter and Sunny Coach — the range is *warmth*, not *edge*, and
  capability is identical across all three.

The goal these were meant to serve — do not look generated — is met by the
committed typography, the warm non-default palette, and prose written to the
constitution's rules.
