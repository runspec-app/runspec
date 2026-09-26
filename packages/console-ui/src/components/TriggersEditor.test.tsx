/**
 * Unit tests for the TriggersEditor form (de)serialisation — the declarative
 * multi-runnable action list, the LLM pre-turn, and the agent action allowlist.
 * These cover the round-trip logic the DOM tests can't reach cheaply.
 */
import { describe, it, expect } from 'vitest'

import { triggerToForm, formToTrigger, parseRun, composeRun, argsForRun, literalFromPattern } from './TriggersEditor'
import type { Runnable } from '../bridge'

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

  it('lifts match.mailbox/folder into their own fields and back, keeping other rows', () => {
    const form = triggerToForm({
      name: 'c', source: 'outlook', event: 'new_mail', mode: 'agent',
      match: { mailbox: '^helpdesk@corp\\.com$', folder: '^Tickets/Open$', subject: '(?i)reset' },
      claim: true, claim_folder: 'Tickets/Claimed',
    })
    expect(form.match_mailbox).toBe('^helpdesk@corp\\.com$')
    expect(form.match_folder).toBe('^Tickets/Open$')
    expect(form.match).toEqual([{ field: 'subject', pattern: '(?i)reset' }])
    expect(form.claim).toBe(true)
    expect(form.claim_folder).toBe('Tickets/Claimed')

    const out = formToTrigger(form)
    expect(out.match).toEqual({
      subject: '(?i)reset', mailbox: '^helpdesk@corp\\.com$', folder: '^Tickets/Open$',
    })
    expect(out.claim).toBe(true)
    expect(out.claim_folder).toBe('Tickets/Claimed')
  })

  it('omits an empty mailbox-to-match (fires for any mailbox)', () => {
    const out = formToTrigger({
      name: 'c', source: 'outlook', event: 'new_mail', mode: 'agent',
      autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default',
      map: [], reply_draft: false, rule_kind: 'single', action_mode: 'sequential',
      actions: [], rules: 'x', runnables: [], action_allow: [],
      match_mailbox: '', match_folder: '^Tickets/Open$',
    })
    expect(out.match).toEqual({ folder: '^Tickets/Open$' })
    expect((out.match as Record<string, unknown>).mailbox).toBeUndefined()
  })

  it('omits claim keys when the claim system is off', () => {
    const out = formToTrigger({
      name: 'a', source: 'outlook', event: 'new_mail', mode: 'agent',
      autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default',
      map: [], reply_draft: false, rule_kind: 'single', action_mode: 'sequential',
      actions: [], rules: 'do it', runnables: [], action_allow: [],
      claim: false, claim_folder: '',
    })
    expect(out.claim).toBeUndefined()
    expect(out.claim_folder).toBeUndefined()
  })
})

describe('literalFromPattern (Trigger-now folder/mailbox pre-seed)', () => {
  it('de-anchors and unescapes a simple pattern', () => {
    expect(literalFromPattern('^Tickets/Open$')).toBe('Tickets/Open')
    expect(literalFromPattern('^helpdesk@corp\\.com$')).toBe('helpdesk@corp.com')
    expect(literalFromPattern('Inbox')).toBe('Inbox')
  })
  it('returns empty for a pattern with other regex metacharacters', () => {
    expect(literalFromPattern('(?i)tickets|open')).toBe('')
    expect(literalFromPattern('Tickets/.*')).toBe('')
  })
})

describe('run-target picker helpers', () => {
  it('parseRun splits a host target', () => {
    expect(parseRun('web01__reset-password')).toEqual({
      kind: 'host', target: 'web01', runnable: 'reset-password',
    })
  })

  it('parseRun splits a group target', () => {
    expect(parseRun('group:Production__reset-password')).toEqual({
      kind: 'group', target: 'Production', runnable: 'reset-password',
    })
  })

  it('parseRun tolerates an incomplete value', () => {
    expect(parseRun('web01')).toEqual({ kind: 'host', target: 'web01', runnable: '' })
    expect(parseRun('')).toEqual({ kind: 'host', target: '', runnable: '' })
  })

  it('composeRun is the inverse and empty until complete', () => {
    expect(composeRun('host', 'web01', 'backup')).toBe('web01__backup')
    expect(composeRun('group', 'Prod', 'backup')).toBe('group:Prod__backup')
    expect(composeRun('host', 'web01', '')).toBe('') // runnable missing → invalid
    expect(composeRun('host', '', 'backup')).toBe('')
  })

  it('argsForRun resolves the declared args of the chosen runnable', () => {
    const runnables: Runnable[] = [
      { name: 'backup', group: 'g', host: 'web01', args: [{ name: 'db', type: 'str', required: true }] },
      { name: 'backup', group: 'g', host: 'web02', args: [{ name: 'db', type: 'str', required: true }] },
      { name: 'other', group: 'g', host: 'web01', args: [] },
    ]
    expect(argsForRun('web01__backup', runnables).map(a => a.name)).toEqual(['db'])
    // group target resolves by name from any member
    expect(argsForRun('group:Prod__backup', runnables).map(a => a.name)).toEqual(['db'])
    // unknown / incomplete → no args
    expect(argsForRun('web01__nope', runnables)).toEqual([])
    expect(argsForRun('', runnables)).toEqual([])
  })
})

describe('trigger model', () => {
  it('round-trips a per-trigger model and trims it', () => {
    const form = triggerToForm({ name: 't', source: 'outlook', event: 'new_mail', model: 'claude-haiku-4-5' })
    expect(form.model).toBe('claude-haiku-4-5')
    const out = formToTrigger({ ...form, model: ' claude-opus-5-5 ' })
    expect(out.model).toBe('claude-opus-5-5')
  })

  it('defaults to no model', () => {
    expect(triggerToForm({ name: 't', source: 'outlook', event: 'new_mail' }).model).toBe('')
  })
})
