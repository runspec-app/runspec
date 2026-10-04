/**
 * KnowledgeSync: the Knowledge tab's Memory Sync buttons, and the lane name the
 * settings form previews.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { KnowledgeSync } from './KnowledgeSync'
import { laneOf } from '../components/MemorySyncTab'

const { status, push, pull } = vi.hoisted(() => ({
  status: vi.fn(),
  push: vi.fn(),
  pull: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'memory_sync_status') return status
      if (p === 'memory_sync_push') return push
      if (p === 'memory_sync_pull') return pull
      return vi.fn(async () => ({ ok: true }))
    },
  }),
}))

const base = {
  configured: true, engine: 'dulwich', url: 'u', branch: 'main', contrib_branch: 'contrib/jo',
  path: 'facts/jo.toml', shareable: 2, unpushed: true, team_facts: 0,
  last_push: '', last_push_commit: '', last_pull: '', last_pull_commit: '',
}

beforeEach(() => { status.mockReset(); push.mockReset(); pull.mockReset() })

describe('KnowledgeSync', () => {
  it('offers set-up when Memory Sync is not configured', async () => {
    status.mockResolvedValue({ ...base, configured: false })
    const open = vi.fn()
    render(<KnowledgeSync onOpenSettings={open} />)
    fireEvent.click(await screen.findByText('Share with team'))
    expect(open).toHaveBeenCalled()
  })

  it('pulls, and pushes after confirming', async () => {
    status.mockResolvedValue(base)
    pull.mockResolvedValue({ ok: true, files: 1, facts: 3, added: 3, removed: 0, skipped: [] })
    push.mockResolvedValue({ ok: true, branch: 'contrib/jo', changed: true, facts: 2 })
    render(<KnowledgeSync />)
    fireEvent.click(await screen.findByText('Pull'))
    await waitFor(() => expect(pull).toHaveBeenCalled())
    fireEvent.click(screen.getByText('Push'))
    expect(await screen.findByText('Push 2 approved fact(s)?')).toBeTruthy()
    expect(push).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Push' }))
    await waitFor(() => expect(push).toHaveBeenCalled())
  })
})

describe('laneOf', () => {
  it('matches the backend lane_name', () => {
    expect(laneOf('Jason.Finestone')).toBe('jason.finestone')
    expect(laneOf('DOMAIN\\jason')).toBe('domain-jason')
    expect(laneOf('  ')).toBe('user')
  })
})
