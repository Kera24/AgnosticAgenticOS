# Agentic OS Control Centre — Design System (MASTER)

Global source of truth for the dashboard UI. Page-specific overrides live in
`design-system/pages/<page>.md` and win over this file for that page only.

Provenance: generated with the **ui-ux-pro-max** design-intelligence workflow
(`--design-system`, variance 6, motion 3, density 8, plus `typography`,
`chart`, `ux`, and `react` domain searches) and the Anthropic
**frontend-design** skill (visual direction, typography risk, signature
element, restraint rules). Where the two disagreed, the reasoning is recorded
inline.

---

## 1. Design rationale

**Subject.** An operational cockpit for an autonomous software-development
system: specialised agents (architect, conductor, coder, QA, security) hand
work to each other under deterministic gates, circuit breakers and capacity
budgeting, all running locally.

**Direction.** *Precision Operations Console × Contemporary Developer Tool ×
Editorial Technical Interface.* The interface is an instrument, not a
marketing surface: calm graphite materials, hairline structure, disciplined
density, one electric operational accent, and typography that treats data as
the hero.

**Deliberate divergences from the raw ui-ux-pro-max recommendation.**
The `--design-system` run returned "Modern Dark (Cinema Mobile)": Tailwind
slate (`#0F172A`), Inter-only, glassmorphism/glow effects. Three parts were
rejected on the record:

1. *Slate-blue dark + Inter-only* is the template answer for every dark
   dashboard (the frontend-design skill names it a known AI default). We keep
   the **dark-first, status-colour, data-dense-but-scannable** strategy and
   replace the material with warm graphite and the type with a two-family
   system that the skill's own `typography` search ranked for developer
   tools: **Developer Mono — JetBrains Mono + IBM Plex Sans**.
2. *Glassmorphism/ambient glow* is rejected: the product brief demands
   restraint; translucency is allowed only for the command palette scrim.
3. *Green `#22C55E` accent* is reassigned to the **healthy** status semantic
   only. The operational accent must not collide with status colours.

**Kept from ui-ux-pro-max** (priority table + domain searches): dark-first
with explicit status colours; density dial 8 (8–32 px spacing); subtle
motion tier (150–300 ms, no ambient animation); chart guidance (line/status
charts, series distinguished beyond colour, pause for streaming, text
fallbacks); UX rules (visible focus rings, skip link, heading hierarchy,
44 px touch targets, no emoji icons, no colour-only meaning).

**Signature element (the one aesthetic risk).** The **orchestration rail**:
a horizontal signal-path — square stage nodes joined by 1 px trace lines,
like a schematic — showing Architect → Conductor → Coder → Gate → QA →
Security → Commit. The deterministic Gate node is *visibly non-AI*: square,
hatched border, mono label `GATE`, never animated as "thinking". The rail is
the product's identity; everything around it stays quiet.

---

## 2. Colour tokens

All colours ship as CSS variables on `:root` (dark, default) and
`[data-theme="light"]`. Components never hard-code hex values.

### Surfaces & structure (dark)

| Token | Value | Use |
|---|---|---|
| `--bg-base` | `#141518` | App background (warm graphite, never pure black) |
| `--bg-sunken` | `#101114` | Rails, wells, code/log beds |
| `--bg-raised` | `#1B1D21` | Panels, cards, table headers |
| `--bg-overlay` | `#222429` | Popovers, palette, drawers |
| `--bg-hover` | `#25272C` | Hover fill |
| `--border-hairline` | `#2A2D33` | Default 1 px structure |
| `--border-strong` | `#3A3E46` | Emphasis, inputs |
| `--text-primary` | `#E9E8E4` | Primary text (warm off-white) |
| `--text-secondary` | `#A9ACB4` | Secondary text |
| `--text-muted` | `#787C85` | Captions, disabled (large text only) |

### Accent

| Token | Value | Use |
|---|---|---|
| `--accent` | `#53C7E8` | Signal cyan: active nav, active rail stage, primary buttons, links |
| `--accent-strong` | `#7CD6F0` | Hover/active accent text |
| `--accent-dim` | `rgba(83,199,232,.14)` | Accent fills, selected rows |

### Status semantics (colour + icon + label, never colour alone)

| Status | Token | Dark value | Shape/icon convention |
|---|---|---|---|
| Ready / healthy / passed | `--status-ok` | `#5CD08C` | circle-check |
| Running / active | `--status-run` | `#53C7E8` | pulse-dot (static under reduced motion) |
| Cooling / waiting | `--status-cool` | `#9DB4EE` | clock / hourglass |
| Degraded / uncertain | `--status-warn` | `#E5B45B` | triangle |
| Blocked / human required | `--status-block` | `#EE9159` | octagon / hand |
| Failed / error | `--status-fail` | `#EC7070` | circle-x |
| Paused / unknown / none | `--status-idle` | `#8A8F98` | dash / question |

Each status also has a `--status-*-dim` fill (`rgba(...,.13)`) for chips and
row tints. Light theme swaps to darkened equivalents that hold ≥ 4.5:1 on
paper (`#0E7C3F`-family greens, etc.).

### Light theme (secondary)

Paper-graphite: `--bg-base #F4F4F2`, `--bg-raised #FFFFFF`, text `#1F2126`,
hairline `#DCDCD7`, accent darkened to `#0E7FA6` for 4.5:1 text contrast.
Same token names; only values change.

---

## 3. Typography

| Role | Family | Notes |
|---|---|---|
| UI / body | **IBM Plex Sans** (400/500/600) | All prose, labels, buttons |
| Data / identity | **JetBrains Mono** (400/500/600) | Numbers, ids, timestamps, statuses, eyebrows, code, the rail |
| Fallbacks | `ui-sans-serif, system-ui` / `ui-monospace, Consolas` | Self-hosted via @fontsource; no network fetch |

Mono is a *semantic* choice: anything that is machine truth (task ids, token
counts, exit codes, countdowns, backend names) sets in mono; anything human
sets in Plex Sans. This contrast is the typographic personality of the page.

### Type scale (dense-dashboard, density dial 8)

| Token | Size/line | Use |
|---|---|---|
| `--type-display` | 26/32 mono 500 | Big operational numbers only |
| `--type-h1` | 19/26 sans 600 | Page title (one per page) |
| `--type-h2` | 15/22 sans 600 | Panel titles |
| `--type-body` | 13.5/20 sans 400 | Default text |
| `--type-data` | 12.5/18 mono 400 | Tables, timelines, logs |
| `--type-label` | 11/16 mono 500, uppercase, +0.08em | Eyebrows, column headers, statuses |
| `--type-caption` | 12/16 sans 400 | Help text |

Body text never below 12 px. Uppercase mono labels are the only tracking
adjustment; body text tracks normal.

---

## 4. Spacing, radius, borders, elevation

- **Spacing scale** (px): `4, 8, 12, 16, 24, 32, 48` as `--space-1..7`.
  Panel padding 16; dense table cell 8×12; page gutter 24 (16 below 768 px).
- **Radius**: panels/cards `6px`, inputs/buttons `4px`, chips `3px`,
  status dots `50%`. **No pills** except live countdown chip; never on
  buttons.
- **Borders**: 1 px hairline everywhere structure is meant; never rely on
  shadow to separate dense data. 2 px left rule (status colour) marks
  attention rows (blockers, failures).
- **Elevation**: exactly three levels —
  `--shadow-1` none (border only), `--shadow-2` `0 4px 16px rgba(0,0,0,.35)`
  (drawers, popovers), `--shadow-3` `0 12px 40px rgba(0,0,0,.5)` (command
  palette, dialogs). Light theme divides opacity by ~3.

---

## 5. Motion principles (subtle tier, motion dial 3)

- Durations 120–260 ms; easing `cubic-bezier(.2,.8,.3,1)`.
- Motion is reserved for *state meaning*: rail stage handoff (trace pulse),
  countdown tick, backend reroute (chip slide), new blocker arrival (one
  120 ms left-rule flash), toast enter/exit, dialog/palette open.
- No ambient motion in data-dense areas; no parallax; no looping gradients.
- The running pulse is `opacity 1↔.45`, 1.6 s ease-in-out — the only loop.
- `prefers-reduced-motion: reduce` (or the in-app setting): all transitions
  drop to ≤ 1 ms, loops freeze at full opacity, countdown updates by text
  only. Live regions still announce.

---

## 6. Charts & data visualisation

(From ui-ux-pro-max `chart` domain, adapted to tokens.)

- Capacity trend: **line/area, SVG**, ≤ 300 points, area fill 12 % opacity of
  `--accent`; estimated series **dashed**, reported series solid — style
  difference, not colour difference, carries the distinction.
- Cycle history: compact bar row; outcome encoded by colour **and** an
  underline glyph (✓ ✕ ⏸) in the tooltip/table.
- Every chart has: an accessible name, a text summary (`<figcaption>` or
  visually-hidden), and a toggleable data table for screen readers.
- No y-axis lies: baselines at 0 for quantities; no fake smoothing.
- Estimated capacity is *always* labelled `ESTIMATE`; never presented as a
  provider quota (mirrors `capacity.py` confidence semantics).

---

## 7. Data density & layout

- Optimise for 1440 px; graceful at 1280/1024; monitoring-only at 375 px.
- Prefer **tables, rails, timelines and split panels** over uniform card
  grids. Cards are for genuinely independent units (backends).
- Max content width: none (operational tool uses the viewport), but line
  length for prose capped at 72ch.
- Empty states are instructions, not moods: say what the system is waiting
  for and the exact command/control that changes it.

---

## 8. Focus, keyboard, accessibility (WCAG 2.2 AA)

- Focus ring: `2px solid var(--accent)` + `2px` offset, on **every**
  interactive element; never removed without replacement.
- Skip link to `#main`. Landmarks: `banner`, `nav`, `main`, `contentinfo`.
- One `h1` per page; sequential heading levels.
- Hit targets ≥ 44×44 px for primary controls (dense table row actions may be
  32 px with adequate spacing, keyboard reachable).
- Dialogs: focus trap, `Esc` closes, focus returns to invoker.
- Live regions: `aria-live="polite"` for operation status, `assertive` only
  for failures and human-required blockers.
- Status is always icon + text label (+ colour); tooltips have keyboard
  triggers; tables use `<th scope>`; charts have text alternatives.
- Countdown timers are `aria-live="off"` with a static accessible label
  updated once a minute (avoid screen-reader spam).

---

## 9. Component interaction rules

- **Buttons**: primary (accent fill, dark text `#0C2530`), secondary
  (hairline border), danger (fail colour, confirmation always). Disabled
  buttons keep their label and gain a `title`/inline reason — a disabled
  control must explain itself.
- **Destructive/costly actions** (breaker reset, smoke test, project
  restart): explicit confirmation dialog stating the cost ("consumes real
  subscription allowance").
- **In-flight**: buttons show a working state and are locked against
  duplicate submission; server enforces single-flight too.
- **Toasts**: bottom-right, max 3, auto-dismiss 6 s except failures (manual).
- **Tables**: sticky header, row hover fill, mono data columns right-aligned
  for numbers.
- **Forms**: visible labels above fields; validation inline next to the
  field; helper text under; errors never only at the top.

---

## 10. Anti-patterns (enforced)

From both skills, binding for every page:

- No purple-to-blue AI gradients, glowing blobs, glassmorphism panels.
- No identical rounded-card grids; no oversized decorative KPI numbers.
- No emoji as icons (Lucide SVG only, `stroke-width: 1.75`).
- No colour-only status; no removed focus rings; no keyboard traps.
- No fake/placeholder operational data outside tests or labelled demo mode.
- No pure `#000` backgrounds; no light-mode-only assumptions.
- No ambient/looping animation in data areas; no animation of width/height.
- No raw hex in components — tokens only.
- No pill-shaped buttons; no marketing copy inside the operational app.
- The deterministic Gate is never drawn or animated as an AI agent.
