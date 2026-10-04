/**
 * KnowledgeView: loads the overview, draws the entity graph, opens an entity page
 * on click, and approves an inbox fact.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { KnowledgeView } from './KnowledgeView'

const { overview, page, review, distill } = vi.hoisted(() => ({
  overview: vi.fn(),
  page: vi.fn(),
  review: vi.fn(),
  distill: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'knowledge_overview') return overview
      if (p === 'knowledge_page') return page
      if (p === 'knowledge_review') return review
      if (p === 'knowledge_distill') return distill
      return vi.fn(async () => ({ ok: true }))
    },
  }),
}))

const fact = (id: string, text: string, about: string[], status = 'approved') => ({
  id, kind: 'location', about, text, status, source: 'agent', author: 'me@box',
  evidence: [], first_seen: '2026-10-01', last_confirmed: '2026-10-03', confirmations: 2,
})

beforeEach(() => {
  overview.mockReset(); page.mockReset(); review.mockReset(); distill.mockReset()
  overview.mockResolvedValue({
    stats: { approved: 1, observed: 0, pending: 1, retired: 0, entities: 3, fts: true },
    graph: {
      nodes: [
        { id: 'host:web-01', type: 'host', name: 'web-01', weight: 2, facts: 1, pending: false, last_confirmed: '' },
        { id: 'app:orders', type: 'app', name: 'orders', weight: 2, facts: 1, pending: false, last_confirmed: '' },
      ],
      edges: [{ source: 'app:orders', target: 'host:web-01', weight: 2 }],
    },
    inbox: [fact('k_p', 'web-01 has 64GB RAM', ['host:web-01'], 'pending')],
    captured_at: '', dir: '/tmp/knowledge',
    entity_types: ['host', 'app'], kinds: ['location', 'note'],
  })
  page.mockResolvedValue({
    entity: 'host:web-01', type: 'host', name: 'web-01',
    facts: [fact('k_1', 'orders logs live in /var/log/orders', ['host:web-01', 'app:orders'])],
    links: [{ entity: 'app:orders', weight: 2 }], authors: ['me@box'], last_confirmed: '2026-10-03',
  })
  review.mockResolvedValue({ ok: true })
})

describe('KnowledgeView', () => {
  it('draws the graph and opens a page on click', async () => {
    render(<KnowledgeView active aiEnabled />)
    await waitFor(() => expect(screen.getByTestId('node-host:web-01')).toBeTruthy())
    fireEvent.click(screen.getByTestId('node-host:web-01'))
    await waitFor(() => expect(screen.getByText('orders logs live in /var/log/orders')).toBeTruthy())
    expect(page).toHaveBeenCalledWith('host:web-01')
    expect(screen.getByText('Suggest from chats')).toBeTruthy()
  })

  it('hides AI suggestions when AI is off and approves from the inbox', async () => {
    render(<KnowledgeView active aiEnabled={false} />)
    await waitFor(() => expect(overview).toHaveBeenCalled())
    expect(screen.queryByText('Suggest from chats')).toBeNull()
    fireEvent.click(screen.getByText('Inbox'))
    await waitFor(() => expect(screen.getByText('web-01 has 64GB RAM')).toBeTruthy())
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() => expect(review).toHaveBeenCalledWith('k_p', true))
  })

  it('suggests from chats over the remembered range', async () => {
    overview.mockResolvedValue({ ...(await overview()), capture_days: 0 })
    distill.mockResolvedValue({ ok: true, proposed: 3, conversations: 12, calls: 10, remaining: 4 })
    render(<KnowledgeView active aiEnabled />)
    await waitFor(() => expect(screen.getByText('All history')).toBeTruthy())
    fireEvent.click(screen.getByText('Suggest from chats'))
    await waitFor(() => expect(distill).toHaveBeenCalledWith(0))
  })
})
