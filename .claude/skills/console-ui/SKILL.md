---
description: Build or edit a runspec-console UI view/component so it comes out native to the codebase — Ant Design 5 + React 19, the ThemeContext colour hierarchy (dark/light + white-label accent), the typed `bridge` boundary to the Python backend, and a colocated vitest test. Use whenever adding a tab/view, a Settings drawer section, a modal, or editing anything under packages/console-ui/src.
---

Work on the runspec-console frontend: **$ARGUMENTS**

The console UI (`packages/console-ui/`) is a mature, opinionated codebase with no
CLAUDE.md of its own. This skill is the missing conventions doc. It states the
rules and **points you at the code that is the source of truth** — read those
files for the live values rather than trusting any numbers restated here (the
theme changes; white-label work is active).

## First: read the source of truth (don't skip this)

Before writing a line, read these — they *are* the design system:

1. **`src/ThemeContext.tsx`** — the entire colour + font contract. Every helper
   you'll need is here: `useIsDark()`, `textColor(isDark)`, the muted hierarchy
   `textSecondary`/`textMuted`/`textFaint`, `useAccent()` + `DEFAULT_ACCENT`,
   `UI_FONT`, and `withAlpha(hex, a)`. It's short — read all of it.
2. **`src/index.css`** — the `--rs-*` CSS variables (`--rs-accent`, `--rs-hover`,
   scrollbar + muted-text vars) and the light-theme overrides under
   `html[data-theme='light']`. Use these vars for anything CSS-driven (hover
   states, borders) instead of hardcoding colours.
3. **`src/bridge/index.ts`** — the typed boundary to the Python backend. All the
   shared interfaces (`Runnable`, `ArgDef`, `Host`, `HistoryRecord`, …) and the
   `bridge` object every view calls live here. **The frontend never talks to the
   backend except through `bridge`.**
4. **The nearest sibling** to what you're building — an existing view in
   `src/views/` or component in `src/components/`. Match it. `RotaView.tsx` is a
   good compact reference (state + bridge calls + theme helpers + a live poll).
   `SettingsDrawer.tsx` is the pattern for a Settings tab.

## The rules

**Stack (don't reach outside it).** React 19 + TypeScript, **Ant Design 5**
(`antd`) for every control (`Button`, `Input`, `Switch`, `Segmented`, `Collapse`,
`Table`, `message`, `Modal`, …) and `@ant-design/icons` for icons. Charts use
`@ant-design/plots`; markdown uses `react-markdown` + `remark-gfm`/`remark-breaks`
(see `src/components/Markdown.tsx`). `dayjs` for dates (antd's `DatePicker` speaks
dayjs). **Do not add a dependency** without asking — check `package.json` first;
the set is deliberately small.

**Theming is mandatory, both directions.** The app renders in dark *and* light
and honours a white-label `accent_color`. So:
- Never hardcode a text colour on a raw element. Call `useIsDark()` and pass it
  through `textColor` / `textSecondary` / `textMuted` / `textFaint`. A bare
  `<div>` with no colour defaults to black — invisible in dark mode.
- Use antd components where possible; they theme themselves via the
  `ConfigProvider` token set in `App.tsx`.
- For the brand accent (active nav, links, arg names, accent-tinted hovers) use
  `useAccent()` in TSX, or `var(--rs-accent)` in CSS — **never** a literal blue.
  Tinted fills come from `withAlpha(accent, 0.x)`.
- CSS-only affordances (row hover, resize handles) belong in `index.css` using
  the `--rs-*` vars, so they don't trigger React re-renders — follow the existing
  `.rs-host-row` / `.rs-sidebar-resize` examples.
- Chrome text uses `UI_FONT`; values, host names, args, logs, and the command bar
  stay **monospace** (set `fontFamily` explicitly, as siblings do).

**The bridge boundary.** Add any new backend call as a typed method on the
`BridgeApi` interface in `src/bridge/index.ts`, and add its mock to
`src/bridge/mock.ts` (dev mode + tests run against the mock — a method missing
there breaks `npm run dev`). Backend method names are **snake_case** (they mirror
the Python `Bridge` public methods, e.g. `save_rota`, `screen_state`); this JS
API surface is frozen on the Python side by `tests/test_bridge_api_surface.py`,
so a rename is a two-sided change.

**Every view/component gets a colocated test.** Name it `Thing.test.tsx` (or
`.test.ts` for pure logic) next to the source. Tests run under vitest + jsdom via
`@testing-library/react`. Mock the bridge with the `vi.hoisted` + `Proxy` pattern
you'll see in `src/views/RotaView.test.tsx` — copy it. `src/test/setup.ts`
already polyfills `matchMedia`/`ResizeObserver` for antd; don't re-stub them.

**Layout.** Views live in `src/views/`, reusable pieces in `src/components/`,
shared run-dialog bits in `src/components/runShared.tsx`. A new top-level tab is
wired in `App.tsx` (nav + routing) and reads its data through `bridge`. Keep the
file focused; several views split sub-components into the same file (see
`RotaView`'s `LockIndicator` / `BreakControl`).

## Then: verify before you're done

From `packages/console-ui/`:

```bash
npm run typecheck   # tsc --noEmit — must pass; this is a strict TS project
npm test            # vitest run — your new test included
```

Both must be green. If you touched a `bridge` method name, confirm the Python
side (`tests/test_bridge_api_surface.py`) still agrees, or the desktop app breaks
at runtime even though the frontend compiles.

## Don't

- Don't hardcode colours, blues, or theme-specific values — go through
  `ThemeContext` helpers / `--rs-*` vars.
- Don't call the backend from anywhere but `bridge`, or add a bridge method
  without its `mock.ts` entry.
- Don't add a UI library or component kit alongside antd.
- Don't ship a view without a colocated test and a passing `typecheck`.
- Don't restate the palette from memory — read `ThemeContext.tsx` / `index.css`.
