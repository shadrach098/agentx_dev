# agentx_dev — brand identity

## Positioning

A production-grade Python framework for building LLM agents.

- **Category** — developer tool / open-source Python framework
- **Audience** — Python engineers who value maintainability, security,
  and precise scope
- **Trust anchor** — MIT license, security-hardened, solo-authored but
  studio-grade
- **Cultural stance** — anti-template, editorial, dark-native,
  developer-first

## The three-word test

If someone reads three words about the brand, they should be:

> Nothing sprawls. Precise.

## Words we do not use

- **"small"** or **"lightweight"** — read as diminutive. The framework
  is deliberately scoped; that is a strength, not a smallness. Use
  "focused" / "precise" / "production-grade" instead.
- **"LangChain"** — never in tagline copy. Defining the brand by
  contrast against a competitor makes the brand look reactive. Let
  the mark and the surface do the differentiating.
- **"AI-powered"** / **"revolutionary"** / **"next-generation"** —
  marketing-speak. The framework speaks for itself in code.

## Voice

- Direct
- Terse
- Confident without swagger
- Uses monospace vocabulary (`AgentRunner`, `bind_tools_natively`)
- Never marketing-speak

**Do write:**
- "A small framework."
- "Nothing sprawls."
- "Every call is scoped."

**Never write:**
- "Empower your team..."
- "AI-powered next-generation..."
- "Revolutionary orchestration..."

## Core metaphor

**The terminal prompt.**

The exact moment right before you build an agent: cursor blinking,
waiting for your next command. That's what the framework *is* — a
runtime that gives structure to that moment.

## The mark

```
>|
```

Two glyphs. A chevron prompt followed by a cursor bar. Dispatch meets
ready. That's the whole story of an agent invocation.

**Files:**
- `logo/mark.svg` — lime, on transparent
- `logo/mark-mono.svg` — currentColor, single-color contexts
- `logo/app-icon.svg` — rounded charcoal square for OS icons (1024×1024)
- `logo/wordmark.svg` — text + animated cursor
- `logo/logo-full.svg` — mark + wordmark lockup

**Construction:**
- Chevron path: `M 6 8 L 16 16 L 6 24` inside a 32×32 viewBox
- Cursor: 3×16 rectangle at `x=21, y=8`, `rx=0.5`
- Stroke width: 3, rounded caps + joins
- The chevron and cursor share the same vertical extent (y=8 to y=24)
  so they visually rhyme

**Rules:**
- Never rotate the mark
- Never colorize the chevron and cursor differently
- Minimum clear space: 2 mark-width units on every side
- Minimum size: 20×20 px (below that, use only the cursor bar)
- Never place on a lime background (contrast collapses)

## Palette

| Role | Hex | Use |
|---|---|---|
| Charcoal | `#0A0A0B` | Base surface |
| Elevated | `#111114` | Cards, hover surfaces |
| Border | `#1F1F24` | Hairline dividers |
| Text | `#EEEEF0` | Primary text |
| Text dim | `#8B8B93` | Secondary text |
| Text mute | `#5A5A62` | Metadata, timestamps |
| **Accent lime** | **`#B8FF3E`** | Active states, hero underscore, mark |
| Accent glow | `rgba(184,255,62,0.15)` | Hover halos |

**Rule of one accent:** the lime carries the entire system. Never
introduce a second accent color without deliberate cause.

## Typography

- **JetBrains Mono** — wordmark, headings, all UI chrome text
  - The framework is code; the mark is code-shaped
- **Inter** — body prose (readable at 15px, cool letterforms)
- **No third face.** Ever.

Scale (from `docs/style.css`):

| Level | Size | Face | Weight |
|---|---|---|---|
| Hero | `clamp(40px, 6vw, 68px)` | JetBrains Mono | 500 |
| h1 | 34px | JetBrains Mono | 500 |
| h2 | 20px | JetBrains Mono | 500 |
| h3 | 16px | JetBrains Mono | 500 |
| h4 | 14px | Inter | 600 (uppercase) |
| Body | 15px | Inter | 400 |
| Small | 12–13px | Inter | 400 |
| Code | 13px | JetBrains Mono | 400 |

## Motion signature

- **The blinking cursor** — 1s steps(2) animation, permanent, on the
  brand's live surfaces (docs, README embed, splash)
- Everything else: brief, deliberate, always respects
  `prefers-reduced-motion`

## Tagline options

Ranked by strength:

1. **"Nothing sprawls."** — the primary signature line
2. **"A production-grade Python framework for building LLM agents."** — the descriptive long-form
3. **"One prompt. One cursor. Yours."** — for hero surfaces where the mark carries meaning
4. **"Precise agents. Python-native."** — for comparison surfaces (pricing, PyPI listing)

Use #1 for badges, favicons, social banners, and short surfaces.
Use #2 anywhere a first-time visitor needs to know what the project is.

## What the brand is NOT

- Not corporate SaaS
- Not "AI-powered anything"
- Not a template
- Not colorful
- Not friendly-cute
- Not decorative
- Not mysterious / cyberpunk / hacker aesthetic (we're editorial, not
  Mr Robot)

## Brand kit deck

See `brand/index.html` — a rendered one-page brand deck showing every
touchpoint. Open in a browser to review; screenshot to share.
