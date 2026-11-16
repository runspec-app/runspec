// A fan-out turn (the same call on many hosts) folds into one transcript row.
import { describe, expect, it } from 'vitest'
import { groupToolRuns, toolGroupSummary, toolPrefix, toolRowHost, TOOL_GROUP_MIN, type BlockSegment, type ToolCallEntry } from './OutputPanel'

const call = (name: string, input: Record<string, unknown> = {}, extra: Partial<ToolCallEntry> = {}): BlockSegment =>
  ({ kind: 'tool', entry: { name, input, running: false, durationMs: 40, ...extra } })

const fanout = (n: number, extra: Partial<ToolCallEntry> = {}) =>
  Array.from({ length: n }, (_, i) => call(`web-${String(i + 1).padStart(2, '0')}__system-info`, { detail: 'summary' }, { durationMs: 30 + i, ...extra }))

describe('toolPrefix / toolRowHost', () => {
  it('reads the host from a per_host tool name', () => {
    expect(toolPrefix('web-01__system-info')).toBe('web-01')
    expect(toolRowHost({ name: 'web-01__system-info', input: {}, running: false })).toBe('web-01')
  })
  it("prefers the host the input names, and leaves 'local' and built-ins unsaid", () => {
    expect(toolRowHost({ name: 'run_runnable', input: { host: 'db-1' }, running: false })).toBe('db-1')
    expect(toolRowHost({ name: 'local__fleet-inventory', input: {}, running: false })).toBe('')
    expect(toolRowHost({ name: 'run_python_sandboxed', input: {}, running: false })).toBe('')
  })
})

describe('groupToolRuns()', () => {
  it('folds a run of identical calls on different hosts into one group', () => {
    const segs: BlockSegment[] = [{ kind: 'text', text: 'Collecting…' }, ...fanout(24), call('local__fleet-inventory', { group: 'web' })]
    const items = groupToolRuns(segs)
    expect(items.map(i => i.kind)).toEqual(['text', 'group', 'tool'])
    expect(items[1].kind === 'group' && items[1].entries).toHaveLength(24)
  })

  it(`leaves fewer than ${TOOL_GROUP_MIN} alike, and calls with different args, as single rows`, () => {
    expect(groupToolRuns(fanout(2)).map(i => i.kind)).toEqual(['tool', 'tool'])
    const mixed = [call('a__x', { n: 1 }), call('b__x', { n: 2 }), call('c__x', { n: 3 })]
    expect(groupToolRuns(mixed).map(i => i.kind)).toEqual(['tool', 'tool', 'tool'])
  })

  it('ignores the host arg when comparing search-mode calls', () => {
    const segs = ['h1', 'h2', 'h3'].map(h => call('run_runnable', { id: 'uptime', host: h, args: {} }))
    expect(groupToolRuns(segs).map(i => i.kind)).toEqual(['group'])
  })

  it('never hides a call waiting at the approval gate inside a group', () => {
    const segs = fanout(4)
    const waiting = (segs[3] as { entry: ToolCallEntry }).entry
    waiting.running = true
    waiting.callId = 'c4'
    const items = groupToolRuns(segs, { call_id: 'c4', tool_name: waiting.name } as never)
    expect(items.map(i => i.kind)).toEqual(['group', 'tool'])
  })
})

describe('toolGroupSummary()', () => {
  const entries = (segs: BlockSegment[]) => segs.map(s => (s as { entry: ToolCallEntry }).entry)
  it('says where, how it went and the fastest–slowest time', () => {
    expect(toolGroupSummary(entries(fanout(24)))).toEqual({ where: 'on 24 hosts', status: 'all done', tone: 'ok', duration: '30ms–53ms' })
  })
  it('counts progress while calls are still running', () => {
    const e = entries(fanout(4))
    e[2].running = true; e[3].running = true
    expect(toolGroupSummary(e)).toMatchObject({ status: '2 of 4 done', tone: 'running' })
  })
  it('says how many were denied, and × n when the calls share a host', () => {
    const e = entries([call('x', {}, { operatorDecision: 'denied' }), call('x'), call('x')])
    expect(toolGroupSummary(e)).toMatchObject({ where: '× 3', status: '2 done · 1 denied', tone: 'ok' })
  })
})

describe('toolFailure() and failures in a group', () => {
  it('reads a non-zero exit or a call the console could not make', async () => {
    const { toolFailure } = await import('./OutputPanel')
    const done = (output: string, extra: Partial<ToolCallEntry> = {}) => ({ name: 'x', input: {}, running: false, output, ...extra })
    expect(toolFailure(done('[exit 255]\nssh: timed out'))).toBe('exit 255')
    expect(toolFailure(done("✗ No host in group 'web' has 'x'."))).toBe('failed')
    expect(toolFailure(done('{"ok": true}'))).toBeNull()
    expect(toolFailure(done('[exit 1]', { operatorDecision: 'denied' }))).toBeNull()
    expect(toolFailure(done('[exit 1]', { running: true }))).toBeNull()
  })
  it('leads a fan-out summary with its failures', () => {
    const e = fanout(24).map(s => (s as { entry: ToolCallEntry }).entry)
    e[6].output = '[exit 255]\nssh: timed out'
    e[18].output = '[exit 255]\nssh: timed out'
    expect(toolGroupSummary(e)).toMatchObject({ status: '2 of 24 failed · 22 ok', tone: 'failed' })
  })
})
