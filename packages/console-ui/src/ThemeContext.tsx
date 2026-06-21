import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark

export const useIsDark = () => useContext(ThemeContext)

// Primary text colour for raw (non-antd) elements, so they're readable in both
// themes (a bare <div> otherwise defaults to black, invisible in dark mode).
export const textColor = (isDark: boolean) => (isDark ? 'rgba(255,255,255,0.85)' : 'rgba(0,0,0,0.88)')
