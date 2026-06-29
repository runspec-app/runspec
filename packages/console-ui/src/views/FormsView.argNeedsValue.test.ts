import { describe, it, expect, vi } from 'vitest'
import { argNeedsValue, credEnvPrefix, credEnvNames, bindingsForArgs, initialFormValues, partitionArgs, suppressPlainEnter } from './FormsView'
import type { ArgDef } from '../bridge'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })
const cred = (over: Partial<{ id: string; label: string; kind: string; has_secret: boolean; username: string; username_arg: string; secret_arg: string }>) =>
  ({ id: 'c', label: 'C', kind: 'userpass', has_secret: true, username: '', username_arg: '', secret_arg: '', ...over })

describe('argNeedsValue', () => {
  it('blocks a required non-secret arg when empty', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), '')).toBe(true)
    expect(argNeedsValue(arg({ type: 'str', required: true }), undefined)).toBe(true)
  })

  it('does not block when a value is present', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), 'v')).toBe(false)
  })

  it('blocks a required password arg when empty (the run dialog separately relaxes this when a selected credential binds the arg)', () => {
    // argNeedsValue is binding-agnostic; the form's submit check additionally
    // skips a required arg that a selected credential supplies (see bindingsForArgs).
    expect(argNeedsValue(arg({ name: 'password', type: 'password', required: true }), '')).toBe(true)
    expect(argNeedsValue(arg({ name: 'password', type: 'password', required: true }), undefined)).toBe(true)
  })

  it('does not block an optional password arg', () => {
    expect(argNeedsValue(arg({ name: 'password', type: 'password', required: false }), '')).toBe(false)
  })

  it('does not block an optional arg', () => {
    expect(argNeedsValue(arg({ type: 'str', required: false }), '')).toBe(false)
  })
})

describe('credEnvPrefix / credEnvNames', () => {
  it('derives a prefix from a label', () => {
    expect(credEnvPrefix('Jira')).toBe('JIRA')
    expect(credEnvPrefix('Jira Dev')).toBe('JIRA_DEV')
    expect(credEnvPrefix('my-svc.acct')).toBe('MY_SVC_ACCT')
    expect(credEnvPrefix('  __weird__  ')).toBe('WEIRD')
    expect(credEnvPrefix('!!!')).toBe('')
  })

  it('derives env var names by kind, including aliases', () => {
    expect(credEnvNames('Jira', 'userpass')).toEqual(['JIRA_USERNAME', 'JIRA_USER', 'JIRA_PASSWORD'])
    expect(credEnvNames('DB', 'password')).toEqual(['DB_PASSWORD'])
    expect(credEnvNames('CI', 'token')).toEqual(['CI_TOKEN', 'CI_PAT'])
    expect(credEnvNames('Repo', 'ssh-key')).toEqual([])
    expect(credEnvNames('', 'userpass')).toEqual([])
  })
})

describe('initialFormValues (edit-and-rerun prefill)', () => {
  const args: ArgDef[] = [
    arg({ name: 'workers', type: 'int', default: 4 }),
    arg({ name: 'mode', type: 'choice', options: ['a', 'b'], default: 'a' }),
    arg({ name: 'verbose', type: 'flag', default: false }),
    arg({ name: 'name', type: 'str', required: true }),
    arg({ name: 'token', type: 'str', default: '$API_TOKEN' }),
    arg({ name: 'password', type: 'password', required: true }),
  ]

  it('uses spec defaults with no prefill ($VAR refs left blank)', () => {
    expect(initialFormValues(args)).toEqual({ workers: 4, mode: 'a', verbose: false })
  })

  it('overlays the previous run values onto the defaults', () => {
    expect(initialFormValues(args, { workers: 8, mode: 'b', verbose: true, name: 'run-42' }))
      .toEqual({ workers: 8, mode: 'b', verbose: true, name: 'run-42' })
  })

  it('drops prefill keys that are not declared args', () => {
    const v = initialFormValues(args, { workers: 8, removed_arg: 'x' })
    expect(v.workers).toBe(8)
    expect('removed_arg' in v).toBe(false)
  })

  it('never pre-fills a password arg (secrets are not recorded)', () => {
    const v = initialFormValues(args, { password: 'leaked' })
    expect('password' in v).toBe(false)
  })
})

describe('partitionArgs', () => {
  it('keeps required args visible and folds the rest into defaulted', () => {
    const args: ArgDef[] = [
      arg({ name: 'name', type: 'str', required: true }),
      arg({ name: 'workers', type: 'int', required: false, default: 4 }),
      arg({ name: 'verbose', type: 'flag', required: false, default: false }),
      arg({ name: 'token', type: 'str', required: false, default: '$API_TOKEN' }),
    ]
    const { required, defaulted } = partitionArgs(args)
    expect(required.map(a => a.name)).toEqual(['name'])
    // A $VAR-ref default is still non-required → collapses with the rest.
    expect(defaulted.map(a => a.name)).toEqual(['workers', 'verbose', 'token'])
  })

  it('handles all-required and all-optional groups', () => {
    expect(partitionArgs([arg({ required: true })]).defaulted).toEqual([])
    expect(partitionArgs([arg({ required: false })]).required).toEqual([])
  })
})

describe('suppressPlainEnter', () => {
  it('suppresses a bare Enter', () => {
    const e = { key: 'Enter', shiftKey: false, preventDefault: vi.fn() }
    expect(suppressPlainEnter(e)).toBe(true)
    expect(e.preventDefault).toHaveBeenCalled()
  })

  it('lets Shift+Enter through to insert a newline', () => {
    const e = { key: 'Enter', shiftKey: true, preventDefault: vi.fn() }
    expect(suppressPlainEnter(e)).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('ignores other keys', () => {
    const e = { key: 'a', shiftKey: false, preventDefault: vi.fn() }
    expect(suppressPlainEnter(e)).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })
})

describe('bindingsForArgs', () => {
  const declared = new Set(['user', 'password'])

  it('maps a credential\'s username/secret args to bindings when the runnable declares them', () => {
    const b = bindingsForArgs([cred({ label: 'Jira', username_arg: 'user', secret_arg: 'password' })], declared)
    expect(b).toEqual({
      user: { label: 'Jira', role: 'username' },
      password: { label: 'Jira', role: 'secret' },
    })
  })

  it('ignores bindings to args the runnable does not declare', () => {
    const b = bindingsForArgs([cred({ label: 'Jira', username_arg: 'login', secret_arg: 'token' })], declared)
    expect(b).toEqual({})
  })

  it('ignores credentials with no binding configured', () => {
    expect(bindingsForArgs([cred({ label: 'EnvOnly' })], declared)).toEqual({})
  })

  it('first selected credential wins a contested arg', () => {
    const b = bindingsForArgs([
      cred({ id: 'a', label: 'A', secret_arg: 'password' }),
      cred({ id: 'b', label: 'B', secret_arg: 'password' }),
    ], declared)
    expect(b.password).toEqual({ label: 'A', role: 'secret' })
  })
})
