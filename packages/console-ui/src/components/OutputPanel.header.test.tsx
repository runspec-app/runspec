/**
 * Card header actions + tool-row labels.
 *
 *   1. The header ends in Copy output · Ask the assistant · ⋯ (the prompt and
 *      rerun actions moved into the ⋯ menu), and the body no longer spends a
 *      line on a Copy/Ask-only toolbar.
 *   2. Search-mode wrappers (`run_runnable`, `use_tool`) show the runnable /
 *      tool that actually ran, not the wrapper name.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { OutputPanel, cardMoreItems, toolDisplay } from './OutputPanel'
import type { InvocationBlock, BlockSegment, ToolCallEntry } from './OutputPanel'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

function chatBlock(over: Partial<InvocationBlock> = {}): InvocationBlock {
  return {
    id: 'c1', sessionId: 's1', type: 'chat', label: 'why is web-01 low on disk?',
    startedAt: new Date().toISOString(), lines: [], segments: [],
    currentText: '', done: true, ...over,
  }
}

const wrapped: BlockSegment = {
  kind: 'tool',
  entry: {
    name: 'run_runnable',
    input: { id: 'runspec_linux__find-large-files', host: 'web-01', args: { path: '/var' } },
    running: false,
    output: '{}',
  },
}

describe('toolDisplay', () => {
  it('unwraps run_runnable to the runnable, its command and host + args', () => {
    const e: ToolCallEntry = {
      name: 'run_runnable',
      input: { id: 'linux__cron', command: ['add'], host: 'web-01', args: { schedule: '@daily' } },
      running: false,
    }
    expect(toolDisplay(e)).toEqual({
      name: 'cron add',
      args: [['host', 'web-01'], ['schedule', '@daily']],
      via: 'run_runnable',
    })
  })

  it('unwraps use_tool to the inner built-in and its input', () => {
    const e: ToolCallEntry = { name: 'use_tool', input: { name: 'outlook_inbox', input: { limit: 5 } }, running: false }
    expect(toolDisplay(e)).toEqual({ name: 'outlook_inbox', args: [['limit', 5]], via: 'use_tool' })
  })

  it('leaves other tools alone, minus the group/host prefix', () => {
    const e: ToolCallEntry = { name: 'web-01__uptime', input: { brief: true }, running: false }
    expect(toolDisplay(e)).toEqual({ name: 'uptime', args: [['brief', true]] })
  })

  it('falls back to the wrapper name when run_runnable has no id', () => {
    const e: ToolCallEntry = { name: 'run_runnable', input: {}, running: false }
    expect(toolDisplay(e).name).toBe('run_runnable')
  })
})

describe('cardMoreItems', () => {
  it('offers the prompt actions on a finished chat turn', () => {
    const keys = cardMoreItems(chatBlock(), { canRerun: true, canEditInForm: false }).map(i => i?.key)
    expect(keys).toEqual(['copy-prompt', 'edit-resend', 'resend'])
  })

  it('is empty while the turn is still running', () => {
    expect(cardMoreItems(chatBlock({ done: false }), { canRerun: true, canEditInForm: true })).toEqual([])
  })

  it('offers rerun (and edit-in-form when possible) on a finished run', () => {
    const block = chatBlock({ type: 'run', rerunData: { host: 'web-01', runnable: 'uptime', args: {} } })
    expect(cardMoreItems(block, { canRerun: true, canEditInForm: true }).map(i => i?.key)).toEqual(['edit-form', 'rerun'])
    expect(cardMoreItems(block, { canRerun: true, canEditInForm: false }).map(i => i?.key)).toEqual(['rerun'])
    expect(cardMoreItems(block, { canRerun: false, canEditInForm: true })).toEqual([])
  })
})

describe('OutputPanel card header', () => {
  it('ends with Copy output, Ask the assistant and More actions', () => {
    render(<OutputPanel blocks={[chatBlock({ segments: [{ kind: 'text', text: 'done' }] })]} onAskLlm={() => {}} />)
    expect(screen.getByRole('button', { name: 'Copy output to clipboard' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Ask the assistant about this' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'More actions' })).toBeTruthy()
  })

  it('moves the prompt actions into the More menu', async () => {
    render(<OutputPanel blocks={[chatBlock({ segments: [{ kind: 'text', text: 'done' }] })]} />)
    expect(screen.queryByText('Edit & resend')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }))
    expect(await screen.findByText('Edit & resend')).toBeTruthy()
    expect(screen.getByText('Copy prompt')).toBeTruthy()
    expect(screen.getByText('Resend')).toBeTruthy()
  })

  it('labels a search-mode tool row with the runnable that ran', () => {
    render(<OutputPanel blocks={[chatBlock({ segments: [wrapped] })]} />)
    expect(screen.getByText('find-large-files')).toBeTruthy()
    expect(screen.queryByText('run_runnable')).toBeNull()
  })
})
