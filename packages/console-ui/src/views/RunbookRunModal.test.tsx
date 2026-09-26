/**
 * RunbookRunModal: params lay out like the Forms tab — a multi-column grid, each
 * description on its own truncated line (not squeezed beside the name), defaulted
 * params folded into "Default arguments", and a Forms-width dialog.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RunbookRunModal, paramModalWidth } from './RunbookRunModal'
import type { Runbook } from '../bridge'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

const LONG = 'The host whose kernel packages to inspect — pick one from the fleet; the runbook lists and removes only packages for kernels older than the running one.'

function runbook(): Runbook {
  return {
    id: 'kernel-cleanup',
    title: 'Kernel cleanup',
    symptom: '/boot is filling up',
    steps: [{ text: 'do it' }],
    tags: [],
    args: [
      { name: 'host', type: 'str', required: true, ui: 'host', description: LONG },
      { name: 'keep', type: 'int', required: true, description: 'How many old kernels to keep' },
      { name: 'purge', type: 'flag', required: false, default: true, description: 'Also purge config files' },
    ],
  }
}

describe('RunbookRunModal layout', () => {
  it('lays params out in the Forms grid with descriptions on their own line', () => {
    render(<RunbookRunModal runbook={runbook()} hostNames={['web-01']} onClose={() => {}} onRun={() => {}} />)
    const desc = screen.getByText(LONG)
    // Truncated (ellipsis) rather than wrapping beside the name.
    expect(desc.style.whiteSpace).toBe('nowrap')
    expect(desc.style.textOverflow).toBe('ellipsis')
    // Required params sit in a CSS grid.
    const grid = screen.getByText('keep').closest('div[style*="grid"]') as HTMLElement
    expect(grid).toBeTruthy()
    expect(grid.style.gridTemplateColumns).toContain('minmax(260px')
  })

  it('folds defaulted params into "Default arguments"', () => {
    render(<RunbookRunModal runbook={runbook()} hostNames={['web-01']} onClose={() => {}} onRun={() => {}} />)
    expect(screen.getByText('Default arguments (1)')).toBeTruthy()
  })

  it('still validates required params', async () => {
    const onRun = vi.fn()
    render(<RunbookRunModal runbook={runbook()} hostNames={['web-01']} onClose={() => {}} onRun={onRun} />)
    await userEvent.click(screen.getByRole('button', { name: 'Run runbook' }))
    expect(onRun).not.toHaveBeenCalled()
    expect(screen.getAllByText('Required').length).toBe(2)
  })
})

describe('paramModalWidth', () => {
  it('matches the Forms dialog widths', () => {
    expect(paramModalWidth(3)).toBe('min(860px, 94vw)')
    expect(paramModalWidth(7)).toBe('min(1160px, 96vw)')
  })
})
