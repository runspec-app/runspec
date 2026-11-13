import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { KnowledgeTools, buildToolIndex, mentionedTools } from './KnowledgeTools'
import type { Runnable, WikiPage } from '../bridge'

const { wikiPages, page } = vi.hoisted(() => ({ wikiPages: vi.fn(), page: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'knowledge_wiki_pages') return wikiPages
      if (p === 'knowledge_page') return page
      return vi.fn(async () => ({ ok: true }))
    },
  }),
}))

const rn = (name: string, host: string, group = 'ops-tools', extra: Partial<Runnable> = {}): Runnable => ({
  name, host, group, description: `${name} does things`, args: [{ name: 'url', type: 'str' } as Runnable['args'][number]], ...extra,
})

const tailLog: WikiPage = {
  path: 'runnables/tail-log.md', entity: 'runnable:tail-log', type: 'Runnable', title: 'tail-log',
  description: '', status: 'stable', stale_after: '', verified_by: 'human:ana', url: 'https://git/x/-/blob/main/runnables/tail-log.md',
  sections: [
    { heading: 'Works with', text: '- after http-check, read the log first', bullets: ['after http-check, read the log first'] },
    { heading: 'Watch out for', text: '- lines, not bytes', bullets: ['lines, not bytes'] },
  ],
}

beforeEach(() => {
  wikiPages.mockReset(); page.mockReset()
  try { localStorage.clear() } catch { /* ignore */ }
  wikiPages.mockResolvedValue({ configured: true, repo_url: 'https://git/x', ref: 'main', pages: [tailLog] })
  page.mockResolvedValue({
    entity: 'runnable:tail-log', type: 'runnable', name: 'tail-log', links: [], authors: [], last_confirmed: '',
    facts: [
      { id: 'k_1', kind: 'note', about: ['runnable:tail-log'], text: 'pipe it to grep', status: 'approved', source: 'agent', author: 'ana', evidence: [], first_seen: '', last_confirmed: '', confirmations: 1, scope: 'team' },
      { id: 'k_2', kind: 'note', about: ['runnable:tail-log', 'host:web-01'], text: 'web-01 logs rotate hourly', status: 'approved', source: 'agent', author: 'ana', evidence: [], first_seen: '', last_confirmed: '', confirmations: 1, scope: 'local' },
    ],
  })
})

describe('buildToolIndex()', () => {
  it('makes one entry per runnable name and joins its page', () => {
    const idx = buildToolIndex([rn('tail-log', 'a'), rn('tail-log', 'b', 'linux'), rn('http-check', 'a')], [tailLog])
    expect(idx.map(t => t.name)).toEqual(['http-check', 'tail-log'])
    const t = idx[1]
    expect(t.hosts).toEqual(['a', 'b'])
    expect(t.venvs).toEqual(['ops-tools', 'linux'])
    expect(t.page?.path).toBe('runnables/tail-log.md')
    expect(idx[0].page).toBeNull()
  })
})

describe('mentionedTools()', () => {
  it('finds whole tool names in text, longest first', () => {
    expect(mentionedTools('run `http-check` then tail-log-x, not tail-log', ['tail-log', 'http-check', 'tail-log-x']))
      .toEqual(['http-check', 'tail-log-x', 'tail-log'])
    expect(mentionedTools('nothing here', ['cron'])).toEqual([])
    // A plain-word name links only when marked up.
    expect(mentionedTools('a 502 right after a deploy', ['deploy'])).toEqual([])
    expect(mentionedTools('then run **deploy** again', ['deploy'])).toEqual(['deploy'])
    expect(mentionedTools('use [cron](/runnables/cron.md)', ['cron'])).toEqual(['cron'])
  })
})

describe('KnowledgeTools', () => {
  const runnables = [rn('tail-log', 'web-01'), rn('http-check', 'web-01'), rn('df', 'web-01', 'linux')]

  it('opens the first tool with a page, with its sections, links and facts', async () => {
    render(<KnowledgeTools runnables={runnables} active />)
    await waitFor(() => expect(screen.getByText('Works with')).toBeTruthy())
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('tail-log')
    expect(screen.getByText('Open in repo')).toBeTruthy()
    expect(await screen.findByText('pipe it to grep')).toBeTruthy()  // a team fact not on the page
    expect(screen.getByTestId('this-console-facts').textContent).toContain('never shared')
    fireEvent.click(screen.getByText('http-check →'))  // a tool the Works-with section names
    await waitFor(() => expect(screen.getByTestId('no-page')).toBeTruthy())
    expect(screen.getByTestId('no-page').textContent).toContain('Start a page')
  })

  it('filters to tools with no page and offers set-up when no repo is configured', async () => {
    wikiPages.mockResolvedValue({ configured: false, repo_url: '', ref: '', pages: [] })
    const open = vi.fn()
    render(<KnowledgeTools runnables={runnables} active onOpenSettings={open} />)
    await waitFor(() => expect(screen.getByTestId('no-page')).toBeTruthy())
    fireEvent.click(screen.getByText('Share with team'))
    expect(open).toHaveBeenCalledWith('memorySync')
    // Every tool lacks a page, so the filter would change nothing and isn't offered.
    expect(screen.queryByText(/No page yet/)).toBeNull()
    expect(screen.getByTestId('tool-df')).toBeTruthy()
  })

  it('searches page text as well as tool names', async () => {
    render(<KnowledgeTools runnables={runnables} active />)
    await waitFor(() => expect(screen.getByText('Works with')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Find a tool or page'), { target: { value: 'bytes' } })
    expect(screen.getByText('Pages mentioning “bytes”')).toBeTruthy()
  })
})
