/**
 * SpecsView — pure helpers (visibility / denylist / prettyJson) and a light
 * render smoke test of the master/detail inspector.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SpecsView, effectiveVisibility, matchesDenylist, prettyJson } from './SpecsView'
import type { Runnable, DenylistHostStatus } from '../bridge'

const { getToolVisibility, denylistStatus } = vi.hoisted(() => ({
  getToolVisibility: vi.fn(),
  denylistStatus: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_tool_visibility') return getToolVisibility
      if (p === 'denylist_status') return denylistStatus
      return vi.fn()
    },
  }),
}))

beforeEach(() => {
  getToolVisibility.mockReset().mockResolvedValue({ hidden_from_agent: [], hidden_from_all: [] })
  denylistStatus.mockReset().mockResolvedValue([])
})

// ── Pure helpers ──────────────────────────────────────────────────────────────

describe('effectiveVisibility', () => {
  it('is visible with no config', () => {
    expect(effectiveVisibility('deploy', 'ops', 'web1', null)).toBe('visible')
    expect(effectiveVisibility('deploy', 'ops', 'web1', { hidden_from_agent: [], hidden_from_all: [] })).toBe('visible')
  })
  it('reports agent-hidden and all-hidden (all wins)', () => {
    expect(effectiveVisibility('deploy', 'ops', 'web1', { hidden_from_agent: ['deploy'], hidden_from_all: [] })).toBe('agent')
    expect(effectiveVisibility('deploy', 'ops', 'web1', { hidden_from_agent: ['deploy'], hidden_from_all: ['deploy'] })).toBe('all')
  })
  it('honours group: / host: prefixes and globs', () => {
    expect(effectiveVisibility('x', 'ops', 'web1', { hidden_from_agent: ['group:ops'], hidden_from_all: [] })).toBe('agent')
    expect(effectiveVisibility('x', 'ops', 'web1', { hidden_from_agent: [], hidden_from_all: ['host:web*'] })).toBe('all')
  })
})

describe('matchesDenylist', () => {
  const status = (over: Partial<DenylistHostStatus>): DenylistHostStatus =>
    ({ host: 'web1', venv: 'app', ok: true, mode: 'enforce', disabled: false, rules: [], ...over })

  it('is unchecked when no statuses given', () => {
    expect(matchesDenylist('deploy', 'web1', null).checked).toBe(false)
  })
  it('matches a rule by runnable + host glob', () => {
    const m = matchesDenylist('deploy', 'web1', [status({ rules: [{ runnables: ['dep*'], hosts: ['*'], users: ['*'], reason: 'freeze' }] })])
    expect(m.checked).toBe(true)
    expect(m.rules).toHaveLength(1)
    expect(m.mode).toBe('enforce')
  })
  it('does not match a rule scoped to another host', () => {
    const m = matchesDenylist('deploy', 'web1', [status({ rules: [{ runnables: ['deploy'], hosts: ['db*'], users: ['*'], reason: 'x' }] })])
    expect(m.rules).toHaveLength(0)
  })
  it('reports a whole-venv disable', () => {
    expect(matchesDenylist('deploy', 'web1', [status({ disabled: true })]).disabled).toBe(true)
  })
  it('exempts the reserved runspec control-plane name', () => {
    const m = matchesDenylist('runspec', 'web1', [status({ disabled: true, rules: [{ runnables: ['*'], hosts: ['*'], users: ['*'], reason: 'all' }] })])
    expect(m.rules).toHaveLength(0)
    expect(m.disabled).toBe(false)
  })
  it('ignores statuses for other hosts', () => {
    expect(matchesDenylist('deploy', 'web1', [status({ host: 'db1', disabled: true })]).rules).toHaveLength(0)
  })
})

describe('prettyJson', () => {
  it('indents an object', () => {
    expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}')
  })
  it('guards against circular refs', () => {
    const o: Record<string, unknown> = {}
    o.self = o
    expect(prettyJson(o)).toContain('[Circular]')
  })
  it('caps long output', () => {
    const out = prettyJson({ s: 'x'.repeat(100) }, 40)
    expect(out.endsWith('… (truncated)')).toBe(true)
  })
})

// ── Render smoke test ─────────────────────────────────────────────────────────

const rn = (over: Partial<Runnable> & { name: string }): Runnable =>
  ({ group: 'ops', host: 'web1', description: '', args: [], ...over } as Runnable)

const PROPS = {
  selectedHost: '', activeScope: [] as string[], onScopeToggle: () => {},
}

describe('SpecsView render', () => {
  it('lists runnables and inspects one on click', async () => {
    const runnables = [
      rn({ name: 'backup', description: 'Back up things', args: [{ name: 'source', type: 'path', required: true }] }),
      rn({ name: 'deploy', autonomy: 'autonomous' }),
    ]
    await act(async () => { render(<SpecsView runnables={runnables} {...PROPS} />) })

    // list shows both runnables
    await waitFor(() => expect(screen.getAllByText('backup').length).toBeGreaterThan(0))
    expect(screen.getAllByText('deploy').length).toBeGreaterThan(0)
    // empty state until a selection is made
    expect(screen.getByText(/Select a runnable to inspect/i)).toBeTruthy()

    // click the backup row → inspector renders its sections
    await userEvent.click(screen.getAllByText('backup')[0])
    await waitFor(() => expect(screen.getByText('Settings')).toBeTruthy())
    expect(screen.getByText('Back up things')).toBeTruthy()
    expect(screen.getByText(/Raw spec/i)).toBeTruthy()
  })
})
