/**
 * A run card (Forms run, MCP call, rule trigger) gets the structured view and
 * the Table/Raw toggle even when its JSON has other lines around it — log lines
 * printed before the result, a warning on stderr. Those lines show above the
 * table instead of the card falling back to plain JSON text.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { OutputPanel, StructuredOutput, EXTRA_LINES_SHOWN } from './OutputPanel'
import type { InvocationBlock, OutputLine } from './OutputPanel'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

function runBlock(lines: OutputLine[]): InvocationBlock {
  return {
    id: 'r1', sessionId: 's1', type: 'run', label: '/gitlab whoami on local',
    startedAt: new Date().toISOString(), lines,
    segments: [], currentText: '', done: true, exitCode: 0,
  }
}

const stdout = (line: string): OutputLine => ({ id: 'r1', line, stream: 'stdout' })

describe('run card with log lines before its JSON', () => {
  it('shows the key-value view, the log line above it, and a Raw toggle', () => {
    const { container } = render(<OutputPanel blocks={[runBlock([
      stdout('HTTP Request: GET https://gitlab.example.com/api/v4/user "HTTP/1.1 200 OK"'),
      stdout('{"username": "jason", "state": "active"}'),
    ])]} />)
    // Structured: the value renders in its own cell, not as raw JSON text.
    expect(screen.getByText('jason')).toBeTruthy()
    expect(container.textContent).not.toContain('"username"')
    const extra = container.querySelector('[data-rs-extra-lines]')
    expect(extra?.textContent).toContain('HTTP Request: GET')

    // Raw still shows the output exactly as printed.
    fireEvent.click(container.querySelector('.anticon-code')!.closest('button')!)
    expect(container.textContent).toContain('{"username": "jason", "state": "active"}')
    expect(container.querySelector('[data-rs-extra-lines]')).toBeNull()
  })

  it('shows no extra-lines strip for a clean JSON result', () => {
    const { container } = render(<OutputPanel blocks={[runBlock([stdout('{"a": 1}')])]} />)
    expect(container.querySelector('[data-rs-extra-lines]')).toBeNull()
  })
})

describe('ExtraLines cap', () => {
  it('caps the lines shown and points at Raw for the rest', () => {
    const logs = Array.from({ length: EXTRA_LINES_SHOWN + 3 }, (_, i) => `log line ${i}`)
    const { container } = render(<StructuredOutput text={[...logs, '[{"id": 1}, {"id": 2}]'].join('\n')} />)
    const extra = container.querySelector('[data-rs-extra-lines]')!
    expect(extra.textContent).toContain('log line 0')
    expect(extra.textContent).not.toContain(`log line ${EXTRA_LINES_SHOWN}`)
    expect(extra.textContent).toContain('3 more lines (Raw shows everything)')
  })
})
