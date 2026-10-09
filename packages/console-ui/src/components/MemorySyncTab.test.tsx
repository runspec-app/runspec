import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { createText, refreshText, WikiRepoSection } from './MemorySyncTab'

const { create, refresh } = vi.hoisted(() => ({ create: vi.fn(), refresh: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'memory_sync_create_repo') return create
      if (p === 'memory_sync_refresh_docs') return refresh
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

describe('WikiRepoSection', () => {
  it('saves the form, then creates the repo', async () => {
    const save = vi.fn().mockResolvedValue(true)
    create.mockResolvedValue({ ok: true, branch: 'main', files: 59 })
    render(<WikiRepoSection save={save} />)
    fireEvent.click(screen.getByRole('button', { name: /Create wiki repo/ }))
    expect(await screen.findByText(/Created the wiki on main \(59 files\)/)).toBeTruthy()
    expect(save).toHaveBeenCalled()
  })

  it('does nothing when the form does not save', async () => {
    const save = vi.fn().mockResolvedValue(false)
    refresh.mockClear()
    render(<WikiRepoSection save={save} />)
    fireEvent.click(screen.getByRole('button', { name: /Refresh runspec pages/ }))
    await vi.waitFor(() => expect(save).toHaveBeenCalled())
    expect(refresh).not.toHaveBeenCalled()
  })

  it('shows a refusal', async () => {
    create.mockResolvedValue({ ok: false, error: 'main already exists on the repo' })
    render(<WikiRepoSection save={async () => true} />)
    fireEvent.click(screen.getByRole('button', { name: /Create wiki repo/ }))
    expect(await screen.findByText('main already exists on the repo')).toBeTruthy()
  })
})

describe('refreshText', () => {
  it('describes each outcome', () => {
    expect(refreshText({ ok: true, pages: 0 }).text).toMatch(/already up to date/)
    expect(refreshText({ ok: true, pages: 3, changed: true, branch: 'refresh/runspec-docs' }).text)
      .toMatch(/Pushed 3 changed page\(s\) to refresh\/runspec-docs/)
    expect(refreshText({ ok: true, pages: 3, changed: false, branch: 'r' }).text).toMatch(/already has these changes/)
    expect(refreshText({ ok: false, error: 'nope' })).toEqual({ ok: false, text: 'nope' })
    expect(createText({ ok: false }).ok).toBe(false)
  })
})
