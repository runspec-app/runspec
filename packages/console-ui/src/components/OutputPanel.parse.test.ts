import { describe, it, expect } from 'vitest'
import { parseOutput, _humanVal } from './OutputPanel'
import type { OutputLine } from './OutputPanel'

const out = (s: string): OutputLine[] => [{ id: '1', line: s, stream: 'stdout' }]

describe('parseOutput', () => {
  it('classifies an array of objects as a table', () => {
    const p = parseOutput(out(JSON.stringify([{ a: 1 }, { a: 2 }])))
    expect(p.kind).toBe('table')
    expect(p.rows).toHaveLength(2)
  })

  it('classifies a dict of arrays-of-objects as grouped sections', () => {
    // shape recent-errors used to emit: {System:[…], Application:[…]}
    const p = parseOutput(out(JSON.stringify({ System: [{ id: 1 }], Application: [{ id: 2 }] })))
    expect(p.kind).toBe('sections')
    expect(p.sections?.map(s => s.title)).toEqual(['System', 'Application'])
    expect(p.sections?.[0].rows).toEqual([{ id: 1 }])
  })

  it('classifies metadata + one row array as a captioned section', () => {
    // shape query-eventlog used to emit: {log, level, events:[…]}
    const p = parseOutput(out(JSON.stringify({ log: 'System', level: 'error', events: [{ id: 7000 }] })))
    expect(p.kind).toBe('sections')
    expect(p.meta).toEqual({ log: 'System', level: 'error' })
    expect(p.sections).toHaveLength(1)
    expect(p.sections?.[0].title).toBe('events')
    expect(p.sections?.[0].rows).toEqual([{ id: 7000 }])
  })

  it('treats empty arrays as empty sections', () => {
    const p = parseOutput(out(JSON.stringify({ System: [], Application: [] })))
    expect(p.kind).toBe('sections')
    expect(p.sections?.every(s => s.rows.length === 0)).toBe(true)
  })

  it('falls back to object for a plain scalar dict', () => {
    const p = parseOutput(out(JSON.stringify({ hostname: 'box', uptime_seconds: 10 })))
    expect(p.kind).toBe('object')
  })

  it('falls back to object when a value is a nested non-array object', () => {
    const p = parseOutput(out(JSON.stringify({ meta: { a: 1 }, rows: [{ x: 1 }] })))
    expect(p.kind).toBe('object')
  })

  it('returns raw for empty stdout and non-JSON', () => {
    expect(parseOutput(out('')).kind).toBe('raw')
    expect(parseOutput(out('hello world')).kind).toBe('raw')
  })
})

describe('_humanVal', () => {
  it('never returns [object Object] for nested objects', () => {
    const s = _humanVal('x', { a: 1 })
    expect(s).not.toContain('[object Object]')
    expect(s).toContain('"a"')
  })

  it('joins arrays of scalars', () => {
    expect(_humanVal('tags', ['a', 'b', 'c'])).toBe('a, b, c')
  })

  it('compacts arrays of objects to JSON, not [object Object]', () => {
    const s = _humanVal('items', [{ a: 1 }])
    expect(s).not.toContain('[object Object]')
    expect(s.startsWith('[')).toBe(true)
  })

  it('still formats unit-suffixed numbers', () => {
    expect(_humanVal('free_mb', 2048)).toBe('2.0 GB')
    expect(_humanVal('uptime_seconds', 3600)).toBe('1.0h')
  })

  it('truncates very long JSON with an ellipsis', () => {
    expect(_humanVal('blob', { data: 'x'.repeat(500) }).endsWith('…')).toBe(true)
  })
})
