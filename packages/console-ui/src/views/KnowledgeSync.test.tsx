/**
 * KnowledgeSync: the Knowledge tab's Memory Sync buttons, and the lane name the
 * settings form previews.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { KnowledgeSync, SyncResultBanner, pullTip } from './KnowledgeSync'
import { errorText, laneOf, type SyncResult } from '../components/MemorySyncTab'

// The tab shows the outcome on its own line, under the toolbar.
function Harness() {
  const [result, setResult] = useState<SyncResult | null>(null)
  return (
    <>
      <KnowledgeSync onResult={setResult} />
      <SyncResultBanner result={result} onClose={() => setResult(null)} />
    </>
  )
}

const { status, push, pull, preview } = vi.hoisted(() => ({
  status: vi.fn(),
  push: vi.fn(),
  pull: vi.fn(),
  preview: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'memory_sync_status') return status
      if (p === 'memory_sync_push') return push
      if (p === 'memory_sync_pull') return pull
      if (p === 'memory_sync_preview') return preview
      return vi.fn(async () => ({ ok: true }))
    },
  }),
}))

const base = {
  configured: true, engine: 'dulwich', url: 'u', branch: 'main', pull_from: 'branch' as const, pull_source: 'main', contrib_branch: 'contrib/jo',
  path: 'facts/jo.toml', shareable: 2, unpushed: true, team_facts: 0,
  last_push: '', last_push_commit: '', last_pull: '', last_pull_commit: '', last_pull_ref: '',
}

const shareTwo = {
  ok: true, lane: 'jo', branch: 'contrib/jo', path: 'facts/jo.toml',
  share: [
    { id: 'k_1', kind: 'note', about: ['runnable:tail-log'], text: 'tail-log follows http-check' },
    { id: 'k_2', kind: 'glossary', about: ['term:BAU'], text: 'BAU means business as usual' },
  ],
  kept: [
    { id: 'k_3', kind: 'host', about: ['host:web-01'], text: 'web-01 is the orders primary', reason: 'it is about host:web-01, which each console sets up itself' },
  ],
}

beforeEach(() => {
  status.mockReset(); push.mockReset(); pull.mockReset(); preview.mockReset()
  preview.mockResolvedValue(shareTwo)
})

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
    render(<Harness />)
    fireEvent.click(await screen.findByText('Pull'))
    await waitFor(() => expect(pull).toHaveBeenCalled())
    fireEvent.click(screen.getByText('Push'))
    // The confirmation lists what leaves this console and what stays.
    expect(await screen.findByText('Push 2 facts to contrib/jo?')).toBeTruthy()
    expect(screen.getByText('tail-log follows http-check')).toBeTruthy()
    expect(screen.getByText('Stays on this console (1 fact)')).toBeTruthy()
    expect(screen.getByText('web-01 is the orders primary')).toBeTruthy()
    expect(push).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Push 2 facts' }))
    await waitFor(() => expect(push).toHaveBeenCalled())
    expect(await screen.findByText(/Pushed 2 facts to contrib\/jo/)).toBeTruthy()
  })

  it('keeps a failed push on screen', async () => {
    status.mockResolvedValue(base)
    push.mockResolvedValue({ ok: false, error: "GitLab: Author 'jo@pc' is not a member of team" })
    render(<Harness />)
    fireEvent.click(await screen.findByText('Push'))
    fireEvent.click(await screen.findByRole('button', { name: 'Push 2 facts' }))
    expect(await screen.findByText(/Push failed: GitLab: Author/)).toBeTruthy()
  })

  it('cannot push when nothing is shareable', async () => {
    status.mockResolvedValue(base)
    preview.mockResolvedValue({ ...shareTwo, share: [] })
    render(<Harness />)
    fireEvent.click(await screen.findByText('Push'))
    expect(await screen.findByText('No approved team facts to share yet.')).toBeTruthy()
    const ok = screen.getAllByRole('button').find(b => b.textContent === 'Push' && b.closest('.ant-modal'))
    expect(ok?.hasAttribute('disabled')).toBe(true)
  })

  it('reports a pull that throws', async () => {
    status.mockResolvedValue(base)
    pull.mockRejectedValue(new Error('bridge went away'))
    render(<Harness />)
    fireEvent.click(await screen.findByText('Pull'))
    expect(await screen.findByText('Pull failed: bridge went away')).toBeTruthy()
  })
})

describe('pullTip', () => {
  const tagged = { ...base, pull_from: 'latest-tag' as const, pull_source: 'the newest v* tag', last_pull: '2026-10-12T09:00:00Z', last_pull_ref: 'v1.0.0' }
  it('names the source and a newer release', () => {
    const tip = pullTip(tagged, { ok: true, available: true, latest: 'v1.1.0', pinned: false })
    expect(tip).toMatch(/from the newest v\* tag/)
    expect(tip).toMatch(/Last pull: v1.0.0,/)
    expect(tip).toMatch(/v1.1.0 is ready to pull/)
  })
  it('points a pinned tag at Settings', () => {
    const tip = pullTip({ ...tagged, pull_from: 'tag', pull_source: 'v1.0.0' }, { ok: true, available: true, latest: 'v1.1.0', pinned: true })
    expect(tip).toMatch(/change the pinned tag in Settings/)
  })
  it('says when the branch has new merges', () => {
    expect(pullTip(base, { ok: true, available: true, latest: 'abc', pinned: false })).toMatch(/main has new merges/)
    expect(pullTip(base, null)).toMatch(/Never pulled/)
  })
})

describe('laneOf', () => {
  it('matches the backend lane_name', () => {
    expect(laneOf('Jason.Finestone')).toBe('jason.finestone')
    expect(laneOf('DOMAIN\\jason')).toBe('domain-jason')
    expect(laneOf('  ')).toBe('user')
  })
})

describe('errorText', () => {
  it('skips form validation and reads thrown errors', () => {
    expect(errorText({ errorFields: [] })).toBeNull()
    expect(errorText(new Error('boom'))).toBe('boom')
  })
})
