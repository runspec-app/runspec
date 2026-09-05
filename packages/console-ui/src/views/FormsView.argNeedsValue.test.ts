import { describe, it, expect, vi } from 'vitest'
import { argNeedsValue, credEnvPrefix, credEnvNames, bindingsForArgs, initialFormValues, partitionArgs, suppressPlainEnter, argSpansRow, autonomyTagProps, dedupToolName, matchRunnableForEdit } from './FormsView'
import type { ArgDef, Runnable } from '../bridge'

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
  it('collapses only args that carry a default; the rest stay visible', () => {
    const args: ArgDef[] = [
      arg({ name: 'name', type: 'str', required: true }),
      arg({ name: 'note', type: 'str', required: false }),            // optional, NO default → visible
      arg({ name: 'workers', type: 'int', required: false, default: 4 }),
      arg({ name: 'verbose', type: 'flag', required: false, default: false }),
      arg({ name: 'token', type: 'str', required: false, default: '$API_TOKEN' }),
    ]
    const { visible, defaulted } = partitionArgs(args)
    // required AND optional-no-default lead the form
    expect(visible.map(a => a.name)).toEqual(['name', 'note'])
    // only args with an actual default (incl. a flag's false and a $VAR ref) collapse
    expect(defaulted.map(a => a.name)).toEqual(['workers', 'verbose', 'token'])
  })

  it('a required arg and an optional-no-default arg are both visible, not defaulted', () => {
    expect(partitionArgs([arg({ required: true })]).defaulted).toEqual([])
    const opt = partitionArgs([arg({ name: 'note', required: false })])
    expect(opt.defaulted).toEqual([])
    expect(opt.visible.map(a => a.name)).toEqual(['note'])
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

describe('argSpansRow', () => {
  it('spans the full row for path and folder pickers', () => {
    expect(argSpansRow(arg({ type: 'path' }))).toBe(true)
    expect(argSpansRow(arg({ type: 'path', ui: 'directory' }))).toBe(true)
    expect(argSpansRow(arg({ type: 'str', ui: 'directory' }))).toBe(true)
  })

  it('keeps compact fields in a grid cell', () => {
    expect(argSpansRow(arg({ type: 'str' }))).toBe(false)
    expect(argSpansRow(arg({ type: 'int' }))).toBe(false)
    expect(argSpansRow(arg({ type: 'choice', options: ['a', 'b'] }))).toBe(false)
    expect(argSpansRow(arg({ type: 'flag' }))).toBe(false)
    expect(argSpansRow(arg({ type: 'str', ui: 'date' }))).toBe(false)
  })
})

describe('autonomyTagProps', () => {
  it('maps each level to a colour + short label', () => {
    expect(autonomyTagProps('autonomous')).toEqual({ color: 'green', label: 'auto' })
    expect(autonomyTagProps('supervised')).toEqual({ color: 'gold', label: 'supervised' })
    expect(autonomyTagProps('manual')).toEqual({ color: 'red', label: 'manual' })
    expect(autonomyTagProps('confirm')).toEqual({ color: 'default', label: 'confirm' })
  })

  it('is case-insensitive', () => {
    expect(autonomyTagProps('AUTONOMOUS').label).toBe('auto')
  })

  it('defaults undefined / blank / unknown to confirm', () => {
    expect(autonomyTagProps(undefined)).toEqual({ color: 'default', label: 'confirm' })
    expect(autonomyTagProps('')).toEqual({ color: 'default', label: 'confirm' })
    expect(autonomyTagProps('weird')).toEqual({ color: 'default', label: 'confirm' })
  })
})

describe('dedupToolName', () => {
  it('joins group and name, sanitising non-tool-name chars', () => {
    expect(dedupToolName({ group: 'ops', name: 'deploy' })).toBe('ops__deploy')
    expect(dedupToolName({ name: 'backup' })).toBe('backup')          // no group → bare name
    expect(dedupToolName({ group: 'my.venv', name: 'sync-files' })).toBe('my_venv__sync-files')
  })
})

describe('matchRunnableForEdit', () => {
  const rn = (over: Partial<Runnable> & { name: string }): Runnable =>
    ({ group: '', host: 'local', args: [], ...over } as Runnable)
  const A = rn({ name: 'deploy', group: 'ops', host: 'web1' })
  const B = rn({ name: 'deploy', group: 'ops', host: 'web2' })
  const C = rn({ name: 'backup', group: '', host: 'db1' })
  const D = rn({ name: 'sync-files', group: 'my.venv', host: 'nas' })
  const all = [A, B, C, D]

  it('resolves a plain name + concrete host (human/history path)', () => {
    expect(matchRunnableForEdit(all, 'deploy', 'web1')).toBe(A)
    expect(matchRunnableForEdit(all, 'backup', 'db1')).toBe(C)
  })

  it('resolves a search-mode dedup id, pinning the host', () => {
    expect(matchRunnableForEdit(all, 'ops__deploy', 'web2')).toBe(B)
    expect(matchRunnableForEdit(all, 'my_venv__sync-files', 'nas')).toBe(D)
  })

  it('resolves a dedup id whose host field is actually the group', () => {
    expect(matchRunnableForEdit(all, 'ops__deploy', 'ops')).toBe(A) // group match → first
  })

  it('resolves a single-host dedup id with no host given', () => {
    expect(matchRunnableForEdit(all, 'backup', '')).toBe(C)
  })

  it('is ambiguous (undefined) for a multi-host dedup id with no host', () => {
    expect(matchRunnableForEdit(all, 'ops__deploy', '')).toBeUndefined()
  })

  it('resolves an explicit host__name tool id', () => {
    expect(matchRunnableForEdit(all, 'web2__deploy', '')).toBe(B)
  })

  it('returns undefined for an unknown id', () => {
    expect(matchRunnableForEdit(all, 'nope', 'web1')).toBeUndefined()
  })
})
