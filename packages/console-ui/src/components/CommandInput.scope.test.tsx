import { describe, it, expect, vi } from 'vitest'

import { scopeLabel, type ComposerScope } from './CommandInput'

const scope = (active: string[]): ComposerScope => ({
  host: 'web-04', venvs: ['ops-tools', 'linux-tools', 'jira'], active, onToggle: vi.fn(), onClear: vi.fn(),
})

describe('scopeLabel()', () => {
  it('names the host and how many of its venvs the / menu lists', () => {
    expect(scopeLabel(scope([]))).toBe('web-04 · all venvs')
    expect(scopeLabel(scope(['jira']))).toBe('web-04 · jira')
    expect(scopeLabel(scope(['jira', 'ops-tools']))).toBe('web-04 · 2 venvs')
    expect(scopeLabel(scope(['jira', 'ops-tools', 'linux-tools']))).toBe('web-04 · all venvs')
  })
})
