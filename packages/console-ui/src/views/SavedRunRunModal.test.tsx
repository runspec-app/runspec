/**
 * The saved-run dialog waits for any confirmation behind Run: cancelled, it
 * stays open and says nothing started; confirmed, it closes.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../bridge', () => ({ bridge: new Proxy({}, { get: () => vi.fn(() => Promise.resolve([])) }) }))

import { SavedRunRunModal } from './SavedRunRunModal'
import type { SavedRun } from '../bridge'

const RUN: SavedRun = {
  id: 'restart-web', title: 'Restart web tier', description: '', runnable: 'service-restart', command: [],
  host: '', group: 'Production', hosts: [], selected_hosts: [], args: {}, credential_ids: [],
  params: [{ name: 'service', type: 'str', required: false, default: 'nginx' } as unknown as SavedRun['params'][number]],
}

describe('SavedRunRunModal', () => {
  it('stays open when the run is cancelled at its confirmation', async () => {
    const onClose = vi.fn()
    const onRun = vi.fn(() => Promise.resolve(false))
    render(<SavedRunRunModal savedRun={RUN} hostNames={[]} onClose={onClose} onRun={onRun} />)
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))
    await waitFor(() => expect(onRun).toHaveBeenCalled())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes once the run starts', async () => {
    const onClose = vi.fn()
    render(<SavedRunRunModal savedRun={RUN} hostNames={[]} onClose={onClose} onRun={() => Promise.resolve(true)} />)
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})
