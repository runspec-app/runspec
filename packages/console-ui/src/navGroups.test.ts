import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { layoutNavTabs, hostMatches, NAV_GROUPS, NAV_OVERFLOW } from './navGroups'

const tabs = (...keys: string[]) => keys.map(key => ({ key }))
const keys = (ts: { key: string }[]) => ts.map(t => t.key)

describe('layoutNavTabs()', () => {
  it('groups the main tabs and folds the occasional ones into More', () => {
    const all = tabs('console', 'history', 'logs', 'files', 'knowledge', 'improve', 'evals',
      'specs', 'forms', 'marketplace', 'schedules', 'triggers', 'rota', 'dev')
    const { groups, overflow, ordered } = layoutNavTabs(all)
    expect(groups.map(keys)).toEqual([
      ['console', 'forms', 'history', 'logs'],
      ['schedules', 'triggers', 'rota'],
      ['knowledge', 'marketplace'],
    ])
    expect(keys(overflow)).toEqual(['files', 'specs', 'improve', 'evals', 'dev'])
    expect(ordered).toHaveLength(all.length)
    expect(keys(ordered).slice(0, 4)).toEqual(['console', 'forms', 'history', 'logs'])
  })

  it('drops groups emptied by the AI switch and keeps unknown tabs reachable', () => {
    const { groups, overflow } = layoutNavTabs(tabs('console', 'forms', 'schedules', 'newthing'))
    expect(groups.map(keys)).toEqual([['console', 'forms'], ['schedules']])
    expect(keys(overflow)).toEqual(['newthing'])
  })

  it('places every tab App.tsx defines', () => {
    const src = readFileSync(resolve(__dirname, 'App.tsx'), 'utf8')
    const block = src.slice(src.indexOf('const navTabs'), src.indexOf('], features)'))
    const defined = [...block.matchAll(/key: '([a-z]+)'\s+as ViewKey/g)].map(m => m[1])
    expect(defined.length).toBeGreaterThan(5)
    const known = new Set([...NAV_GROUPS.flat(), ...NAV_OVERFLOW])
    for (const k of defined) expect(known.has(k), k).toBe(true)
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
