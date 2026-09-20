/**
 * Tests for the Repos tab's "code-graph MCP for large repos" nudge: it shows when
 * AI is on, is dismissible (persisted to localStorage), and is absent when AI is
 * off (there's no agent to benefit, and the MCP tab it points at is hidden).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ReposTab } from './SettingsDrawer'
import { FeaturesContext } from '../features'

const { getRepos, listCredentials, getSyncConfig } = vi.hoisted(() => ({
  getRepos: vi.fn(),
  listCredentials: vi.fn(),
  getSyncConfig: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'get_repos') return getRepos
        if (prop === 'list_credentials') return listCredentials
        if (prop === 'get_sync_config') return getSyncConfig
        return vi.fn()
      },
    },
  ),
}))

const NOTE = /code-graph MCP can speed the agent up/i

describe('ReposTab — code-graph MCP note', () => {
  beforeEach(() => {
    getRepos.mockReset().mockResolvedValue([])
    listCredentials.mockReset().mockResolvedValue([])
    getSyncConfig.mockReset().mockResolvedValue({ engine: true })
    try { localStorage.clear() } catch { /* ignore */ }
  })

  it('shows the note when AI is on, and dismissing it persists', async () => {
    render(<ReposTab />)
    await screen.findByText(NOTE)
    const user = userEvent.setup()
    // Ant Alert close button.
    await act(async () => { await user.click(document.querySelector('.ant-alert-close-icon') as Element) })
    await waitFor(() => expect(screen.queryByText(NOTE)).toBeNull())
    expect(localStorage.getItem('repos:graph-mcp-note-dismissed')).toBe('1')
  })

  it('does not show the note when already dismissed', async () => {
    localStorage.setItem('repos:graph-mcp-note-dismissed', '1')
    render(<ReposTab />)
    await waitFor(() => expect(getRepos).toHaveBeenCalled())
    expect(screen.queryByText(NOTE)).toBeNull()
  })

  it('does not show the note when AI is off', async () => {
    render(
      <FeaturesContext.Provider value={{ ai: false }}>
        <ReposTab />
      </FeaturesContext.Provider>,
    )
    await waitFor(() => expect(getRepos).toHaveBeenCalled())
    expect(screen.queryByText(NOTE)).toBeNull()
  })
})
