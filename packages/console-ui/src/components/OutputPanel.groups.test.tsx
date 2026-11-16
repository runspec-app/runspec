/**
 * A fan-out turn — the agent calling one tool on 24 hosts — reads as one row
 * that says how it went and opens to each call with its host.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { ChatTranscript, type BlockSegment } from './OutputPanel'

vi.mock('../bridge', () => ({ bridge: new Proxy({}, { get: () => vi.fn() }) }))

const fanout: BlockSegment[] = Array.from({ length: 24 }, (_, i) => ({
  kind: 'tool',
  entry: { name: `web-${String(i + 1).padStart(2, '0')}__system-info`, input: { detail: 'summary' }, running: false, durationMs: 37 + i },
}))

describe('ChatTranscript fan-out groups', () => {
  it('shows 24 identical host calls as one row, and each call with its host when opened', () => {
    render(<ChatTranscript segments={[{ kind: 'text', text: 'Collecting.' }, ...fanout]} currentText="" done />)
    const group = screen.getByTestId('tool-group')
    expect(screen.getByTestId('tool-group-where').textContent).toBe('on 24 hosts')
    expect(screen.getByTestId('tool-group-status').textContent).toBe('all done')
    expect(group.textContent).toContain('37ms–60ms')
    expect(screen.queryAllByTestId('tool-host')).toHaveLength(0)
    const toggle = screen.getByRole('button', { name: /system-info on 24 hosts, all done/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const hosts = screen.getAllByTestId('tool-host').map(h => h.textContent)
    expect(hosts).toHaveLength(24)
    expect(hosts[0]).toBe('on web-01')
  })
})
