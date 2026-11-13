import { describe, it, expect } from 'vitest'
import { askLlmHeading } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

const block = (over: Partial<InvocationBlock>): InvocationBlock => ({
  id: 'b1', sessionId: 's1', type: 'run', label: '/upgrade-packages on little-boy',
  startedAt: '', lines: [], segments: [], currentText: '', done: true, ...over,
})

describe('askLlmHeading', () => {
  it('names what ran and where', () => {
    expect(askLlmHeading(block({ exitCode: 0 }))).toBe('Output from /upgrade-packages on little-boy')
  })

  it('says a scheduled run failed, so the assistant has a failure to explain', () => {
    expect(askLlmHeading(block({ exitCode: 1, trigger: { name: 'Nightly upgrade', source: 'schedule' } })))
      .toBe('Output from /upgrade-packages on little-boy (scheduled: Nightly upgrade) — it failed (exit code 1)')
  })

  it('names a trigger, and says nothing about exit codes for a chat', () => {
    expect(askLlmHeading(block({ type: 'chat', label: 'Trigger t · new_mail', exitCode: 1, trigger: { name: 't', source: 'outlook' } })))
      .toBe('Output from Trigger t · new_mail (trigger: t)')
  })
})
