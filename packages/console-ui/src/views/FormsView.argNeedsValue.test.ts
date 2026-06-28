import { describe, it, expect } from 'vitest'
import { argNeedsValue, credEnvPrefix, credEnvNames, bindingsForArgs } from './FormsView'
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
