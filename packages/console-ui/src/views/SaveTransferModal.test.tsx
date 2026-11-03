/**
 * Tests for the saved-transfer editor: adding a filter (with its value asked
 * for each run) and a templated ad-hoc host.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'

import { SaveTransferModal } from './SavedTransferModals'
import type { Host, SavedTransfer } from '../bridge'

const { saveSaved } = vi.hoisted(() => ({ saveSaved: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'save_saved_transfer') return saveSaved
        if (prop === 'get_filters') {
          return () => Promise.resolve([
            { name: 'id-lines', pipeline: 'grep -F {{arg}}', param_label: 'Transaction ID' },
          ])
        }
        if (prop === 'list_credentials') return () => Promise.resolve([])
        return vi.fn()
      },
    },
  ),
}))

const HOSTS = [{ name: 'local' }, { name: 'prod-1', group: 'Production' }] as Host[]

const pickFilter = async () => {
  const picker = screen.getByText('No filter').closest('.ant-select') as HTMLElement
  fireEvent.mouseDown(within(picker).getByRole('combobox'))
  fireEvent.click(await screen.findByTitle('id-lines'))
}

describe('SaveTransferModal', () => {
  beforeEach(() => { saveSaved.mockReset().mockResolvedValue({ ok: true, id: 'x' }) })

  it('adds a filter whose value is asked for each run', async () => {
    const initial: SavedTransfer = { id: 'app', host: 'prod-1', paths: ['/var/log/app.log'] }
    render(<SaveTransferModal initial={initial} hosts={HOSTS} onClose={vi.fn()} />)
    await pickFilter()
    fireEvent.change(await screen.findByPlaceholderText('Transaction ID'), { target: { value: 'T-42' } })
    fireEvent.click(screen.getByRole('button', { name: /Ask for “Transaction ID” each run/ }))
    expect((screen.getByPlaceholderText('Transaction ID') as HTMLInputElement).value).toBe('{{transaction_id}}')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveSaved).toHaveBeenCalled())
    const row = saveSaved.mock.calls[0][0]
    expect(row.filter).toBe('id-lines')
    expect(row.filter_arg).toBe('{{transaction_id}}')
    expect(row.paramsRaw).toEqual({ transaction_id: { default: 'T-42', description: 'Transaction ID' } })
  })

  it('offers a param for a templated ad-hoc host', async () => {
    const initial: SavedTransfer = { id: 'any', adhoc: 'db7', paths: ['/var/log/app.log'] }
    render(<SaveTransferModal initial={initial} hosts={HOSTS} onClose={vi.fn()} />)
    fireEvent.change(screen.getByDisplayValue('db7'), { target: { value: '{{host}}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add host' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveSaved).toHaveBeenCalled())
    const row = saveSaved.mock.calls[0][0]
    expect(row.adhoc).toBe('{{host}}')
    expect(row.paramsRaw).toEqual({ host: {} })
  })

  it('saves the file under a templated name', async () => {
    const initial: SavedTransfer = { id: 'orders', host: 'prod-1', paths: ['/var/log/app.log'] }
    render(<SaveTransferModal initial={initial} hosts={HOSTS} onClose={vi.fn()} />)
    const saveAs = screen.getByLabelText('save as')
    fireEvent.change(saveAs, { target: { value: 'logs/x.log' } })
    expect(screen.getByText(/A file name only, no folder/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(saveAs, { target: { value: 'orders-{{txn}}.log' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add txn' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveSaved).toHaveBeenCalled())
    const row = saveSaved.mock.calls[0][0]
    expect(row.dest_name).toBe('orders-{{txn}}.log')
    expect(row.paramsRaw).toEqual({ txn: {} })
  })
})
