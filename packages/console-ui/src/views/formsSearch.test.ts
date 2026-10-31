import { describe, expect, it } from 'vitest'

import type { Host, Runnable } from '../bridge'
import { collapseByRunnable, loadFormsScope, runnableMatchesText, saveFormsScope, selectForms, FORMS_SCOPE_KEY } from './formsSearch'

const host = (name: string, connected = true, group?: string): Host =>
  ({ name, connected, runnableCount: 0, groups: [], group }) as Host
const rn = (name: string, h: string, extra: Partial<Runnable> = {}): Runnable =>
  ({ name, host: h, group: 'ops', description: '', args: [], ...extra }) as Runnable

const hosts = [host('local'), host('web-01', true, 'prod'), host('web-02', false, 'prod'), host('db-01', true, 'data')]
const runnables = [
  rn('disk-usage', 'local'),
  rn('disk-usage', 'web-01'),
  rn('disk-usage', 'web-02'),
  rn('restart-web', 'web-01', { description: 'Restart the nginx service' }),
  rn('restart-web', 'web-02', { description: 'Restart the nginx service' }),
  rn('pg-vacuum', 'db-01', { group: 'dba' }),
  rn('cron', 'db-01', { commands: { add: { description: 'Add a crontab line', args: [] } } as never }),
  rn('ghost', 'old-host'),  // cached for a host outside the active profile
]

const select = (o: Partial<Parameters<typeof selectForms>[0]>) =>
  selectForms({ runnables, hosts, selectedHost: 'local', scope: 'host', activeScope: [], search: '', ...o })
const names = (s: ReturnType<typeof selectForms>) => s.entries.map(e => e.name)

describe('Forms search scope', () => {
  it('host scope shows only the selected host', () => {
    const s = select({})
    expect(s.scope).toBe('host')
    expect(names(s)).toEqual(['disk-usage'])
    expect(s.entries[0].records.map(r => r.host)).toEqual(['local'])
  })

  it('host scope counts matches on other hosts while searching', () => {
    expect(select({ search: 'restart' }).elsewhere).toBe(1)
    expect(select({ search: 'disk' }).elsewhere).toBe(0)  // also here
    expect(select({}).elsewhere).toBe(0)                  // no text, no hint
  })

  it('profile scope searches every sidebar host, one card per runnable', () => {
    const s = select({ scope: 'profile', search: 'nginx' })
    expect(names(s)).toEqual(['restart-web'])
    expect(s.entries[0].records.map(r => r.host)).toEqual(['web-01', 'web-02'])
  })

  it('leaves out hosts outside the active profile', () => {
    expect(names(select({ scope: 'profile' }))).not.toContain('ghost')
  })

  it('orders hosts: selected, then connected, then disconnected', () => {
    const s = select({ scope: 'profile', search: 'disk', selectedHost: 'web-02' })
    expect(s.entries[0].records.map(r => r.host)).toEqual(['web-02', 'local', 'web-01'])
    const t = select({ scope: 'profile', search: 'disk', selectedHost: 'db-01' })
    expect(t.entries[0].records.map(r => r.host)).toEqual(['local', 'web-01', 'web-02'])
  })

  it('host: and group: tokens search the whole profile', () => {
    const s = select({ search: 'group:prod' })
    expect(s.scope).toBe('profile')
    expect(names(s)).toEqual(['disk-usage', 'restart-web'])
    expect(s.entries[0].records.map(r => r.host)).toEqual(['web-01', 'web-02'])
    expect(names(select({ search: 'host:db vacuum' }))).toEqual(['pg-vacuum'])
  })

  it('no selected host means the whole profile', () => {
    expect(select({ selectedHost: '' }).scope).toBe('profile')
  })

  it('venv scope chips narrow the host scope only', () => {
    expect(names(select({ selectedHost: 'db-01', activeScope: ['dba'] }))).toEqual(['pg-vacuum'])
    expect(names(select({ scope: 'profile', activeScope: ['dba'] }))).toContain('disk-usage')
  })

  it('without a host list nothing is filtered by profile', () => {
    expect(names(selectForms({ runnables, hosts: [], selectedHost: '', scope: 'host', activeScope: [], search: 'ghost' }))).toEqual(['ghost'])
  })
})

describe('runnableMatchesText', () => {
  it('needs every word, anywhere, incl. subcommands', () => {
    const cron = runnables.find(r => r.name === 'cron')!
    expect(runnableMatchesText(cron, 'crontab line')).toBe(true)
    expect(runnableMatchesText(cron, 'CRON add')).toBe(true)
    expect(runnableMatchesText(cron, 'cron remove')).toBe(false)
    expect(runnableMatchesText(cron, '')).toBe(true)
  })
})

describe('collapseByRunnable', () => {
  it('keeps venvs apart and drops duplicate host records', () => {
    const recs = [rn('which', 'local', { group: 'console' }), rn('which', 'web-01', { group: 'linux' }), rn('which', 'web-01', { group: 'linux' })]
    const e = collapseByRunnable(recs, hosts, 'local')
    expect(e.map(x => [x.group, x.records.length])).toEqual([['console', 1], ['linux', 1]])
  })
})

describe('scope persistence', () => {
  it('round-trips and defaults to host', () => {
    const store = new Map<string, string>()
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => { store.set(k, v) } }
    expect(loadFormsScope(storage)).toBe('host')
    saveFormsScope('profile', storage)
    expect(store.get(FORMS_SCOPE_KEY)).toBe('profile')
    expect(loadFormsScope(storage)).toBe('profile')
    expect(loadFormsScope(null)).toBe('host')
  })
})
