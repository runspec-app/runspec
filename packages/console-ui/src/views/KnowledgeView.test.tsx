/**
 * KnowledgeView: loads the overview, draws the entity graph, opens an entity page
 * on click, and approves an inbox fact.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { GraphCanvas, KnowledgeView, staleCheck } from './KnowledgeView'
import type { KnowledgeFact } from '../bridge'

const { overview, page, review, distill, pin } = vi.hoisted(() => ({
  pin: vi.fn(),
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
      if (p === 'knowledge_pin') return pin
      return vi.fn(async () => ({ ok: true }))
    },
  }),
}))

const fact = (id: string, text: string, about: string[], status = 'approved') => ({
  id, kind: 'location', about, text, status, source: 'agent', author: 'me@box',
  evidence: [], first_seen: '2026-10-01', last_confirmed: '2026-10-03', confirmations: 2,
})

beforeEach(() => {
  overview.mockReset(); page.mockReset(); review.mockReset(); distill.mockReset(); pin.mockReset()
  pin.mockResolvedValue({ ok: true })
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
    pinned: [fact('k_pin', 'deploys freeze on Fridays', ['term:freeze'])],
    pin_max: 6,
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
  it('lists pinned facts and pins / unpins from a fact row', async () => {
    render(<KnowledgeView active aiEnabled />)
    await waitFor(() => expect(screen.getByText('deploys freeze on Fridays')).toBeTruthy())
    expect(screen.getByText(/Pinned · 1\/6/)).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Unpin fact'))
    await waitFor(() => expect(pin).toHaveBeenCalledWith('k_pin', false))
    fireEvent.click(screen.getByTestId('node-host:web-01'))
    await waitFor(() => expect(screen.getByText('orders logs live in /var/log/orders')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Pin fact'))
    await waitFor(() => expect(pin).toHaveBeenCalledWith('k_1', true))
  })

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

  it('has zoom buttons and fades runnables that are no longer installed', async () => {
    overview.mockResolvedValue({
      ...(await overview()),
      graph: {
        nodes: [
          { id: 'host:web-01', type: 'host', name: 'web-01', weight: 2, facts: 1, pending: false, last_confirmed: '' },
          { id: 'runnable:gone', type: 'runnable', name: 'gone', weight: 2, facts: 1, pending: false, last_confirmed: '' },
        ],
        edges: [{ source: 'host:web-01', target: 'runnable:gone', weight: 2 }],
      },
      installed_runnables: ['disk-usage'],
    })
    render(<KnowledgeView active aiEnabled />)
    await waitFor(() => expect(screen.getByTestId('node-runnable:gone')).toBeTruthy())
    expect(screen.getByTestId('node-runnable:gone').getAttribute('opacity')).toBe('0.4')
    expect(screen.getByTestId('node-host:web-01').getAttribute('opacity')).toBe('1')
    expect(screen.getByLabelText('Zoom in')).toBeTruthy()
    expect(screen.getByLabelText('Fit to view')).toBeTruthy()
    fireEvent.click(screen.getByText('Hide not installed (1)'))
    await waitFor(() => expect(screen.queryByTestId('node-runnable:gone')).toBeNull())
  })
})

describe('GraphCanvas selection', () => {
  const nodes = [
    { id: 'host:a', type: 'host', name: 'a', weight: 2, facts: 1, pending: false, last_confirmed: '' },
    { id: 'runnable:x', type: 'runnable', name: 'x', weight: 2, facts: 1, pending: false, last_confirmed: '' },
  ]
  const edges = [{ source: 'host:a', target: 'runnable:x', weight: 1 }]

  it('clears on a click on empty canvas or the clear button, not on a pan', () => {
    const onClear = vi.fn()
    const { rerender } = render(<GraphCanvas nodes={nodes} edges={edges} selected="" onSelect={vi.fn()} onClear={onClear} />)
    expect(screen.queryByLabelText('Clear selection')).toBeNull()
    rerender(<GraphCanvas nodes={nodes} edges={edges} selected="host:a" onSelect={vi.fn()} onClear={onClear} />)
    const svg = screen.getByRole('img', { name: 'Knowledge graph' })
    fireEvent.mouseDown(svg, { clientX: 10, clientY: 10 })
    fireEvent.mouseUp(svg, { clientX: 80, clientY: 60 }) // a pan
    expect(onClear).not.toHaveBeenCalled()
    fireEvent.mouseDown(svg, { clientX: 10, clientY: 10 })
    fireEvent.mouseUp(svg, { clientX: 11, clientY: 10 }) // a click
    expect(onClear).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByLabelText('Clear selection'))
    expect(onClear).toHaveBeenCalledTimes(2)
  })
})

describe('staleCheck', () => {
  it('marks runnables missing from discovery, and nothing before discovery', () => {
    const stale = staleCheck(['disk-usage', 'cron'])!
    expect(stale('runnable:install-into-venv')).toBe(true)
    expect(stale('runnable:cron')).toBe(false)
    expect(stale('host:web-01')).toBe(false)
    expect(staleCheck([])).toBeNull()
  })
})
