/**
 * RelayModal — Jump host is deliberately left blank on open in ad-hoc mode.
 *
 * Regression for the reported bug: auto-defaulting Jump to the first fleet
 * host silently used one that may refuse SSH port-forwarding
 * (`AllowTcpForwarding no`), so the first Browse attempt failed with a
 * paramiko `ChannelException(1, 'Administratively prohibited')` before the
 * target credentials were consulted. Force an explicit pick — Browse stays
 * disabled until the operator chooses a jump.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RelayModal } from './RelayModal'
import type { Host } from '../bridge'

const { listCredentials } = vi.hoisted(() => ({ listCredentials: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'list_credentials') return listCredentials
      // Every other bridge method the modal or its children may call (e.g.
      // FilterPicker's get_filters) — return a resolved promise so `.then()`
      // chains never blow up in the mount effect.
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

describe('RelayModal ad-hoc jump default', () => {
  it('leaves Jump host blank on open so the operator picks one', async () => {
    render(<RelayModal open onClose={() => {}} hosts={HOSTS} />)
    // Ad-hoc is the default mode; the Jump host Select renders its placeholder
    // rather than a preselected value. Destination host still auto-picks (its
    // path uses the pool, not a direct-tcpip forward, so the bug doesn't
    // apply) — so exactly one "bastion-a" selection-item is expected, from
    // the destination Select.
    expect(await screen.findByText('Jump host')).toBeTruthy()
    expect(screen.queryAllByTitle('bastion-a')).toHaveLength(1)
  })

  it('keeps Browse disabled until Jump host is chosen (even after target typed)', async () => {
    render(<RelayModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('Jump host')
    const user = userEvent.setup()
    // Type a target hostname — the *other* precondition for Browse.
    const target = screen.getByPlaceholderText('target hostname')
    await act(async () => { await user.type(target, 'db.internal') })
    const browse = screen.getByRole('button', { name: /browse/i })
    expect(browse.hasAttribute('disabled')).toBe(true)
  })
})
