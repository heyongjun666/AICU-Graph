---
name: anti-generic-ui
description: >-
  Detect and fix the "AI-generated look" in front-end UI — the generic styling
  coding agents default to (Inter everywhere, Tailwind indigo/slate ramp,
  card-in-card layouts, bounce easing, hero→features→pricing skeletons). Use
  BEFORE building any UI from scratch, and AFTER generating UI to audit and
  polish it. TRIGGER on: "make this look good/professional/less generic",
  "polish the UI", "it looks AI-generated", "design a landing page/dashboard",
  "improve the design", or any review of front-end visuals. Provides banned
  defaults, positive design rules, and a critique→audit→polish→harden process.
metadata:
  origin: original (inspired by Refactoring UI, Open Props, the "Impeccable" skill)
  license: MIT
---

# Anti-Generic UI

Coding agents converge on the same look because they trained on the same
templates (Tailwind UI, shadcn examples, Vercel starters). This skill makes you
*notice* that default and deliberately leave it.

## When to use

- **Before** building UI: set a design direction so you don't reach for defaults.
- **After** generating UI: audit against the tells below and polish.
- When the user says it looks "generic", "AI-generated", "flat", or "like every
  other SaaS site".

## The tells (if you see these, it's the generic look)

- **Type:** Inter / Roboto / Arial for everything; one weight; no display face.
- **Color:** Tailwind's default ramp untouched — `indigo-600`, `slate-900`,
  `gray-500` text on white; a blue→violet gradient on the hero.
- **Layout:** the skeleton hero → 3-column feature grid → logo cloud → pricing →
  FAQ → footer, every section a centered `max-w-7xl` with the same vertical
  rhythm.
- **Depth:** cards inside cards inside cards; `border border-gray-200` +
  `shadow-sm` on everything; uniform `rounded-lg`.
- **Motion:** bounce/elastic easing, 500ms+ transitions, everything fades up on
  scroll the same way.
- **Filler:** emoji as feature icons, gradient text on every heading, lorem-ish
  "Empower your workflow" copy.

## Banned by default (require a reason to use)

- Inter/Roboto/Arial as the *primary* brand face.
- Untouched Tailwind `indigo`/`slate`/`gray` as the brand palette.
- Pure black (`#000`) text/bg and pure gray text on colored backgrounds.
- Card nesting more than one level deep.
- `bounce` / `elastic` / `back` easing for UI transitions.
- The default hero→features→pricing skeleton with no point of view.
- Gradient text on more than one element per viewport.

## Positive rules (do this instead)

1. **Pick a real palette.** Choose a hue that isn't in the default scale, build a
   ramp with a consistent chroma, and define a *non-neutral* surface tone (warm
   off-white, cool near-black) instead of `#fff`/`#000`. One accent, used
   sparingly, beats three gradients.
2. **Pair type with intent.** A distinctive display face for headings + a clean,
   readable text face for body. Set a type scale (e.g. 1.25 ratio) and use weight
   and size — not color — for hierarchy.
3. **Establish rhythm.** One spacing scale (4/8-based), consistent line-length
   (~60–75ch for body), and a vertical rhythm you actually keep.
4. **Depth through light, not boxes.** Prefer layered, soft shadows and subtle
   contrast over `border + shadow-sm` on every card. Flatten nesting.
5. **Motion with purpose.** Short (120–240ms), eased (`ease-out` /
   custom cubic-bezier), and tied to a state change. Respect
   `prefers-reduced-motion`.
6. **One signature move.** Give the design a single memorable element — a custom
   cursor, a distinctive border treatment, an unexpected accent — so it isn't
   anonymous.

## Process: critique → audit → polish → harden

Run as separate passes, like a designer would — not one "make it pretty" step.

1. **Critique** — describe what's generic about the current screen in concrete
   terms (name the font, the indigo, the nesting). No fixes yet.
2. **Audit** — check against *Banned by default* and the *tells*. List violations.
3. **Polish** — apply the *Positive rules*: palette, type, spacing, depth, motion,
   signature. One concern at a time.
4. **Harden** — verify contrast (WCAG AA), reduced-motion, responsive at 360px,
   focus states, and that nothing regressed to a default.

## Common rationalizations

| Rationalization | Reality |
|---|---|
| "Inter is clean and safe." | Safe = invisible. It's the #1 tell. Pick a face with a point of view for headings. |
| "Indigo is the brand color." | If it's untouched Tailwind indigo, it's nobody's brand color. |
| "Cards keep it organized." | Nested cards add noise, not structure. Use spacing and type hierarchy. |
| "The bounce feels playful." | It reads as a default template. Use short ease-out. |
| "It's just an MVP." | The generic look is what makes MVPs feel disposable. One signature move is cheap. |

## Red flags

- A theme/palette that is byte-for-byte the framework default.
- `rounded-lg border shadow-sm` repeated on every container.
- Headings differentiated only by color, not weight/size/face.
- Animations all 500ms with the same easing.
- Three or more gradients competing in one viewport.

## Verification

- [ ] Primary font is not Inter/Roboto/Arial (or has an explicit reason).
- [ ] Palette is not the untouched framework default; surfaces aren't pure #fff/#000.
- [ ] No card nesting beyond one level.
- [ ] Transitions ≤ ~240ms, eased, and gated behind `prefers-reduced-motion`.
- [ ] Text contrast passes WCAG AA; visible focus states exist.
- [ ] The screen has at least one intentional, memorable signature element.
