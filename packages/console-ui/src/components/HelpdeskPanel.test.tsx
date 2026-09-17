/**
 * HelpdeskPanel: the queue affordances kept in the Triggers tab after the run
 * activity + Approve/Deny gate moved to the Console tab. Covers the claim button,
 * a portal request rendering, and the AI-off / empty self-hide.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { HelpdeskPanel } from './HelpdeskPanel'
import { FeaturesContext } from '../features'
import type { TriggerCard } from '../bridge'

const { claimEmail } = vi.hoisted(() => ({ claimEmail: vi.fn() }))
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_trigger_board') return () => Promise.resolve([])
      if (p === 'claim_email') return claimEmail
      return vi.fn()
    },
  }),
}))

function card(over: Partial<TriggerCard>): TriggerCard {
  return {
    card_id: 'c1', trigger: 'helpdesk', source: 'outlook', event_type: 'new_mail',
    summary: 'Password reset please', column: 'triggered', detail: {}, created_at: 1,
    ...over,
  }
}
const emitCard = (c: TriggerCard) =>
  act(() => { window.dispatchEvent(new CustomEvent('runspec:trigger_card', { detail: c })) })

beforeEach(() => { claimEmail.mockReset().mockResolvedValue({ claimed: true }) })

describe('HelpdeskPanel', () => {
  it('renders nothing when there are no queue items', () => {
    const { container } = render(<HelpdeskPanel />)
    expect(container.textContent).toBe('')
  })

  it('shows a Claim button for an awaiting_claim card and claims it', async () => {
    render(<HelpdeskPanel />)
    emitCard(card({ card_id: 'k1', column: 'awaiting_claim', detail: { claim_folder: 'Tickets/Claimed' } }))
    const btn = await screen.findByRole('button', { name: /^Claim$/ })
    await userEvent.click(btn)
    await waitFor(() => expect(claimEmail).toHaveBeenCalledWith('k1'))
    // The claim note points the operator to the Console tab for approvals.
    expect(screen.getByText(/Approve\/deny of the resulting run happens in the Console tab/)).toBeTruthy()
  })

  it('renders a portal request with its runnable and args', async () => {
    render(<HelpdeskPanel />)
    emitCard(card({
      card_id: 'p1', source: 'portal', summary: 'Portal request',
      detail: { visitor: 'sam@corp.com', runnable: 'restart-service', args: { service: 'nginx' } },
    }))
    expect(await screen.findByText('restart-service')).toBeTruthy()
    expect(screen.getByText(/service = nginx/)).toBeTruthy()
  })

  it('stays hidden when AI is off, even with a queue card', async () => {
    render(
      <FeaturesContext.Provider value={{ ai: false }}>
        <HelpdeskPanel />
      </FeaturesContext.Provider>,
    )
    emitCard(card({ card_id: 'k2', column: 'awaiting_claim' }))
    expect(screen.queryByRole('button', { name: /^Claim$/ })).toBeNull()
  })
})
