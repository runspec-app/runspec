import { describe, it, expect } from 'vitest'

import { dedupToolName, legacyDedupToolName, matchRunnableForEdit, resolveCommandPath, sanitizeToolName, toCommandPath } from './formsMatch'
import type { Runnable } from '../bridge'

const r = (name: string, host: string, group?: string): Runnable =>
  ({ name, host, group, autonomy: 'autonomous', args: [], commands: [] }) as unknown as Runnable

describe('sanitizeToolName', () => {
  it('replaces disallowed chars and caps at 64', () => {
    expect(sanitizeToolName('a b.c/d')).toBe('a_b_c_d')
    expect(sanitizeToolName('x'.repeat(80))).toHaveLength(64)
  })
})

describe('dedup / legacy tool names', () => {
  it('dedup is the bare sanitised name (no group qualifier)', () => {
    expect(dedupToolName({ group: 'console', name: 'which' })).toBe('which')
  })
  it('legacy keeps the group__name spelling', () => {
    expect(legacyDedupToolName({ group: 'console', name: 'which' })).toBe('console__which')
    expect(legacyDedupToolName({ name: 'which' })).toBe('which')
  })
})

describe('matchRunnableForEdit', () => {
  const runnables = [r('which', 'local', 'console'), r('which', 'web-01', 'runspec_linux'), r('deploy', 'web-01')]

  it('exact host + name wins (the History rerun path)', () => {
    expect(matchRunnableForEdit(runnables, 'which', 'web-01')?.host).toBe('web-01')
  })
  it('resolves a dedup id, pinning the concrete host', () => {
    expect(matchRunnableForEdit(runnables, 'which', 'local')?.host).toBe('local')
  })
  it('resolves a dedup id by group when host is a venv/group', () => {
    expect(matchRunnableForEdit(runnables, 'which', 'runspec_linux')?.host).toBe('web-01')
  })
  it('resolves an explicit host__name id', () => {
    expect(matchRunnableForEdit(runnables, 'web-01__deploy', '')?.name).toBe('deploy')
  })
  it('returns undefined when nothing matches', () => {
    expect(matchRunnableForEdit(runnables, 'nope', 'nowhere')).toBeUndefined()
  })
})

describe('toCommandPath', () => {
  it('accepts an array, a string, or a space-separated nested string', () => {
    expect(toCommandPath(['restore', 'full'])).toEqual(['restore', 'full'])
    expect(toCommandPath('get-issue')).toEqual(['get-issue'])
    expect(toCommandPath('restore full')).toEqual(['restore', 'full'])
    expect(toCommandPath(['restore full'])).toEqual(['restore', 'full'])
  })
  it('is [] for absent / empty / non-path values', () => {
    expect(toCommandPath(undefined)).toEqual([])
    expect(toCommandPath('')).toEqual([])
    expect(toCommandPath(['', ' '])).toEqual([])
    expect(toCommandPath({ a: 1 })).toEqual([])
  })
})

describe('resolveCommandPath', () => {
  const tree = {
    name: 'backup', host: 'h', args: [],
    commands: {
      restore: { name: 'restore', host: 'h', args: [], commands: { full: { name: 'full', host: 'h', args: [] } } },
      list: { name: 'list', host: 'h', args: [] },
    },
  } as unknown as Runnable

  it('keeps a path that exists', () => {
    expect(resolveCommandPath(tree, ['restore', 'full'])).toEqual({ path: ['restore', 'full'], missing: [] })
    expect(resolveCommandPath(tree, [])).toEqual({ path: [], missing: [] })
  })
  it('stops at the deepest segment that exists', () => {
    expect(resolveCommandPath(tree, ['restore', 'partial'])).toEqual({ path: ['restore'], missing: ['partial'] })
    expect(resolveCommandPath(tree, ['gone'])).toEqual({ path: [], missing: ['gone'] })
  })
})
