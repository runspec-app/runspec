import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark

export const useIsDark = () => useContext(ThemeContext)

// Primary text colour for raw (non-antd) elements, so they're readable in both
// themes (a bare <div> otherwise defaults to black, invisible in dark mode).
export const textColor = (isDark: boolean) => (isDark ? 'rgba(255,255,255,0.85)' : 'rgba(0,0,0,0.88)')

// Muted-text hierarchy (dark / light) — one canonical pair per role, so
// identical-purpose text renders identically across tabs and the whole
// hierarchy is tunable here instead of at ~150 inline call sites.
export const textSecondary = (isDark: boolean) => (isDark ? '#aaa' : '#666')  // readable secondary: labels, descriptions
export const textMuted = (isDark: boolean) => (isDark ? '#888' : '#999')      // dim: hints, separators, muted icons, counts
export const textFaint = (isDark: boolean) => (isDark ? '#555' : '#bbb')      // faintest: placeholders, carets, off-state icons

// The app's signature accent (sidebar title, active nav tab, links, arg names).
// A white-label accent_color overrides the built-in blue pair — App.tsx provides
// the resolved value so every view honours the brand, not just the shell chrome.
export const DEFAULT_ACCENT = { dark: '#4fc1ff', light: '#0958d9' }

export const AccentContext = createContext(DEFAULT_ACCENT.dark)

export const useAccent = () => useContext(AccentContext)

// UI (chrome) font — nav, labels, buttons, prose. Values, args, host names,
// logs and the command bar stay monospace via explicit fontFamily.
// 'Inter Variable' is bundled (public/fonts, @font-face in index.css) so the
// chrome renders identically on every machine; the system stack is the fallback
// for the first paint and any engine that can't load the woff2.
export const UI_FONT = "'Inter Variable', Inter, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

// Value font — args, host names, runnable names, logs, code, the command bar.
// 'JetBrains Mono Variable' is bundled the same way (public/fonts + @font-face
// in index.css); the fallbacks are the good system monos, ending in the generic
// keyword. Every inline `fontFamily: MONO_FONT` used to be the bare `monospace`
// keyword, which maps to Courier/DejaVu on many machines.
export const MONO_FONT = "'JetBrains Mono Variable', 'Cascadia Mono', 'SF Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace"

// One corner radius scale for the whole app — pass these (or the matching Ant
// tokens, which App.tsx sets from them) instead of ad-hoc 2/3/4/6/8/14 values.
export const RADIUS = { sm: 6, md: 8, lg: 12 } as const

// Surface + elevation scale — the depth model. Four surface levels, lowest to
// highest, plus the borders and shadows that separate them:
//   sunken  → app shell (sidebar, tab bar, footer, code wells)
//   base    → content area behind everything
//   raised  → cards, inputs, tables, panels sitting on the content
//   overlay → popovers, dropdowns, modals, drawers floating above it all
// Dark surfaces are a cool near-black (not pure neutral) so any white-label
// accent sits on it cleanly; light is a soft grey with white cards. App.tsx maps
// these onto Ant's colorBgLayout / colorBgContainer / colorBgElevated so Ant
// controls and raw elements share one scale; index.css mirrors them as
// --rs-surface-* / --rs-border* / --rs-shadow-* vars (keep in sync).
export interface Surfaces {
  sunken: string; base: string; raised: string; overlay: string
  border: string; borderStrong: string; divider: string
  shadowSm: string; shadowMd: string; shadowLg: string
}
const DARK_SURFACES: Surfaces = {
  sunken: '#0b0d10', base: '#101216', raised: '#171a1f', overlay: '#1e2228',
  border: '#252a31', borderStrong: '#323844', divider: 'rgba(255,255,255,0.06)',
  shadowSm: '0 1px 2px rgba(0,0,0,0.45)',
  shadowMd: '0 4px 14px rgba(0,0,0,0.5)',
  shadowLg: '0 16px 40px rgba(0,0,0,0.6)',
}
const LIGHT_SURFACES: Surfaces = {
  sunken: '#f0f2f5', base: '#f7f8fa', raised: '#ffffff', overlay: '#ffffff',
  border: '#e5e8ec', borderStrong: '#d3d8df', divider: 'rgba(0,0,0,0.06)',
  shadowSm: '0 1px 2px rgba(16,24,40,0.06)',
  shadowMd: '0 4px 14px rgba(16,24,40,0.08)',
  shadowLg: '0 16px 40px rgba(16,24,40,0.14)',
}
export const surfaces = (isDark: boolean): Surfaces => (isDark ? DARK_SURFACES : LIGHT_SURFACES)

/** 'rgba(r,g,b,a)' from a '#rgb'/'#rrggbb' hex — for accent-tinted hover fills. */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
  const n = parseInt(full, 16)
  if (full.length !== 6 || Number.isNaN(n)) return hex
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}
