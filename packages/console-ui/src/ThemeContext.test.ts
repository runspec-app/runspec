/**
 * ThemeContext design tokens: the surface / elevation scale is complete in both
 * themes, ordered lowest→highest in dark, the CSS mirror in index.css matches,
 * and the radius scale + UI font are what App.tsx feeds into Ant.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { surfaces, RADIUS, UI_FONT, MONO_FONT, withAlpha, type Surfaces } from './ThemeContext'

const HEX = /^#[0-9a-f]{6}$/i
const RGBA = /^rgba\(\d+,\d+,\d+,0?\.\d+\)$/
const luminance = (hex: string) => {
  const n = parseInt(hex.slice(1), 16)
  return ((n >> 16) & 255) * 0.2126 + ((n >> 8) & 255) * 0.7152 + (n & 255) * 0.0722
}

describe('surfaces()', () => {
  const dark = surfaces(true)
  const light = surfaces(false)
  const levels: Array<keyof Surfaces> = ['sunken', 'base', 'raised', 'overlay']

  it('returns distinct palettes per theme with every slot filled', () => {
    expect(dark).not.toEqual(light)
    for (const s of [dark, light]) {
      for (const k of [...levels, 'border', 'borderStrong'] as const) expect(s[k]).toMatch(HEX)
      expect(s.divider).toMatch(RGBA)
      for (const k of ['shadowSm', 'shadowMd', 'shadowLg'] as const) expect(s[k]).toMatch(/^0 \d+px \d+px rgba\(/)
    }
  })

  it('dark surfaces get lighter as they rise (sunken < base < raised < overlay)', () => {
    const lum = levels.map(k => luminance(dark[k]))
    for (let i = 1; i < lum.length; i++) expect(lum[i]).toBeGreaterThan(lum[i - 1])
    expect(luminance(dark.borderStrong)).toBeGreaterThan(luminance(dark.border))
  })

  it('light shell is darker than its cards, and borders are darker than the cards', () => {
    expect(luminance(light.sunken)).toBeLessThan(luminance(light.base))
    expect(luminance(light.base)).toBeLessThan(luminance(light.raised))
    expect(luminance(light.borderStrong)).toBeLessThan(luminance(light.border))
  })

  it('is mirrored 1:1 by the --rs-* vars in index.css (first paint + CSS-only chrome)', () => {
    const css = readFileSync(resolve(__dirname, 'index.css'), 'utf8')
    const root = css.slice(css.indexOf(':root {'), css.indexOf("html[data-theme='light']"))
    const lightBlock = css.slice(css.indexOf("html[data-theme='light'] {"))
    const pairs: Array<[keyof Surfaces, string]> = [
      ['sunken', '--rs-surface-sunken'], ['base', '--rs-surface-base'],
      ['raised', '--rs-surface-raised'], ['overlay', '--rs-surface-overlay'],
      ['border', '--rs-border'], ['borderStrong', '--rs-border-strong'], ['divider', '--rs-divider'],
      ['shadowSm', '--rs-shadow-sm'], ['shadowMd', '--rs-shadow-md'], ['shadowLg', '--rs-shadow-lg'],
    ]
    const norm = (v: string) => v.replace(/\s+/g, '')
    for (const [key, cssVar] of pairs) {
      const inRoot = root.match(new RegExp(`${cssVar}:\\s*([^;]+);`))
      const inLight = lightBlock.match(new RegExp(`${cssVar}:\\s*([^;]+);`))
      expect(inRoot, `${cssVar} in :root`).not.toBeNull()
      expect(inLight, `${cssVar} in light`).not.toBeNull()
      expect(norm(inRoot![1])).toBe(norm(dark[key]))
      expect(norm(inLight![1])).toBe(norm(light[key]))
    }
    for (const [k, px] of [['sm', RADIUS.sm], ['md', RADIUS.md], ['lg', RADIUS.lg]] as const) {
      expect(root).toContain(`--rs-radius-${k}: ${px}px;`)
    }
  })
})

describe('typography + radius tokens', () => {
  it('RADIUS is a small ascending scale', () => {
    expect(RADIUS.sm).toBeLessThan(RADIUS.md)
    expect(RADIUS.md).toBeLessThan(RADIUS.lg)
  })

  it('UI_FONT leads with the bundled Inter and keeps a system fallback', () => {
    expect(UI_FONT.startsWith("'Inter Variable'")).toBe(true)
    expect(UI_FONT).toContain('system-ui')
    expect(UI_FONT.trim().endsWith('sans-serif')).toBe(true)
  })

  it('MONO_FONT leads with the bundled JetBrains Mono and ends in the generic keyword', () => {
    expect(MONO_FONT.startsWith("'JetBrains Mono Variable'")).toBe(true)
    expect(MONO_FONT.trim().endsWith('monospace')).toBe(true)
  })

  it('both bundled fonts are declared with @font-face and shipped in public/fonts', () => {
    const css = readFileSync(resolve(__dirname, 'index.css'), 'utf8')
    for (const family of ['Inter Variable', 'JetBrains Mono Variable']) {
      const face = css.match(new RegExp(`@font-face\\s*\\{[^}]*font-family:\\s*'${family}'[^}]*\\}`))
      expect(face, family).not.toBeNull()
      const src = face![0].match(/url\('([^']+\.woff2)'\)/)?.[1]
      expect(src).toBeDefined()
      expect(() => readFileSync(resolve(__dirname, '..', 'public', src!.replace(/^\//, '')))).not.toThrow()
    }
  })

  it('no view or component hardcodes the bare monospace keyword any more', () => {
    const { readdirSync, statSync } = require('node:fs') as typeof import('node:fs')
    const walk = (dir: string): string[] => readdirSync(dir).flatMap(n => {
      const p = resolve(dir, n)
      return statSync(p).isDirectory() ? walk(p) : p.endsWith('.tsx') && !p.includes('.test.') ? [p] : []
    })
    const offenders = walk(resolve(__dirname)).filter(p => /fontFamily: ['"]monospace['"]/.test(readFileSync(p, 'utf8')))
    expect(offenders).toEqual([])
  })
})

describe('withAlpha()', () => {
  it('expands 3- and 6-digit hex to rgba and passes anything else through', () => {
    expect(withAlpha('#fff', 0.5)).toBe('rgba(255, 255, 255, 0.5)')
    expect(withAlpha('#4fc1ff', 0.1)).toBe('rgba(79, 193, 255, 0.1)')
    expect(withAlpha('red', 0.1)).toBe('red')
  })
})
