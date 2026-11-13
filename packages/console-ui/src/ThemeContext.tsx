import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark

export const useIsDark = () => useContext(ThemeContext)

// Primary text colour for raw (non-antd) elements, so they're readable in both
// themes (a bare <div> otherwise defaults to black, invisible in dark mode).
export const textColor = (isDark: boolean) => (isDark ? '#e8ebef' : '#1b2126')

// Muted-text hierarchy (dark / light) — one canonical pair per role, so
// identical-purpose text renders identically across tabs and the whole
// hierarchy is tunable here instead of at ~150 inline call sites.
//
// Contrast floor (WCAG AA, checked against every surface level in
// ThemeContext.test.ts): secondary and muted are TEXT, so >= 4.5:1; faint is for
// marks that carry no information on their own (an empty-cell dash, a caret, an
// off-state icon), so >= 3:1. Text that says something (an empty-state message,
// a hint) uses muted, never faint.
export const textSecondary = (isDark: boolean) => (isDark ? '#c3ccd2' : '#3d454c')  // readable secondary: labels, descriptions
export const textMuted = (isDark: boolean) => (isDark ? '#a9b3ba' : '#525b63')      // dim: hints, separators, muted icons, counts
export const textFaint = (isDark: boolean) => (isDark ? '#8a959d' : '#6e7780')      // faintest: empty-cell dashes, carets, off-state icons
// Small warning-toned text ("required" markers). Ant's colorWarning (#faad14) is
// ~1.9:1 on white, so light uses a deeper orange that clears 4.5:1.
export const textWarning = (isDark: boolean) => (isDark ? '#ffa940' : '#ad4e00')

// The app's signature accent (sidebar title, active nav tab, links, arg names).
// A white-label accent_color overrides the built-in runspec teal pair — App.tsx
// provides the resolved value so every view honours the brand, not just the
// shell chrome. See docs/design/brand.md. The light value is a step deeper
// than the button teal so accent TEXT (active tab, sidebar title, links) still
// clears 4.5:1 on the aluminium shell; filled buttons keep BRAND_PRIMARY.
export const DEFAULT_ACCENT = { dark: '#3cc7b6', light: '#08665c' }

// Ant's colorPrimary (filled buttons, switches, checkboxes) for an unbranded
// console. Darker than the dark-theme accent so white button text stays legible
// (5.2:1); a white-label accent_color replaces it.
export const BRAND_PRIMARY = '#0b7a6e'

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

// One type scale for the whole app — pick a step instead of an ad-hoc size.
// 11 is the floor for text (badges, counts, tag labels); 12 is secondary and
// meta text; 13 is the body of dense views (tables, cards, forms); 14 is Ant's
// base size and emphasised body; 16/18/20 are headings; 32 is the empty-state
// glyph. Icon glyphs (antd *Outlined icons, the ● status dot) size freely —
// they're drawings, not text. ThemeContext.test.ts fails on an inline text
// fontSize that isn't on this scale.
export const FONT_SIZE = { xs: 11, sm: 12, md: 13, lg: 14, xl: 16, xxl: 18, xxxl: 20, display: 32 } as const

// Surface + elevation scale — the depth model. Four surface levels, lowest to
// highest, plus the borders and shadows that separate them:
//   sunken  → app shell (sidebar, tab bar, footer, code wells)
//   base    → content area behind everything
//   raised  → cards, inputs, tables, panels sitting on the content
//   overlay → popovers, dropdowns, modals, drawers floating above it all
// Dark is gunmetal, a blue-grey lifted well off black so long sessions don't
// read as a void; light is satin aluminium, a machined grey with no pure white,
// so the shell isn't glaring. Both keep a slight cool bias so any white-label
// accent sits on them cleanly. App.tsx maps
// these onto Ant's colorBgLayout / colorBgContainer / colorBgElevated so Ant
// controls and raw elements share one scale; index.css mirrors them as
// --rs-surface-* / --rs-border* / --rs-shadow-* vars (keep in sync).
export interface Surfaces {
  sunken: string; base: string; raised: string; overlay: string
  border: string; borderStrong: string; divider: string
  shadowSm: string; shadowMd: string; shadowLg: string
}
const DARK_SURFACES: Surfaces = {
  sunken: '#1e2428', base: '#262e33', raised: '#2f383e', overlay: '#39434a',
  border: '#3b454c', borderStrong: '#4d5860', divider: 'rgba(255,255,255,0.07)',
  shadowSm: '0 1px 2px rgba(6,10,13,0.35)',
  shadowMd: '0 4px 14px rgba(6,10,13,0.4)',
  shadowLg: '0 16px 40px rgba(6,10,13,0.5)',
}
const LIGHT_SURFACES: Surfaces = {
  sunken: '#d4d9dd', base: '#e0e4e7', raised: '#eceff1', overlay: '#f5f6f7',
  border: '#c3c9ce', borderStrong: '#adb4ba', divider: 'rgba(20,30,40,0.08)',
  shadowSm: '0 1px 2px rgba(20,30,40,0.10)',
  shadowMd: '0 4px 14px rgba(20,30,40,0.12)',
  shadowLg: '0 16px 40px rgba(20,30,40,0.18)',
}
export const surfaces = (isDark: boolean): Surfaces => (isDark ? DARK_SURFACES : LIGHT_SURFACES)

const _hex6 = (hex: string): string | null => {
  const h = hex.trim().replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
  return /^[0-9a-fA-F]{6}$/.test(full) ? full.toLowerCase() : null
}
const _lum = (full: string) => {
  const n = parseInt(full, 16)
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
}
/** WCAG 2 contrast ratio between two '#rgb'/'#rrggbb' colours (1 when unparseable). */
export function contrastRatio(a: string, b: string): number {
  const x = _hex6(a), y = _hex6(b)
  if (!x || !y) return 1
  const [hi, lo] = [_lum(x), _lum(y)].sort((p, q) => q - p)
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * The accent as TEXT: the colour unchanged when it already reads at 4.5:1 on
 * every surface of the theme, else mixed toward white (dark) or black (light)
 * just far enough that it does. A white-label accent_color is chosen for fills
 * (buttons, the logo), and a deep brand red on gunmetal reads at ~2:1 — so
 * links, active tabs and highlighted text use this, while colorPrimary keeps
 * the brand colour as given.
 */
export function readableAccent(hex: string, isDark: boolean, min = 4.5): string {
  const full = _hex6(hex)
  if (!full) return hex
  const sf = surfaces(isDark)
  const bgs = [sf.sunken, sf.base, sf.raised, sf.overlay]
  const worst = (c: string) => Math.min(...bgs.map(bg => contrastRatio(c, bg)))
  if (worst('#' + full) >= min) return '#' + full
  const n = parseInt(full, 16)
  const rgb = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
  const target = isDark ? 255 : 0
  for (let t = 0.04; t <= 1.0001; t += 0.04) {
    const mixed = '#' + rgb.map(v => Math.round(v + (target - v) * t).toString(16).padStart(2, '0')).join('')
    if (worst(mixed) >= min) return mixed
  }
  return isDark ? '#ffffff' : '#000000'
}

/** 'rgba(r,g,b,a)' from a '#rgb'/'#rrggbb' hex — for accent-tinted hover fills. */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
  const n = parseInt(full, 16)
  if (full.length !== 6 || Number.isNaN(n)) return hex
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}
