/**
 * Colour discipline: status and surface colours come from the theme
 * (ThemeContext.tsx tokens, kit StatusTag / StatusIcon), never from Ant's
 * preset tag or badge colours (`<Tag color="red">`) or hex / rgb() literals
 * in a view. Those
 * ignore dark/light and the white-label accent, and they drifted in every
 * window that wasn't on the kit.
 *
 * This is a ratchet: files that still have some are listed in BASELINE with
 * their count. A new file may have none, and a listed file may not grow. When
 * you remove some, lower its number (the test fails until you do, so the
 * baseline never hides a regression later).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = __dirname

// Files whose job is to define colours.
const EXEMPT = new Set([
  'ThemeContext.tsx',      // the tokens themselves
  'bridge/mock.ts',        // dev mock data (brand colours, sample themes)
  'terminal_look.ts',
  'components/TerminalLook.tsx', // PuTTY colour schemes, previewed as-is
  'views/knowledgeGraph.ts',     // the graph's categorical palette
  'diagramRender.ts',            // Mermaid's canvas theme
])

// Remaining offenders, by file: Tag/Badge presets, hex and rgb() literals.
// Burn these down.
const BASELINE: Record<string, number> = {
  'components/ErrorBoundary.tsx': 7,  // var() fallbacks: it renders when the theme may be gone
  'App.tsx': 9,
  'components/CommandInput.tsx': 13,  // syntax-highlight palette
  'components/ConfigSyncPreview.tsx': 9,
  'components/CredentialGuide.tsx': 3,
  'components/HelpdeskPanel.tsx': 3,
  'components/LogStream.tsx': 8,
  'components/Markdown.tsx': 4,
  'components/OtherHostsSection.tsx': 4,
  'components/OutputPanel.tsx': 12,  // incl. a #000 alpha mask (a gradient fade, not a colour)
  'components/SettingsDrawer.tsx': 9,
  'components/runShared.tsx': 7,
  'views/DevView.tsx': 9,
  'views/EvalsView.tsx': 1,
  'views/HistoryView.tsx': 3,
  'views/HostScheduleModal.tsx': 1,
  'views/HostsView.tsx': 5,
  'views/ImproveView.tsx': 1,
  'views/KnowledgeCheck.tsx': 1,
  'views/KnowledgeEdits.tsx': 2,
  'views/KnowledgeTools.tsx': 4,
  'views/KnowledgeView.tsx': 2,
  'views/MarketplaceView.tsx': 1,
  'views/PathInput.tsx': 3,
  'views/RunnableAction.tsx': 2,
  'views/RunnablesView.tsx': 4,
  'views/files/FilePane.tsx': 1,
}

const TAG_PRESET = /<(?:Tag|Badge)\b[^>]*\bcolor=/g
// Any hex colour in code, not only a string that is nothing but one: it also
// hides in '1px solid #2a2a2a', color-mix(…) and var(--x, #hex) fallbacks.
// Comment lines don't count.
const HEX = /#[0-9a-fA-F]{3,8}(?![0-9a-zA-Z_-])/g
// A literal rgb()/rgba() colour in a string (not a var() fallback).
const RGB = /['"`]rgba?\(\s*\d/g

function files(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) return files(p)
    return /\.(tsx?|jsx?)$/.test(name) && !/\.test\./.test(name) ? [p] : []
  })
}

function codeOnly(text: string): string {
  return text.split('\n').filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n')
}

export function offences(text: string): number {
  const code = codeOnly(text)
  return (code.match(TAG_PRESET)?.length ?? 0) + (code.match(HEX)?.length ?? 0) + (code.match(RGB)?.length ?? 0)
}

describe('colour discipline', () => {
  const counts = Object.fromEntries(
    files(SRC)
      .map(p => [relative(SRC, p).replace(/\\/g, '/'), offences(readFileSync(p, 'utf8'))] as const)
      .filter(([rel, n]) => n > 0 && !EXEMPT.has(rel)),
  )

  it('counts Tag presets and hex literals', () => {
    expect(offences(`<Tag color="red">x</Tag> <Tag>y</Tag> style={{ color: '#52c41a' }} "#fff"`)).toBe(3)
    expect(offences(`<Badge color="orange" /> background: 'rgba(82,196,26,0.16)' 'var(--x, rgba(0,0,0,.1))'`)).toBe(2)
    // Hex inside a longer string, and in a var() fallback; not in comments or ids.
    expect(offences(`border: '1px solid #2a2a2a', c: 'var(--x, #52c41a)'\n// was #fff\nhref="#send-bar"`)).toBe(2)
  })

  it('no file adds a raw colour (use StatusTag, StatusIcon or a theme token)', () => {
    const grew = Object.entries(counts)
      .filter(([rel, n]) => n > (BASELINE[rel] ?? 0))
      .map(([rel, n]) => `${rel}: ${n} (allowed ${BASELINE[rel] ?? 0})`)
    expect(grew).toEqual([])
  })

  it('the baseline is kept tight: lower a number when you remove some', () => {
    const loose = Object.entries(BASELINE)
      .filter(([rel, n]) => (counts[rel] ?? 0) < n)
      .map(([rel, n]) => `${rel}: now ${counts[rel] ?? 0}, baseline ${n}`)
    expect(loose).toEqual([])
  })
})
