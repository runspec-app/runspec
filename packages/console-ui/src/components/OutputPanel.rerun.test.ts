import { describe, it, expect } from 'vitest'
import { rerunFromToolEntry } from './OutputPanel'
import type { ToolCallEntry } from './OutputPanel'

const entry = (name: string, input: Record<string, unknown>): ToolCallEntry => ({
  name, input, output: 'ok', running: false,
})

describe('rerunFromToolEntry', () => {
  it('unwraps the search-mode run_runnable envelope', () => {
    const r = rerunFromToolEntry(entry('run_runnable', {
      id: 'deploy', host: 'web1', args: { env: 'prod' }, command: ['rollout'],
    }))
    expect(r).toEqual({ host: 'web1', runnable: 'deploy', args: { env: 'prod' }, commandPath: ['rollout'] })
  })

  it('defaults an absent host / args / command in the envelope', () => {
    const r = rerunFromToolEntry(entry('run_runnable', { id: 'ping' }))
    expect(r).toEqual({ host: '', runnable: 'ping', args: {}, commandPath: [] })
  })

  it('returns null for a run_runnable envelope with no id', () => {
    expect(rerunFromToolEntry(entry('run_runnable', { host: 'web1' }))).toBeNull()
  })

  it('parses a per_host prefixed tool (prefix = host)', () => {
    const r = rerunFromToolEntry(entry('web1__deploy', { env: 'prod', command: ['rollout'] }))
    expect(r).toEqual({ host: 'web1', runnable: 'deploy', args: { env: 'prod' }, commandPath: ['rollout'] })
  })

  it('parses a dedup prefixed tool (prefix = venv/group), host resolved App-side', () => {
    const r = rerunFromToolEntry(entry('myvenv__backup', { full: true }))
    expect(r).toEqual({ host: 'myvenv', runnable: 'backup', args: { full: true }, commandPath: [] })
  })

  it('ignores built-ins with only single underscores', () => {
    expect(rerunFromToolEntry(entry('console_status', {}))).toBeNull()
    expect(rerunFromToolEntry(entry('run_python_sandboxed', { code: 'x' }))).toBeNull()
    expect(rerunFromToolEntry(entry('search_history', { limit: 5 }))).toBeNull()
  })

  it('ignores MCP tools', () => {
    expect(rerunFromToolEntry(entry('mcp_server_tool', { q: 1 }))).toBeNull()
  })

  it('does not mutate the entry input when stripping command', () => {
    const input = { env: 'prod', command: ['rollout'] }
    rerunFromToolEntry(entry('web1__deploy', input))
    expect(input).toEqual({ env: 'prod', command: ['rollout'] })
  })
})
