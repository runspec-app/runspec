/**
 * Tests for the saved-transfers list (Transfer dialog → Manage…): search,
 * run / edit / duplicate / new hand-offs, and delete through the bridge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { SavedTransfersListModal } from './SavedTransferModals'
import type { SavedTransfer } from '../bridge'

const { deleteSaved } = vi.hoisted(() => ({ deleteSaved: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    { get: (_t, prop) => (prop === 'delete_saved_transfer' ? deleteSaved : vi.fn()) },
  ),
}))

const SAVED: SavedTransfer[] = [
  {
    id: 'app-logs', title: 'App logs for a day', paths: ['/var/log/app/*.log'],
    source: 'prod-1', dest: 'chat workspace', direction: 'pull', autonomy: 'autonomous',
    params: [{ name: 'day', type: 'str', required: false, default: 'yesterday' }],
  },
  {
    id: 'push-config', paths: ['config/*.yaml'], host: 'local', to_group: 'Production',
    source: 'this computer', dest: 'group Production:/srv/app/config', direction: 'push',
  },
]

describe('SavedTransfersListModal', () => {
  beforeEach(() => { deleteSaved.mockReset().mockResolvedValue({ ok: true }) })

  const setup = () => {
    const props = { onClose: vi.fn(), onRun: vi.fn(), onEdit: vi.fn(), onNew: vi.fn() }
    render(<SavedTransfersListModal saved={SAVED} {...props} />)
    return props
  }

  it('lists every saved transfer with where it copies and filters by search', async () => {
    setup()
    expect(await screen.findByText('App logs for a day')).toBeTruthy()
    expect(screen.getByText('push-config')).toBeTruthy()
    expect(screen.getByText(/group Production:\/srv\/app\/config/)).toBeTruthy()
    expect(screen.getByText('asks first')).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText('Search by name, host or path'), { target: { value: 'srv/app' } })
    expect(screen.queryByText('App logs for a day')).toBeNull()
    expect(screen.getByText('push-config')).toBeTruthy()
  })

  it('hands run, edit, duplicate and new to the dialog', async () => {
    const props = setup()
    fireEvent.click(await screen.findByLabelText('Run app-logs'))
    expect(props.onRun).toHaveBeenCalledWith(SAVED[0])
    fireEvent.click(screen.getByLabelText('Edit push-config'))
    expect(props.onEdit).toHaveBeenLastCalledWith(SAVED[1], 'push-config')
    fireEvent.click(screen.getByLabelText('Duplicate app-logs'))
    expect(props.onEdit).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: 'app-logs-copy', title: 'App logs for a day (copy)' }),
    )
    expect(props.onEdit.mock.calls.at(-1)?.[1]).toBeUndefined()
    fireEvent.click(screen.getByText('New…'))
    expect(props.onNew).toHaveBeenCalled()
  })

  it('deletes through the bridge after confirming', async () => {
    setup()
    fireEvent.click(await screen.findByLabelText('Delete push-config'))
    fireEvent.click(await screen.findByText('OK'))
    await waitFor(() => expect(deleteSaved).toHaveBeenCalledWith('push-config'))
  })
})
