/**
 * KnowledgeSync: the Knowledge tab's Pull button, and the lane name the
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

beforeEach(() => {
  status.mockReset(); push.mockReset(); pull.mockReset(); preview.mockReset()
})

describe('KnowledgeSync', () => {
  it('offers set-up when Memory Sync is not configured', async () => {
    status.mockResolvedValue({ ...base, configured: false })
    const open = vi.fn()
    render(<KnowledgeSync onOpenSettings={open} />)
    fireEvent.click(await screen.findByText('Share with team'))
    expect(open).toHaveBeenCalled()
  })

  it('pulls the team pages and facts, and no longer offers Push', async () => {
    status.mockResolvedValue(base)
    pull.mockResolvedValue({
      ok: true, ref: 'v1.4.0', files: 1, facts: 3, added: 3, removed: 0, skipped: [],
      pages: { pages: 12, added: 2, removed: 0 }, drafts_landed: 1,
    })
    render(<Harness />)
    fireEvent.click(await screen.findByText('Pull'))
    await waitFor(() => expect(pull).toHaveBeenCalled())
    expect(await screen.findByText(
      'Pulled 12 pages (2 new, 0 removed) and 3 team facts by 1 contributor from v1.4.0. 1 of your page edits is now merged.',
    )).toBeTruthy()
    expect(screen.queryByText('Push')).toBeNull()
    expect(push).not.toHaveBeenCalled()
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
