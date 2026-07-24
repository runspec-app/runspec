import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark

export const useIsDark = () => useContext(ThemeContext)

// Primary text colour for raw (non-antd) elements, so they're readable in both
// themes (a bare <div> otherwise defaults to black, invisible in dark mode).
export const textColor = (isDark: boolean) => (isDark ? 'rgba(255,255,255,0.85)' : 'rgba(0,0,0,0.88)')

// The app's signature accent (sidebar title, active nav tab, links, arg names).
// A white-label accent_color overrides the built-in blue pair — App.tsx provides
// the resolved value so every view honours the brand, not just the shell chrome.
export const DEFAULT_ACCENT = { dark: '#4fc1ff', light: '#0958d9' }

export const AccentContext = createContext(DEFAULT_ACCENT.dark)

export const useAccent = () => useContext(AccentContext)

// UI (chrome) font — nav, labels, buttons, prose. Values, args, host names,
// logs and the command bar stay monospace via explicit fontFamily.
export const UI_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

/** 'rgba(r,g,b,a)' from a '#rgb'/'#rrggbb' hex — for accent-tinted hover fills. */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
  const n = parseInt(full, 16)
  if (full.length !== 6 || Number.isNaN(n)) return hex
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}
