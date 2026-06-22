# Objective Content Studio — Frontend

React 19 + Vite single-page app for the content team: fetch course hierarchies,
extract reading material, build the RAG index, and generate & review objective
(MCQ) assessment content.

## Run

```bash
npm install
npm run dev      # local dev server (Vite, HMR)
npm run build    # production build → dist/
npm run preview  # serve the production build
npm run lint     # eslint
```

The API base URL is configured in `src/api.js`.

## Design system — "Refined Violet"

A clean, professional workspace: true-neutral graphite surfaces, a single
confident **violet** primary (focus / active state), large radii and soft
layered depth. Status colors stay semantic (green / amber / red / cyan). Ships
with **dark** (default) and **light** themes, toggled from the sidebar and
persisted to `localStorage`.

Every visual decision derives from a CSS custom property — **never hardcode a
color, radius, or shadow in a component**. The tokens are the contract.

| Layer | What lives there |
|-------|------------------|
| `styles/tokens.css` | The single source of truth: palette (both themes), type scale, spacing, radii, shadows, motion, z-index, safe-area insets. |
| `styles/base.css`   | Element resets, base typography, scrollbars, focus rings, reduced-motion. |
| `styles/components/`| One stylesheet per surface. |
| `styles/index.css`  | Entry point — `@import`s tokens → base → components in cascade order. |

### Component stylesheets

```
styles/components/
  layout.css         shell, sidebar, mobile bar, topbar, stats, toolbar
  controls.css       buttons, form inputs, badges, segmented control
  courses.css        course cards, collapse, pipeline dots, prerequisites
  overlays.css       modal, wizard, ingest modal, activity drawer, toasts
  feedback.css       empty states, skeletons, tooltips, keyframes
  chat.css           chat page
  generation.css     generation studio
  mcq.css            MCQ generation, progress board, results, scope modal
  pipeline-page.css  MCQ pipeline page (stage map + editable prompts)
  responsive.css     cross-cutting breakpoints (surface-specific rules live
                     with their own component)
```

### Type

- **Plus Jakarta Sans** — display / headings / metrics (`--font-display`)
- **Inter** — body (`--font-sans`)
- **JetBrains Mono** — IDs, tokens, data (`--font-mono`)

Loaded in `index.html`.

## Responsive

Mobile-first behaviour, three breakpoints (`1024 / 860 / 560`px):

- **Desktop** — the sidebar collapses to an icon-only rail via the edge toggle
  (state persisted to `localStorage`); labels show as hover tooltips.
- **≤ 860px** — the sidebar becomes an off-canvas drawer opened from the mobile
  top bar (`MobileBar`), with a scrim. Page headers stack.
- **≤ 560px** — two-up stat grid, full-width search and actions, single-column
  reflow, ≥ 40px touch targets.
- Wide content (tables, logs, pipelines) scrolls inside its own container — the
  page body never scrolls horizontally.
- Safe-area insets are respected on notched phones (`viewport-fit=cover`).

## Conventions

- Components are function components in `src/components/`, one per file.
- Shared primitives (`Spinner`, `EmptyState`, `Skeleton`, `Segmented`,
  `EnvBadge`) live in `src/components/ui.jsx`.
- Class names are the contract between JSX and CSS — rename in both or neither.
