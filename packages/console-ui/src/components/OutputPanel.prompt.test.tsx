/**
 * The prompt in a card: the header shows only its first line (cut off with
 * "…"), and a prompt that doesn't fit there is shown in full at the top of the
 * body as a "You" message, rendered as markdown and capped until "Show all".
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { OutputPanel, PromptMessage, PROMPT_CLAMP_LINES, promptNeedsBody, promptTitle } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

const MULTI = 'web-01 is throwing 502s. Can you:\n- check nginx\n- check disk\n\nHere is the `error.log` tail.'

function chatBlock(over: Partial<InvocationBlock> = {}): InvocationBlock {
  return {
    id: 'c1', sessionId: 's1', type: 'chat', label: MULTI,
    startedAt: new Date().toISOString(), lines: [], segments: [{ kind: 'text', text: 'Looking.' }],
    currentText: '', done: true, ...over,
  }
}

describe('promptTitle', () => {
  it('is the first non-empty line', () => {
    expect(promptTitle('\n  first line  \nsecond')).toBe('first line')
  })
  it('drops a leading heading or quote marker', () => {
    expect(promptTitle('## Disk report\nbody')).toBe('Disk report')
    expect(promptTitle('> quoted ask')).toBe('quoted ask')
  })
  it('leaves a run label alone', () => {
    expect(promptTitle('/find-large-files on web-01')).toBe('/find-large-files on web-01')
  })
})

describe('promptNeedsBody', () => {
  it('is false for a short one-line prompt', () => {
    expect(promptNeedsBody('why is web-01 low on disk?')).toBe(false)
    expect(promptNeedsBody('why is web-01 low on disk?\n\n')).toBe(false)
  })
  it('is true for a multi-line prompt', () => {
    expect(promptNeedsBody(MULTI)).toBe(true)
  })
  it('is true for one line too long for the header', () => {
    expect(promptNeedsBody('x'.repeat(101))).toBe(true)
  })
})

describe('card prompt rendering', () => {
  it('puts only the first line in the header and the full prompt in the body', () => {
    render(<OutputPanel blocks={[chatBlock()]} />)
    expect(screen.getByTestId('card-title').textContent).toBe('web-01 is throwing 502s. Can you:')
    const msg = screen.getByTestId('prompt-message')
    // Rendered as markdown: the list and inline code come through as elements.
    expect(msg.querySelectorAll('li')).toHaveLength(2)
    expect(msg.querySelector('code')?.textContent).toBe('error.log')
  })

  it('shows no "You" message for a short prompt (the header already says it)', () => {
    render(<OutputPanel blocks={[chatBlock({ label: 'why is web-01 low on disk?' })]} />)
    expect(screen.getByTestId('card-title').textContent).toBe('why is web-01 low on disk?')
    expect(screen.queryByTestId('prompt-message')).toBeNull()
  })

  it('shows no "You" message on a trigger card or a run card', () => {
    render(<OutputPanel blocks={[
      chatBlock({ id: 't1', trigger: { name: 'disk-alert', source: 'mail' } }),
      chatBlock({ id: 'r1', type: 'run', label: '/uptime on web-01\n(rerun)', segments: [] }),
    ]} />)
    expect(screen.queryByTestId('prompt-message')).toBeNull()
  })

  it('hides the prompt with the rest of the body when the card collapses', () => {
    render(<OutputPanel blocks={[chatBlock()]} />)
    fireEvent.click(screen.getByTestId('card-title'))
    expect(screen.queryByTestId('prompt-message')).toBeNull()
  })
})

describe('PromptMessage', () => {
  it('caps a long prompt until Show all is clicked', () => {
    const text = Array.from({ length: PROMPT_CLAMP_LINES + 4 }, (_, i) => `line ${i + 1}`).join('\n')
    render(<PromptMessage text={text} />)
    const btn = screen.getByRole('button', { name: `Show all (${PROMPT_CLAMP_LINES + 4} lines)` })
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(btn)
    expect(screen.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('has no Show all for a prompt that fits', () => {
    render(<PromptMessage text={'one\ntwo'} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
