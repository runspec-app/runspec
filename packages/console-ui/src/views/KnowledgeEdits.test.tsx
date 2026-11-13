import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { KnowledgeTools } from './KnowledgeTools'
import { ProposeBar } from './KnowledgeEdits'
import type { Runnable, WikiDraft, WikiPage } from '../bridge'

const m = vi.hoisted(() => ({
  wikiPages: vi.fn(), page: vi.fn(), drafts: vi.fn(), draftFact: vi.fn(), convert: vi.fn(),
  propose: vi.fn(), draftText: vi.fn(), draftSave: vi.fn(), discard: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => ({
      knowledge_wiki_pages: m.wikiPages, knowledge_page: m.page, knowledge_drafts: m.drafts,
      knowledge_draft_fact: m.draftFact, knowledge_convert_facts: m.convert, memory_sync_propose: m.propose,
      knowledge_draft_text: m.draftText, knowledge_draft_save: m.draftSave, knowledge_draft_discard: m.discard,
    } as Record<string, unknown>)[p as string] ?? vi.fn(async () => ({ ok: true })),
  }),
}))

const cronPage: WikiPage = {
  path: 'runnables/cron.md', entity: 'runnable:cron', type: 'Runnable', title: 'cron', description: '',
  status: '', stale_after: '', verified_by: '', url: '',
  sections: [{ heading: 'Works with', text: '- run list first', bullets: ['run list first'] }],
}
const draft: WikiDraft = {
  path: 'runnables/cron.md', entity: 'runnable:cron', new: false, added: 1, removed: 0,
  diff: '--- a/runnables/cron.md\n+++ b/runnables/cron.md\n@@ -9 +9,2 @@\n - run list first\n+- remove by --match',
}
const fact = (id: string, text: string, extra = {}) => ({
  id, kind: 'note', about: ['runnable:cron'], text, status: 'approved', source: 'agent', author: 'jo',
  evidence: [], first_seen: '', last_confirmed: '', confirmations: 1, scope: 'team', ...extra,
})
const rn = (name: string): Runnable => ({ name, host: 'local', group: 'ops', args: [] })

beforeEach(() => {
  Object.values(m).forEach(f => f.mockReset())
  try { localStorage.clear() } catch { /* ignore */ }
  m.wikiPages.mockResolvedValue({ configured: true, repo_url: 'https://gitlab.x/ops/wiki', ref: 'main', pages: [cronPage] })
  m.drafts.mockResolvedValue({ drafts: [draft], drafted: ['k_done'] })
  m.page.mockResolvedValue({ entity: 'runnable:cron', type: 'runnable', name: 'cron', links: [], authors: [], last_confirmed: '',
    facts: [fact('k_new', 'clear removes every line'), fact('k_done', 'already on a page'), fact('k_seed', 'built in', { source: 'seed' })] })
  m.draftFact.mockResolvedValue({ ok: true, path: 'runnables/cron.md', heading: 'Watch out for' })
  m.convert.mockResolvedValue({ ok: true, facts: 2, pages: 1, kept_local: 1, no_page: 0 })
  m.propose.mockResolvedValue({ ok: true, branch: 'contrib/jo', changed: true, pages: 1, merge_request_url: 'https://gitlab.x/ops/wiki/-/merge_requests/new?x' })
  m.draftText.mockResolvedValue({ ok: true, path: 'runnables/df.md', text: '---\ntype: Runnable\n---\n\n# df\n', base: null })
  m.draftSave.mockResolvedValue({ ok: true, kept: true })
})

describe('Tools view page edits', () => {
  it('shows the unproposed edit of a page, and adds a fact to its page', async () => {
    render(<KnowledgeTools runnables={[rn('cron'), rn('df')]} active />)
    await waitFor(() => expect(screen.getByTestId('page-draft')).toBeTruthy())
    expect(screen.getByTestId('draft-diff').textContent).toContain('+- remove by --match')
    expect(screen.getByTestId('tool-cron').getAttribute('aria-label')).toBe('cron, has a team page, edited on this console')  // marked in the index
    // Facts already drafted, and built-in ones, aren't offered again.
    await waitFor(() => expect(screen.getByText('clear removes every line')).toBeTruthy())
    expect(screen.queryByText('already on a page')).toBeNull()
    expect(screen.queryByText('built in')).toBeNull()
    fireEvent.click(screen.getByText('Add to page'))
    await waitFor(() => expect(m.draftFact).toHaveBeenCalledWith('k_new'))
    fireEvent.click(screen.getByText('Add all facts to pages'))
    expect(m.convert).not.toHaveBeenCalled()  // it asks first
    fireEvent.click(await screen.findByRole('button', { name: 'Add to pages' }))
    await waitFor(() => expect(m.convert).toHaveBeenCalled())
  })

  it('starts a page for a tool that has none', async () => {
    m.drafts.mockResolvedValue({ drafts: [], drafted: [] })
    render(<KnowledgeTools runnables={[rn('cron'), rn('df')]} active />)
    await waitFor(() => expect(screen.getByText('Works with')).toBeTruthy())
    fireEvent.click(screen.getByTestId('tool-df'))
    fireEvent.click(await screen.findByText('Start a page'))
    await waitFor(() => expect(m.draftText).toHaveBeenCalledWith('runnables/df.md', 'runnable:df', ''))
    const box = await screen.findByLabelText('Page Markdown')
    fireEvent.change(box, { target: { value: '---\ntype: Runnable\n---\n\n# df\n\n## Works with\n\n- journal-vacuum\n' } })
    // Preview renders the Markdown without the frontmatter, and Write goes back to the text.
    fireEvent.click(screen.getByText('Preview'))
    const preview = await screen.findByTestId('page-preview')
    expect(preview.textContent).toContain('journal-vacuum')
    expect(preview.textContent).not.toContain('type: Runnable')
    fireEvent.click(screen.getByText('Write'))
    await screen.findByLabelText('Page Markdown')
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
    await waitFor(() => expect(m.draftSave).toHaveBeenCalledWith('runnables/df.md', expect.stringContaining('journal-vacuum')))
  })
})

describe('ProposeBar', () => {
  it('lists the edits, proposes them, and links the merge request', async () => {
    const changed = vi.fn()
    render(<ProposeBar drafts={[draft]} configured onChanged={changed} />)
    expect(screen.getByTestId('propose-bar').textContent).toContain('1 page edited on this console')
    fireEvent.click(screen.getByRole('button', { name: /Propose changes/ }))
    expect(await screen.findByText('Propose 1 page to the team?')).toBeTruthy()
    const ok = screen.getAllByRole('button', { name: /Propose changes/ }).find(b => b.closest('.ant-modal'))!
    fireEvent.click(ok)
    await waitFor(() => expect(m.propose).toHaveBeenCalled())
    const link = await screen.findByText(/Open merge request/)
    expect(link.closest('a')?.getAttribute('href')).toBe('https://gitlab.x/ops/wiki/-/merge_requests/new?x')
    expect(changed).toHaveBeenCalled()
  })

  it('sends set-up to Settings when no wiki repo is configured', () => {
    const open = vi.fn()
    render(<ProposeBar drafts={[draft]} configured={false} onChanged={() => {}} onOpenSettings={open} />)
    fireEvent.click(screen.getByText('Set up the team wiki'))
    expect(open).toHaveBeenCalledWith('memorySync')
  })
})
