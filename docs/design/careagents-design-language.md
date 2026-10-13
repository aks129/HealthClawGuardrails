# CareAgents design language

The consumer app is a health assistant that a person messages. The conversation
is the product, so the interface around it stays quiet. This page is the
reference for `careagents/static/careagents.css` and the CareAgents templates.
The HealthClaw project site has its own system in `design.md`.

## Principles

1. **The conversation is the product.** No new interfaces where a message will
   do. Each screen has one main action.
2. **Calm, not clinical, not playful.** A cool white ground, one accent, plenty
   of space. No gradients and no decorative shadows. Hairlines separate
   things; cards hold only records and numbers.
3. **Readable by everyone who will use it.** Our testers include people in
   their seventies. Body text is 17px, line height 1.5, every tap target is at
   least 44px, and every text and background pair clears WCAG AA in light and
   dark.
4. **Made-up records always look made up.** One colour, amber, means "practice
   records". Nothing else uses it.
5. **Move only to show what changed.** A new message or a sheet fades in over
   120–160ms. With `prefers-reduced-motion`, nothing moves.

## Tokens

All colours are custom properties on `:root`. Dark mode redefines the same
names under `@media (prefers-color-scheme: dark)` (guarded by
`:root:not([data-theme="light"])`) and again under `:root[data-theme="dark"]`.
No rule outside those blocks names a hex value.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#F6F8F7` | `#0E1412` | Page ground |
| `--surface` | `#FFFFFF` | `#161D1B` | Bubbles, rows, cards |
| `--surface-2` | `#EEF2F0` | `#1D2624` | Hover, quiet chips |
| `--ink` | `#101715` | `#E7EEEB` | Body text |
| `--ink-soft` | `#56635E` | `#A2B0AB` | Secondary text |
| `--hairline` | `#DDE4E1` | `#2B3633` | Borders and rules |
| `--field-line` | `#7E8C87` | `#6F7F79` | Input borders (3:1 non-text) |
| `--accent` | `#0B6B5D` | `#5CC8B3` | Primary button, focus, links |
| `--accent-hover` | `#08564B` | `#7FD7C6` | Hover |
| `--accent-tint` | `#E2F0EC` | `#133630` | User bubble, selected row |
| `--accent-ink` | `#0A5A4F` | `#8EDCCB` | Accent text on any ground |
| `--practice-ink` / `-bg` | `#8A5100` / `#FCF1DC` | `#F2C47E` / `#33270F` | Made-up records only |
| `--ok-ink` / `-bg` | `#1D6338` / `#E4F2E8` | `#86D6A1` / `#12301D` | Connected, done |
| `--warn-ink` / `-bg` | `#A23A12` / `#FCEAE2` | `#F5A884` / `#3A2016` | Worth a look |
| `--critical-ink` / `-bg` | `#A8201A` / `#FBE6E4` | `#FF9F97` / `#3D1714` | Errors, destructive |
| `--radius` / `--radius-sm` | 12px / 8px | same | Containers / controls |

Contrast, measured: body ink on the ground is 17:1, secondary ink 5.9:1, white
on the accent 6.4:1, practice ink on practice amber 5.8:1. The lowest text pair
in either theme is above 5:1.

## Type

**Atkinson Hyperlegible Next** for every word, at 400, 600 and 700.
**Atkinson Hyperlegible Mono** for lab values, units, codes and the pairing
code, always with tabular figures. Both are self-hosted
(`scripts/vendor_frontend_assets.py`); a page never loads a font from a third
party.

| Role | Size | Weight |
| --- | --- | --- |
| Landing headline | `clamp(34px, 6vw, 56px)`, line height 1.1 | 700 |
| Page title | 28–36px | 700 |
| Section heading | 21px | 600 |
| Row or card title | 17–19px | 600 |
| Body, chat bubble | 17px, line height 1.5 | 400 |
| Secondary | 15–16px | 400 |
| Badge, smallest | 14px | 600 |

Hierarchy comes from size and weight. No italics for emphasis, no all-caps
labels.

**Why Atkinson.** The Braille Institute drew it for low-vision readers. Letters
that other faces make look alike (I, l and 1; O and 0; rn and m) have distinct
shapes, which matters when the text is a dose or a lab value. It is free under
the OFL, so we can host it ourselves.

## Components

- **Chat.** Assistant bubbles are white with a hairline. The user's bubbles are
  in the accent tint. Bubbles stop at 65 characters wide. Tool activity shows as
  a quiet line with a check icon, not a pill.
- **Buttons.** One primary (filled accent) per screen. Secondary buttons are
  white with a border. Destructive buttons have a dashed red border and no fill
  until hovered.
- **Rows.** Settings and pickers are lists of rows: one idea per row, at least
  52px tall, with a chevron when the row opens something.
- **Cards.** Only for records and numbers: a record connection, an assistant,
  the visit brief. Brief items sit in one bordered list, and lab values are
  set in the mono.
- **Icons.** Line icons on a 24px grid with a 1.5px stroke, drawn as inline SVG
  or as CSS masks (`--i-chat`, `--i-key`, `--i-chevron`, `--i-check`,
  `--i-dots`), so they take the text colour. No emoji as decoration: a
  persona's emoji in the markup draws as the chat icon.
- **Wordmark.** Lowercase `careagents` in the sans at 600. No second colour.
- **Focus.** A 3px accent outline on `:focus-visible`, never removed.

## Why amber means made-up

Testers start on synthetic records, and a person reading "your A1c rose" must
never think it is about them. So made-up records have their own colour: the
sample banner in the chat and the brief, the "Made-up records" badge on the
hub, and the source line on each sample brief item. Amber is used for nothing
else. The warning colour is a red-orange, and the general beta banner is
neutral, because on the hub it can say "your records are connected". Colour is
never the only signal: every amber element also says "made-up" in words.

## Do and don't

| Do | Don't |
| --- | --- |
| Use a token for every colour | Put a hex value or `rgba()` in a rule |
| Use one filled button per screen | Put two primary actions side by side |
| Use hairlines to separate | Add shadows to lift things off the page |
| Use the mono for numbers and codes | Use the mono for sentences |
| Use amber for made-up records only | Use amber for warnings or "beta" |
| Say what a thing does in plain words | Say "Grade A", "passkey" or "guardrail" in marketing copy |
| Keep motion at 160ms or less | Animate anything that is already on screen |
