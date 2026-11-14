/**
 * The run inbox's Stop asks first, like the Console's: Keep running backs out,
 * Stop run stops it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RunInbox } from './RunInbox'
import type { RunCard } from '../bridge'

const CARD: RunCard = {
  card_id: 'c1', trigger: 'nightly-backup', source: 'schedule', event_type: 'fire',
  summary: 'backup', column: 'running', detail: {}, created_at: Date.now() / 1000,
}

describe('RunInbox', () => {
  it('asks before stopping a run', async () => {
    const onStop = vi.fn()
    const user = userEvent.setup()
    render(<RunInbox cards={{ c1: CARD }} selected="c1" onSelect={() => {}} onStop={onStop} />)
    await act(async () => { await user.click(await screen.findByRole('button', { name: /Stop/ })) })
    expect(onStop).not.toHaveBeenCalled()
    expect((await screen.findByTestId('risk-action')).textContent).toContain('nightly-backup')
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Keep running' })) })
    expect(onStop).not.toHaveBeenCalled()

    await act(async () => { await user.click(screen.getByRole('button', { name: /Stop/ })) })
    await act(async () => { await user.click(await screen.findByRole('button', { name: 'Stop run' })) })
    await waitFor(() => expect(onStop).toHaveBeenCalledWith('c1'))
  })
})
