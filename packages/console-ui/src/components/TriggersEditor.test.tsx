/**
 * Unit tests for the TriggersEditor form (de)serialisation — the declarative
 * multi-runnable action list, the LLM pre-turn, and the agent action allowlist.
 * These cover the round-trip logic the DOM tests can't reach cheaply.
 */
import { describe, it, expect } from 'vitest'

import { triggerToForm, formToTrigger } from './TriggersEditor'

describe('TriggersEditor form mapping', () => {
  it('derives rule_kind=list and maps actions/action_mode/pre_turn from a trigger', () => {
    const form = triggerToForm({
      name: 'nightly',
      source: 'schedule',
      event: 'nightly_report',
      mode: 'rule',
      action_mode: 'parallel',
      pre_turn: 'Find the host.',
      actions: [
        { run: 'web01__check-memory', map: { threshold: '{{ _llm.level }}' } },
        { run: 'db01__check-disk' },
      ],
    })
    expect(form.rule_kind).toBe('list')
    expect(form.action_mode).toBe('parallel')
    expect(form.pre_turn).toBe('Find the host.')
    expect(form.actions).toHaveLength(2)
    expect(form.actions[0]).toEqual({
      run: 'web01__check-memory',
      map: [{ arg: 'threshold', template: '{{ _llm.level }}' }],
    })
  })

  it('single run → rule_kind=single', () => {
    const form = triggerToForm({
      name: 's', source: 'schedule', event: 'a', mode: 'rule', run: 'h__r',
    })
    expect(form.rule_kind).toBe('single')
  })

  it('emits actions array-of-tables shape for a list rule', () => {
    const out = formToTrigger({
      name: 'nightly', source: 'schedule', event: 'nightly_report',
      mode: 'rule', autonomy: 'confirm', enabled: true, match: [],
      once_per_thread: 'default', map: [], reply_draft: false,
      rule_kind: 'list', action_mode: 'parallel', pre_turn: 'Find host.',
      actions: [
        { run: 'web01__check-memory', map: [{ arg: 'svc', template: '{{ subject }}' }] },
        { run: '  ', map: [] }, // blank run dropped
      ],
      action_allow: [],
    })
    expect(out.mode).toBe('rule')
    expect(out.action_mode).toBe('parallel')
    expect(out.pre_turn).toBe('Find host.')
    expect(out.actions).toEqual([
      { run: 'web01__check-memory', map: { svc: '{{ subject }}' } },
    ])
    expect(out.run).toBeUndefined()
  })

  it('single rule emits run/map, not actions', () => {
    const out = formToTrigger({
      name: 's', source: 'schedule', event: 'a', mode: 'rule', autonomy: 'confirm',
      enabled: true, match: [], once_per_thread: 'default',
      map: [{ arg: 'x', template: '{{ y }}' }], reply_draft: false,
      rule_kind: 'single', action_mode: 'sequential', actions: [], action_allow: [],
      run: 'h__r',
    })
    expect(out.run).toBe('h__r')
    expect(out.map).toEqual({ x: '{{ y }}' })
    expect(out.actions).toBeUndefined()
  })

  it('agent mode emits action_allow and never actions', () => {
    const out = formToTrigger({
      name: 'a', source: 'outlook', event: 'new_mail', mode: 'agent',
      autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default',
      map: [], reply_draft: false, rule_kind: 'single', action_mode: 'sequential',
      actions: [], rules: 'do it', runnables: ['r1'],
      action_allow: ['create_document', 'outlook_draft'],
    })
    expect(out.action_allow).toEqual(['create_document', 'outlook_draft'])
    expect(out.actions).toBeUndefined()
    expect(out.run).toBeUndefined()
  })
})
