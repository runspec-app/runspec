/**
 * Trigger runs surface in the Console tab.
 *
 * A trigger fire is mirrored into the Console as a live block (from
 * runspec:trigger_card, keyed by card_id = chat_id) so all agent/trigger activity
 * — and, in a follow-up, its Approve/Deny gate — has one home. These cover:
 *   1. a rule trigger renders a run block, filled from the card's terminal result;
 *   2. an agent trigger renders a chat block that the streamed token/run_end
 *      handlers populate (keyed by the same id);
 *   3. the finished post_to_chat mirror (runspec:trigger_note) with the same
 *      card_id folds onto the live block instead of duplicating it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import { ConsoleView } from '../views/ConsoleView'
import type { TriggerCard } from '../bridge'

// The real `bridge` is a Proxy that waits on window.pywebview — replace it. The
// Console seeds itself on mount via list_chat_sessions() + both run boards,
// which must be awaitable.
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_trigger_board' || p === 'get_schedule_board' || p === 'list_chat_sessions' || p === 'get_pending_confirms') {
        return () => Promise.resolve([])
      }
      if (p === 'close_chat_session' || p === 'select_chat_session') return () => Promise.resolve({ ok: true })
      return vi.fn()
    },
  }),
}))

// Hand-driven requestAnimationFrame queue so a test can dispatch streamed tokens
// and flush them in one frame (OutputPanel coalesces per frame).
let rafCbs: FrameRequestCallback[] = []
beforeEach(() => {
  rafCbs = []
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    rafCbs.push(cb)
    return rafCbs.length
  }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
})
afterEach(() => vi.restoreAllMocks())

const flushRaf = () => {
  const cbs = rafCbs
  rafCbs = []
  cbs.forEach(cb => cb(0))
}

function card(over: Partial<TriggerCard>): TriggerCard {
  return {
    card_id: 'c1', trigger: 'nightly', source: 'outlook', event_type: 'new_mail',
    summary: 'A new mail', column: 'triggered', detail: {}, created_at: Date.now() / 1000,
    ...over,
  }
}
const emitCard = (c: TriggerCard) =>
  window.dispatchEvent(new CustomEvent('runspec:trigger_card', { detail: c }))
const emitToken = (id: string, token: string) =>
  window.dispatchEvent(new CustomEvent('runspec:token', { detail: { id, token } }))
const emitEnd = (id: string, exit_code = 0) =>
  window.dispatchEvent(new CustomEvent('runspec:run_end', { detail: { id, exit_code, duration_ms: 5 } }))
const emitNote = (detail: { id: string; kind: string; label?: string; text?: string }) =>
  window.dispatchEvent(new CustomEvent('runspec:trigger_note', { detail }))
const emitConfirm = (detail: Record<string, unknown>) =>
  window.dispatchEvent(new CustomEvent('runspec:tool_confirm', { detail }))

function mount() {
  render(<ConsoleView inFlight={[]} />)
}

describe('trigger runs in the Console', () => {
  it('renders a rule trigger as a run block filled from the card result', async () => {
    mount()
    await act(async () => {
      emitCard(card({ card_id: 'r1', detail: { mode: 'rule', tool: 'restart' }, column: 'running' }))
    })
    // The runnable target is the block label (and the session chip's).
    expect(screen.getAllByText('/restart').length).toBeGreaterThanOrEqual(1)
    // A "trigger:" tag marks it as an automatic fire.
    expect(screen.getByText(/trigger: nightly/)).toBeTruthy()

    await act(async () => {
      emitCard(card({ card_id: 'r1', column: 'done', detail: { mode: 'rule', tool: 'restart', result: 'restarted ok' } }))
    })
    expect(screen.getByText(/restarted ok/)).toBeTruthy()
  })

  it('renders an agent trigger as a chat block the stream populates', async () => {
    mount()
    await act(async () => {
      emitCard(card({ card_id: 'a1', detail: { mode: 'agent' }, column: 'triage' }))
    })
    // The label shows on the block and on the session chip (the fire is its own
    // read-only session, auto-selected as nothing else is open).
    expect(screen.getAllByText(/Trigger nightly · new_mail/).length).toBeGreaterThanOrEqual(1)

    await act(async () => { emitToken('a1', 'Handled '); emitToken('a1', 'the mail.'); flushRaf() })
    expect(screen.getByText(/Handled the mail\./)).toBeTruthy()
    await act(async () => { emitEnd('a1') })
  })

  it('folds a same-id trigger_note mirror onto the live block (no duplicate)', async () => {
    mount()
    await act(async () => {
      emitCard(card({ card_id: 'r2', column: 'done', detail: { mode: 'rule', tool: 'backup', result: 'done: 3 files' } }))
    })
    expect(screen.getAllByText(/done: 3 files/).length).toBe(1)

    // The finished post_to_chat mirror carries the same card_id — it must not add
    // a second block.
    await act(async () => { emitNote({ id: 'r2', kind: 'run', label: 'backup', text: 'done: 3 files' }) })
    expect(screen.getAllByText(/done: 3 files/).length).toBe(1)
  })

  // The console MCP server surfaces a parked confirm as a source="mcp" run card
  // (detail.mode="rule") keyed by card_id == the confirm's chat id, so an external
  // agent's confirm-gated call gets a visible Approve/Deny in the Console tab —
  // the home it lacked (worst of all with [console] ai = false). This locks in the
  // exact card+confirm contract bridge_mcp_server produces.
  it('renders Approve/Deny on an MCP-server run card parked for approval', async () => {
    mount()
    await act(async () => {
      emitCard(card({
        card_id: 'm1', source: 'mcp', event_type: 'request',
        detail: { mode: 'rule', runnable: 'drain-node' }, column: 'awaiting_human',
      }))
    })
    await act(async () => {
      emitConfirm({
        id: 'm1', request_id: 'req-m1', tool_name: 'drain-node', tool_input: { node: 'n1' },
        display_name: 'drain-node', display_input: { node: 'n1' }, autonomy: 'confirm',
      })
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^deny$/i })).toBeTruthy()
  })
})
