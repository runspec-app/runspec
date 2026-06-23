import { describe, it, expect } from 'vitest'
import { argNeedsValue, credEnvPrefix, credEnvNames } from './FormsView'
import type { ArgDef } from '../bridge'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

describe('argNeedsValue', () => {
  it('blocks a required non-secret arg when empty', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), '')).toBe(true)
    expect(argNeedsValue(arg({ type: 'str', required: true }), undefined)).toBe(true)
  })

  it('does not block when a value is present', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), 'v')).toBe(false)
  })

  it('blocks a required password arg when empty — humans must type required secrets', () => {
    // Credentials are no longer copied into args on the form path, so a required
    // secret must be typed (or the runnable should read an env-var credential).
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

  it('derives env var names by kind', () => {
    expect(credEnvNames('Jira', 'userpass')).toEqual(['JIRA_USERNAME', 'JIRA_PASSWORD'])
    expect(credEnvNames('DB', 'password')).toEqual(['DB_PASSWORD'])
    expect(credEnvNames('CI', 'token')).toEqual(['CI_TOKEN'])
    expect(credEnvNames('Repo', 'ssh-key')).toEqual([])
    expect(credEnvNames('', 'userpass')).toEqual([])
  })
})
