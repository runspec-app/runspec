/**
 * Settings → Mail tab first-run experience. A fresh install (no [mailbox], no
 * [[watch]]) must make the *primary* "Monitor a mailbox" step discoverable and
 * keep the optional "Also watch" extras from reading as the way to add a
 * mailbox — the confusion reported when the only visible action was the extras'
 * "+ Add" button.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { MailboxesTab } from './SettingsDrawer'

const { getMailbox, getWatches, configDir, saveMailbox, saveWatches } = vi.hoisted(() => ({
  getMailbox: vi.fn(),
  getWatches: vi.fn(),
  configDir: vi.fn(),
  saveMailbox: vi.fn(),
  saveWatches: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_mailbox') return getMailbox
      if (prop === 'get_watches') return getWatches
      if (prop === 'config_dir') return configDir
      if (prop === 'save_mailbox') return saveMailbox
      if (prop === 'save_watches') return saveWatches
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

beforeEach(() => {
  getMailbox.mockReset().mockResolvedValue(null)
  getWatches.mockReset().mockResolvedValue([])
  configDir.mockReset().mockResolvedValue('%APPDATA%/runspec-console')
  saveMailbox.mockReset().mockResolvedValue(undefined)
  saveWatches.mockReset().mockResolvedValue(undefined)
})

describe('Settings → Mail (first run)', () => {
  it('guides the operator to the primary mailbox when nothing is configured', async () => {
    render(<MailboxesTab />)

    // The primary step is discoverable even while its form is collapsed.
    expect(await screen.findByText(/Start here/)).toBeTruthy()
    // The primary form is hidden until the master switch is on.
    expect(screen.queryByLabelText('Mailbox email address')).toBeNull()
    // The extras list shows an empty state that points back to the primary step…
    expect(screen.getByText(/No extra mailboxes/)).toBeTruthy()
    // …and its button reads as an *extra*, not the way to add a mailbox.
    expect(screen.getByText('+ Add extra mailbox')).toBeTruthy()
    expect(screen.queryByText('+ Add mailbox')).toBeNull()
  })

  it('reveals the primary mailbox form and drops the hint when enabled', async () => {
    render(<MailboxesTab />)
    await screen.findByText(/Start here/)

    fireEvent.click(screen.getByRole('switch'))

    expect(await screen.findByLabelText('Mailbox email address')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText(/Start here/)).toBeNull())
  })

  it('drops the empty state once an extra mailbox is added', async () => {
    render(<MailboxesTab />)
    await screen.findByText(/No extra mailboxes/)

    fireEvent.click(screen.getByText('+ Add extra mailbox'))

    await waitFor(() => expect(screen.queryByText(/No extra mailboxes/)).toBeNull())
  })
})
