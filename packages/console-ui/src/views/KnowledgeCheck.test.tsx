import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { WikiCheckPanel, checkSummary } from './KnowledgeCheck'
import type { WikiCheck } from '../bridge'

const check: WikiCheck = {
  summary: { clash: 1, stale: 0, duplicate: 0, gap: 2 },
  findings: [
    { kind: 'clash', entity: 'runnable:cron', path: 'runnables/cron.md', flags: ['line'], message: "runnables/cron.md mentions --line, which cron's runspec.toml doesn't have." },
    { kind: 'gap', entity: '', path: '', names: ['df', 'tail-log'], message: '2 installed tools have no page.' },
  ],
}

describe('WikiCheckPanel', () => {
  it('summarises, opens to the findings, and opens a page or a tool', () => {
    expect(checkSummary(check)).toBe('1 out of date · 2 without a page')
    const open = vi.fn()
    render(<WikiCheckPanel check={check} onOpen={open} />)
    expect(screen.queryByText(/mentions --line/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Wiki check/ }))
    expect(screen.getByText(/mentions --line/)).toBeTruthy()
    fireEvent.click(screen.getByText('Open page'))
    expect(open).toHaveBeenCalledWith('runnable:cron')
    fireEvent.click(screen.getByText('tail-log'))
    expect(open).toHaveBeenCalledWith('tail-log')
  })

  it('says so when nothing needs a look', () => {
    render(<WikiCheckPanel check={{ summary: { clash: 0, stale: 0, duplicate: 0, gap: 0 }, findings: [] }} onOpen={() => {}} />)
    expect(screen.getByTestId('wiki-check').textContent).toContain('nothing needs a look')
  })
})
