/**
 * Running a saved transfer: a pull starts straight away; a push or relay
 * (which can replace files on hosts) lists every file and target first, the
 * risk ladder's fleet confirmation (components/risk.tsx).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { SavedTransferRunModal } from './SavedTransferModals'
import type { SavedTransfer } from '../bridge'

const { api } = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p: string) => (api[p] ??= vi.fn(() => Promise.resolve({}))) }),
}))

const PULL: SavedTransfer = {
  id: 'app-logs', paths: ['/var/log/app/*.log'], source: 'prod-1', dest: 'workspace', direction: 'pull',
}
const PUSH: SavedTransfer = {
  id: 'push-config', paths: ['config/*.yaml'], host: 'local', to_group: 'Production',
  source: 'this computer', dest: 'group Production:/srv/app/config', direction: 'push',
}

beforeEach(() => {
  for (const k of Object.keys(api)) delete api[k]
  api.run_saved_transfer = vi.fn(() => Promise.resolve({ id: 'run-1', dest: '/srv/app/config' }))
  api.preview_saved_transfer = vi.fn(() => Promise.resolve({
    direction: 'push', targets: ['prod-1', 'prod-2'],
    sources: [{ host: 'local', unmatched: [], files: [
      { path: 'config/app.yaml', size: 10, mtime: 0, dest: '/srv/app/config/app.yaml' },
    ] }],
  }))
})

describe('SavedTransferRunModal', () => {
  it('runs a pull without asking', async () => {
    render(<SavedTransferRunModal entry={PULL} hostNames={['prod-1']} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    await waitFor(() => expect(api.run_saved_transfer).toHaveBeenCalledTimes(1))
    expect(api.preview_saved_transfer).not.toHaveBeenCalled()
  })

  it('shows every file and host before a push, and Cancel stops it', async () => {
    render(<SavedTransferRunModal entry={PUSH} hostNames={['prod-1', 'prod-2']} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    const action = await screen.findByTestId('risk-action')
    expect(action.textContent).toContain('local:config/app.yaml  →  /srv/app/config/app.yaml')
    expect(screen.getByText('prod-1, prod-2')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('risk-action')).toBeNull())
    expect(api.run_saved_transfer).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    fireEvent.click(await screen.findByTestId('risk-ok'))
    await waitFor(() => expect(api.run_saved_transfer).toHaveBeenCalledTimes(1))
  })
})
