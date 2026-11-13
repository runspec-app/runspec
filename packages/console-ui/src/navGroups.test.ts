import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { layoutNavTabs, hostMatches, groupViews, parentOf, NAV_GROUPS, NAV_OVERFLOW, NAV_SECTIONS } from './navGroups'

const tabs = (...keys: string[]) => keys.map(key => ({ key }))
const keys = (ts: { key: string }[]) => ts.map(t => t.key)

describe('layoutNavTabs()', () => {
  it('groups the main tabs and folds the occasional ones into More', () => {
    const all = tabs('console', 'history', 'files', 'knowledge', 'quality', 'forms', 'marketplace', 'automation', 'dev')
    const { groups, overflow, ordered } = layoutNavTabs(all)
    expect(groups.map(keys)).toEqual([
      ['console', 'forms', 'history', 'files'],
      ['automation'],
      ['knowledge', 'marketplace'],
    ])
    expect(keys(overflow)).toEqual(['quality', 'dev'])
    expect(ordered).toHaveLength(all.length)
    expect(keys(ordered).slice(0, 4)).toEqual(['console', 'forms', 'history', 'files'])
  })

  it('drops groups emptied by the AI switch and keeps unknown tabs reachable', () => {
    const { groups, overflow } = layoutNavTabs(tabs('console', 'forms', 'automation', 'newthing'))
    expect(groups.map(keys)).toEqual([['console', 'forms'], ['automation']])
    expect(keys(overflow)).toEqual(['newthing'])
  })

  it('places every tab App.tsx defines', () => {
    const src = readFileSync(resolve(__dirname, 'App.tsx'), 'utf8')
    const block = src.slice(src.indexOf('const viewTabs'), src.indexOf('], features)'))
    const defined = [...block.matchAll(/key: '([a-z]+)'\s+as ViewKey/g)].map(m => m[1])
    expect(defined.length).toBeGreaterThan(5)
    const known = new Set([...NAV_GROUPS.flat(), ...NAV_OVERFLOW])
    // A view is placed through its tab (a section's parent, or itself).
    for (const k of defined) expect(known.has(parentOf(k)), k).toBe(true)
  })
})

describe('hostMatches()', () => {
  it('matches name or group, ignoring case and blank queries', () => {
    expect(hostMatches({ name: 'web-04', group: 'prod-web' }, 'WEB-0')).toBe(true)
    expect(hostMatches({ name: 'pg-1', group: 'prod-db' }, 'db')).toBe(true)
    expect(hostMatches({ name: 'pg-1' }, 'web')).toBe(false)
    expect(hostMatches({ name: 'pg-1' }, '  ')).toBe(true)
  })
})

describe('tab sections', () => {
  it('maps each view to its tab', () => {
    expect(parentOf('triggers')).toBe('automation')
    expect(parentOf('rota')).toBe('automation')
    expect(parentOf('analytics')).toBe('history')
    expect(parentOf('logs')).toBe('history')
    expect(parentOf('specs')).toBe('forms')
    expect(parentOf('evals')).toBe('quality')
    expect(parentOf('console')).toBe('console')
  })

  it('groups visible views into tabs, keeping section order and dropping hidden ones', () => {
    // AI off: no triggers / working hours / improve / evals.
    const g = groupViews(['console', 'history', 'logs', 'schedules', 'analytics', 'forms', 'specs', 'dev'])
    expect(g.map(x => x.key)).toEqual(['console', 'history', 'automation', 'forms', 'dev'])
    expect(g.find(x => x.key === 'history')!.sections.map(s => s.key)).toEqual(['history', 'analytics', 'logs'])
    expect(g.find(x => x.key === 'automation')!.sections.map(s => s.key)).toEqual(['schedules'])
    expect(g.find(x => x.key === 'console')!.sections).toEqual([])
  })

  it('every section lives under exactly one tab', () => {
    const all = Object.values(NAV_SECTIONS).flat().map(s => s.key)
    expect(new Set(all).size).toBe(all.length)
  })
})
