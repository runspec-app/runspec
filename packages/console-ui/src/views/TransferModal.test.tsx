/**
 * TransferModal — the unified download / upload / fan-out / relay dialog.
 *
 * Ad-hoc endpoints only require a target hostname; the jump host is optional
 * (blank ⇒ the console connects to the target directly). A jump is added only
 * when the console can't reach the target directly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TransferModal } from './TransferModal'
import type { Host } from '../bridge'

const { listCredentials } = vi.hoisted(() => ({ listCredentials: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'list_credentials') return listCredentials
      // Any other bridge method the modal or its children may call — resolve so
      // `.then()` chains in mount effects never throw.
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

const HOSTS: Host[] = [
  { name: 'bastion-a', connected: true },
  { name: 'bastion-b', connected: true },
] as unknown as Host[]

beforeEach(() => {
  listCredentials.mockReset().mockResolvedValue([])
})

// Open the source-side "kind" Select (the first combobox in the Source row —
// the Saved row above it has its own) and choose an option.
async function pickSourceKind(user: ReturnType<typeof userEvent.setup>, label: string) {
  const row = screen.getByText('Source').parentElement as HTMLElement
  const combos = within(row).getAllByRole('combobox')
  await act(async () => { await user.click(combos[0]) })
  const option = await screen.findByText(label, { selector: '.ant-select-item-option-content' })
  await act(async () => { await user.click(option) })
}

describe('TransferModal', () => {
  it('renders the unified title and defaults to a configured-host source', async () => {
    render(<TransferModal open onClose={() => {}} hosts={HOSTS} />)
    expect(await screen.findByText('Transfer files')).toBeTruthy()
    // A configured-host source auto-picks the first connected host.
    expect(screen.queryAllByTitle('bastion-a').length).toBeGreaterThan(0)
  })

  it('reveals ad-hoc fields with an optional (blank) jump, and a target alone is enough', async () => {
    render(<TransferModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('Transfer files')
    const user = userEvent.setup()
    await pickSourceKind(user, 'Ad-hoc host')

    // The ad-hoc fields appear; the jump host is optional (placeholder, no value).
    const target = await screen.findByPlaceholderText('target hostname')
    expect(screen.getByText('jump host (optional)')).toBeTruthy()

    // Choosing files stays disabled with no target…
    expect(screen.getByRole('button', { name: /choose files/i }).hasAttribute('disabled')).toBe(true)
    // …and becomes enabled once a target is typed — no jump required (jumpless).
    await act(async () => { await user.type(target, 'db.internal') })
    expect(screen.getByRole('button', { name: /choose files/i }).hasAttribute('disabled')).toBe(false)
  })
})
