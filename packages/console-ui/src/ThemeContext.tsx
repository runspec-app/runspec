import { createContext, useContext } from 'react'

export const ThemeContext = createContext(true) // true = dark

export const useIsDark = () => useContext(ThemeContext)
