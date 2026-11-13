/**
 * ThemeContext design tokens: the surface / elevation scale is complete in both
 * themes, ordered lowest→highest in dark, the CSS mirror in index.css matches,
 * and the radius scale + UI font are what App.tsx feeds into Ant.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import {
  readableAccent, contrastRatio,
  surfaces, RADIUS, UI_FONT, MONO_FONT, FONT_SIZE, withAlpha, type Surfaces,
  textSecondary, textMuted, textFaint, textColor, DEFAULT_ACCENT,
  statusText, dangerColors, approveColors, hueSat,
  inkColors,
} from './ThemeContext'

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

// WCAG 2 relative luminance + contrast ratio (sRGB, '#rrggbb').
const relLum = (hex: string) => {
  const n = parseInt(hex.slice(1), 16)
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
}
const contrast = (a: string, b: string) => {
  const [hi, lo] = [relLum(a), relLum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

describe('muted-text contrast floor', () => {
  const levels = ['sunken', 'base', 'raised', 'overlay'] as const
  for (const isDark of [true, false]) {
    const theme = isDark ? 'dark' : 'light'
    const sf = surfaces(isDark)
    it(`${theme}: secondary and muted text reach 4.5:1 on every surface`, () => {
      for (const lvl of levels) {
        expect(contrast(textSecondary(isDark), sf[lvl]), `secondary on ${lvl}`).toBeGreaterThanOrEqual(4.5)
        expect(contrast(textMuted(isDark), sf[lvl]), `muted on ${lvl}`).toBeGreaterThanOrEqual(4.5)
      }
    })
    it(`${theme}: faint marks reach 3:1 on every surface`, () => {
      for (const lvl of levels) {
        expect(contrast(textFaint(isDark), sf[lvl]), `faint on ${lvl}`).toBeGreaterThanOrEqual(3)
      }
    })
    it(`${theme}: primary text and the default accent read as text on every surface`, () => {
      const accent = isDark ? DEFAULT_ACCENT.dark : DEFAULT_ACCENT.light
      for (const lvl of levels) {
        expect(contrast(textColor(isDark), sf[lvl]), `text on ${lvl}`).toBeGreaterThanOrEqual(7)
        expect(contrast(accent, sf[lvl]), `accent on ${lvl}`).toBeGreaterThanOrEqual(4.5)
      }
    })
    it(`${theme}: the hierarchy steps down (secondary > muted > faint)`, () => {
      const c = (hex: string) => contrast(hex, sf.base)
      expect(c(textSecondary(isDark))).toBeGreaterThan(c(textMuted(isDark)))
      expect(c(textMuted(isDark))).toBeGreaterThan(c(textFaint(isDark)))
    })
  }

  it('index.css mirrors the text tokens for first paint', () => {
    const css = readFileSync(resolve(__dirname, 'index.css'), 'utf8')
    const root = css.slice(css.indexOf(':root {'), css.indexOf("html[data-theme='light']"))
    const lightBlock = css.slice(css.indexOf("html[data-theme='light'] {"))
    for (const [fn, v] of [[textSecondary, 'secondary'], [textMuted, 'muted'], [textFaint, 'faint']] as const) {
      expect(root).toContain(`--rs-text-${v}: ${fn(true)};`)
      expect(lightBlock).toContain(`--rs-text-${v}: ${fn(false)};`)
    }
  })
})

describe('type scale', () => {
  const scale = new Set<number>(Object.values(FONT_SIZE))

  it('FONT_SIZE ascends and starts at an 11px floor', () => {
    const steps = Object.values(FONT_SIZE)
    expect(steps[0]).toBe(11)
    for (let i = 1; i < steps.length; i++) expect(steps[i]).toBeGreaterThan(steps[i - 1])
  })

  it('every inline text fontSize in a view or component is a step on the scale', () => {
    const { readdirSync, statSync } = require('node:fs') as typeof import('node:fs')
    const walk = (dir: string): string[] => readdirSync(dir).flatMap(n => {
      const p = resolve(dir, n)
      return statSync(p).isDirectory() ? walk(p) : p.endsWith('.tsx') && !p.includes('.test.') ? [p] : []
    })
    // Icon glyphs (antd icons, the ● status dot) are drawings and size freely.
    const isIcon = (line: string) => /(Outlined|Filled|TwoTone)\b|●/.test(line)
    const offenders: string[] = []
    for (const p of walk(resolve(__dirname))) {
      readFileSync(p, 'utf8').split('\n').forEach((line, i) => {
        if (isIcon(line)) return
        for (const m of line.matchAll(/fontSize: (\d+(?:\.\d+)?)\b/g)) {
          if (!scale.has(Number(m[1]))) offenders.push(`${p.slice(__dirname.length + 1)}:${i + 1} fontSize ${m[1]}`)
        }
      })
    }
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

describe('readableAccent()', () => {
  const levels = ['sunken', 'base', 'raised', 'overlay'] as const
  it('leaves the default accents alone', () => {
    expect(readableAccent(DEFAULT_ACCENT.dark, true)).toBe(DEFAULT_ACCENT.dark)
    expect(readableAccent(DEFAULT_ACCENT.light, false)).toBe(DEFAULT_ACCENT.light)
  })
  for (const brand of ['#b3001b', '#1d4ed8', '#7c3aed', '#ffd400', '#00a3e0']) {
    for (const isDark of [true, false]) {
      it(`${brand} as text reaches 4.5:1 on every ${isDark ? 'dark' : 'light'} surface`, () => {
        const c = readableAccent(brand, isDark)
        for (const lvl of levels) expect(contrastRatio(c, surfaces(isDark)[lvl]), `${c} on ${lvl}`).toBeGreaterThanOrEqual(4.5)
      })
    }
  }
  it('keeps the hue: a deep red stays red on dark', () => {
    const c = readableAccent('#b3001b', true)
    const n = parseInt(c.slice(1), 16)
    expect((n >> 16) & 255).toBeGreaterThan(((n >> 8) & 255) + 40)
  })
  it('passes through what it cannot parse', () => {
    expect(readableAccent('teal', true)).toBe('teal')
  })
})

describe('ink primary buttons', () => {
  for (const isDark of [true, false]) {
    it(`text reads on every state, and the fill stands out from every surface (${isDark ? 'dark' : 'light'})`, () => {
      const ink = inkColors(isDark)
      for (const fill of [ink.bg, ink.hover, ink.active]) expect(contrastRatio(ink.text, fill)).toBeGreaterThanOrEqual(4.5)
      const sf = surfaces(isDark)
      for (const base of [sf.sunken, sf.base, sf.raised, sf.overlay]) expect(contrastRatio(ink.bg, base)).toBeGreaterThanOrEqual(3)
    })
  }
})

describe('status, danger and approve colours', () => {
  const levels = ['sunken', 'base', 'raised', 'overlay'] as const
  for (const isDark of [true, false]) {
    const theme = isDark ? 'dark' : 'light'
    it(`status text reads at 4.5:1 on every ${theme} surface`, () => {
      for (const [k, c] of Object.entries(statusText(isDark)))
        for (const lvl of levels) expect(contrastRatio(c, surfaces(isDark)[lvl]), `${k} ${c} on ${lvl}`).toBeGreaterThanOrEqual(4.5)
    })
    for (const accent of [undefined, '#0b7a6e', '#9b031a', '#e11d48']) {
      it(`danger for ${accent ?? 'no brand'} reads on every ${theme} surface and on its fill`, () => {
        const d = dangerColors(accent, isDark)
        for (const lvl of levels) expect(contrastRatio(d.text, surfaces(isDark)[lvl])).toBeGreaterThanOrEqual(4.5)
        expect(contrastRatio(d.onFill, d.text)).toBeGreaterThanOrEqual(4.5)
      })
    }
    it(`Approve text reads on its ${theme} fill`, () => {
      const a = approveColors(isDark)
      expect(contrastRatio(a.text, a.bg)).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio(a.text, a.hover)).toBeGreaterThanOrEqual(4.5)
    })
  }
  it('moves danger off red when the brand is red, and only then', () => {
    expect(dangerColors(undefined, true).text).toBe(dangerColors('#0b7a6e', true).text)
    expect(dangerColors('#9b031a', true).text).not.toBe(dangerColors(undefined, true).text)
    expect(dangerColors('#9b031a', false).text).not.toBe(dangerColors(undefined, false).text)
    expect(hueSat(dangerColors('#9b031a', true).text).hue).toBeGreaterThan(300)
  })
})
