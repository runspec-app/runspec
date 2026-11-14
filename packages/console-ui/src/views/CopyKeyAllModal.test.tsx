/**
 * Regression test for the bulk "Copy SSH key to all hosts" dialog: the password
 * field must survive a parent re-render. The parent (SettingsDrawer) recomputes
 * the `hosts` prop into a fresh array on every render — connection-status polling
 * or a host flipping connected after a failed first attempt — and the dialog used
 * to reset on every `hosts` identity change, wiping the password mid-typing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { CopyKeyAllModal, type BulkKeyHost } from './CopyKeyAllModal'

const { copySshIdAll } = vi.hoisted(() => ({ copySshIdAll: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    { get: (_t, prop) => (prop === 'copy_ssh_id_all' ? copySshIdAll : vi.fn()) },
  ),
}))

// A fresh array with the same contents — mimics the parent recomputing
// `allRemoteHosts.map(...)` on each of its renders.
const mkHosts = (): BulkKeyHost[] => [
  { name: 'prod-1', connected: false },
  { name: 'prod-2', connected: true },
]

describe('CopyKeyAllModal', () => {
  beforeEach(() => {
    copySshIdAll.mockReset().mockResolvedValue({ ok: true, message: 'done', per_host: [] })
  })

  const pwd = () => screen.getByPlaceholderText('SSH password') as HTMLInputElement

  it('keeps the typed password when the parent re-renders with a new hosts array', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <CopyKeyAllModal open hosts={mkHosts()} onClose={() => {}} />,
    )

    await user.type(pwd(), 'hunter2')
    expect(pwd().value).toBe('hunter2')

    // Parent re-renders (e.g. status poll) — new `hosts` reference, same contents.
    rerender(<CopyKeyAllModal open hosts={mkHosts()} onClose={() => {}} />)

    // The password must NOT have been cleared by the reset effect.
    expect(pwd().value).toBe('hunter2')
  })

  it('filters, picks all shown, and Enter in the password does not fire', async () => {
    const user = userEvent.setup()
    const hosts: BulkKeyHost[] = [
      { name: 'web-1', connected: true }, { name: 'web-2', connected: true }, { name: 'db-1', connected: false },
    ]
    render(<CopyKeyAllModal open hosts={hosts} onClose={() => {}} />)
    expect(screen.getByText('1 of 3 selected')).toBeTruthy()
    await user.type(screen.getByLabelText('Filter hosts'), 'web')
    await user.click(screen.getByRole('button', { name: 'All shown (2)' }))
    expect(screen.getByText('3 of 3 selected')).toBeTruthy()
    await user.type(pwd(), 'hunter2{Enter}')
    expect(copySshIdAll).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Copy to 3 hosts' }))
    expect(copySshIdAll).toHaveBeenCalledWith('hunter2', ['db-1', 'web-1', 'web-2'])
  })

  it('resets the password when the dialog is closed and reopened', async () => {
    const user = userEvent.setup()
    const hosts = mkHosts()
    const { rerender } = render(
      <CopyKeyAllModal open hosts={hosts} onClose={() => {}} />,
    )

    await user.type(pwd(), 'hunter2')
    expect(pwd().value).toBe('hunter2')

    // Close, then reopen — a genuine open transition clears the field.
    rerender(<CopyKeyAllModal open={false} hosts={hosts} onClose={() => {}} />)
    rerender(<CopyKeyAllModal open hosts={hosts} onClose={() => {}} />)

    expect(pwd().value).toBe('')
  })
})
