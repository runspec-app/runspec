/**
 * Run cards show the args they were called with — a one-line strip under the
 * header, one arg per line after a click — so several runs of one tool (an
 * MCP client calling `jira get-issue` ten times, a Forms run repeated) can be
 * told apart. Secrets are masked.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { OutputPanel, MASKED_ARG, callArgsSummary, maskCallArgs } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

function runBlock(over: Partial<InvocationBlock> = {}): InvocationBlock {
  return {
    id: 'r1', sessionId: 's1', type: 'run', label: '/jira get-issue on local',
    startedAt: new Date().toISOString(), lines: [{ id: 'r1', line: '{}', stream: 'stdout' }],
    segments: [], currentText: '', done: true, exitCode: 0, ...over,
  }
}

describe('maskCallArgs', () => {
  it('masks password-typed and secret-looking names, drops empty values', () => {
    expect(maskCallArgs(
      { issue: 'OPS-1', 'jira-pat': 'abc', login: 'hunter2', api_key: 'k', limit: 5, note: '', tags: [], gone: null },
      ['login'],
    )).toEqual({ issue: 'OPS-1', 'jira-pat': MASKED_ARG, login: MASKED_ARG, api_key: MASKED_ARG, limit: 5 })
  })

  it('handles missing args', () => {
    expect(maskCallArgs(undefined)).toEqual({})
  })
})

describe('callArgsSummary', () => {
  it('writes name=value, a bare name for a true flag, quotes strings with spaces', () => {
    expect(callArgsSummary({ issue: 'OPS-1', verbose: true, dry: false, jql: 'project = OPS', n: 3, ids: [1, 2] }))
      .toBe('issue=OPS-1 verbose dry=false jql="project = OPS" n=3 ids=[1,2]')
  })
})

describe('run card args strip', () => {
  it('shows the summary on one line and expands to one arg per line', () => {
    render(<OutputPanel blocks={[runBlock({ callArgs: { issue: 'OPS-1', body: 'line one\nline two' } })]} />)
    const strip = screen.getByTestId('run-args')
    expect(strip.textContent).toBe('issue=OPS-1 body="line one\\nline two"')
    fireEvent.click(strip)
    const rows = screen.getAllByTestId('run-arg')
    expect(rows.map(r => r.textContent)).toEqual(['issue = OPS-1', 'body = line one\nline two'])
    expect(strip.getAttribute('aria-expanded')).toBe('true')
  })

  it('stays visible when the card is collapsed', () => {
    render(<OutputPanel blocks={[runBlock({ callArgs: { issue: 'OPS-2' } })]} />)
    fireEvent.click(screen.getByTestId('card-title'))  // collapse
    expect(screen.getByTestId('run-args').textContent).toBe('issue=OPS-2')
  })

  it('is absent for a run with no args, and for chat cards', () => {
    render(<OutputPanel blocks={[
      runBlock(),
      runBlock({ id: 'r2', callArgs: {} }),
      { ...runBlock({ id: 'c1', callArgs: { x: 1 } }), type: 'chat', label: 'hi' },
    ]} />)
    expect(screen.queryByTestId('run-args')).toBeNull()
  })

  it('tags an MCP-server call as such, not as a trigger', () => {
    render(<OutputPanel blocks={[runBlock({ trigger: { name: 'jira', source: 'mcp' }, callArgs: { issue: 'OPS-3' } })]} />)
    expect(screen.getByText('MCP call')).toBeTruthy()
    expect(screen.queryByText(/trigger: jira/)).toBeNull()
  })
})
