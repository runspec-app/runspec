/**
 * Component test for the autonomy-gate confirm UI in OutputPanel.
 *
 * This is the render/wiring layer that pure-logic tests can't reach — and the
 * exact class of bug that shipped in 0.91.0: the Approve/Deny buttons only
 * render when the live `runspec:tool_confirm` event's `tool_name` matches the
 * running tool-call segment's `entry.name`. We drive the real event and assert
 * on the DOM, headlessly, so this is caught in CI instead of on a Windows box.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { OutputPanel } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

// The real `bridge` is a Proxy that lazily waits for window.pywebview (2.5s
// fallback to the mock) — neither spy-able nor fast. Replace the module with a
// stub whose methods are vi.fn()s so we can assert the gate answer synchronously.
const { resolveConfirm } = vi.hoisted(() => ({ resolveConfirm: vi.fn() }))
vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (typeof prop === 'symbol') return undefined
        return prop === 'resolve_tool_confirmation' ? resolveConfirm : vi.fn()
      },
    },
  ),
}))

// A chat block parked on a single running tool call. `toolName` is the segment's
// entry.name — i.e. the *raw* tool the agent invoked (e.g. "run_runnable").
function chatBlockWithRunningTool(toolName: string): InvocationBlock {
  return {
    id: 'chat1',
    type: 'chat',
    label: 'do the thing',
    startedAt: new Date().toISOString(),
    lines: [],
    segments: [{ kind: 'tool', entry: { name: toolName, input: { id: 'fleet__drain-node' }, running: true } }],
    currentText: '',
    done: false,
  }
}

function fireConfirm(detail: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new CustomEvent('runspec:tool_confirm', { detail }))
  })
}

describe('OutputPanel autonomy-gate confirm', () => {
  beforeEach(() => {
    resolveConfirm.mockReset().mockResolvedValue(undefined)
  })

  it('renders Approve/Deny when the confirm matches the running tool segment', () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    // Nothing parked yet.
    expect(screen.queryByRole('button', { name: /approve & run/i })).toBeNull()

    // The fix: confirm carries the *raw* tool_name (matches the segment) plus a
    // human-readable resolved view.
    fireConfirm({
      id: 'chat1',
      request_id: 'req-1',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      display_input: { host: 'a', node: 'n1' },
      autonomy: 'confirm',
    })

    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^deny$/i })).toBeTruthy()
    // The resolved runnable + its args show (the #267 intent that never rendered).
    expect(screen.getByText('drain-node')).toBeTruthy()
    expect(screen.getByText(/node = "n1"/)).toBeTruthy()
  })

  it('does NOT render buttons when the confirm name mismatches the segment (the 0.91.0 bug)', () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    // The old, broken shape: tool_name was the *unwrapped* runnable name, which
    // never equals the segment's "run_runnable" → buttons must stay hidden.
    fireConfirm({
      id: 'chat1',
      request_id: 'req-1',
      tool_name: 'drain-node',
      tool_input: { host: 'a', node: 'n1' },
      autonomy: 'confirm',
    })
    expect(screen.queryByRole('button', { name: /approve & run/i })).toBeNull()
  })

  it('answers the gate via the bridge when Approve is clicked', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-42',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    await userEvent.click(screen.getByRole('button', { name: /approve & run/i }))
    // The run_runnable wrapper (display ≠ tool name) is NOT editable, so no edited
    // input is sent.
    expect(resolveConfirm).toHaveBeenCalledWith('req-42', true, undefined)
  })

  it('renders a long/multi-line arg as a textarea so it can be reviewed and edited', async () => {
    const { container } = render(<OutputPanel blocks={[chatBlockWithRunningTool('save_runbook')]} />)
    // A runbook's `steps` is a multi-line body. In a one-line Input only the first
    // line is reachable, so approving it is theatre — the operator can neither read
    // the procedure they're accepting nor correct it.
    const steps = '1. probe each node\n2. restart the unhealthy ones\n3. re-check'
    fireConfirm({
      id: 'chat1',
      request_id: 'req-9',
      tool_name: 'save_runbook',
      tool_input: { title: 'Triage 502s', steps },
      display_name: 'save_runbook',
      display_input: { title: 'Triage 502s', steps },
      autonomy: 'confirm',
    })
    // Short value → plain input; multi-line value → textarea, carrying every line.
    // (Queried directly: getByDisplayValue normalises whitespace, which is exactly
    // the newlines this test is about.)
    expect(screen.getByDisplayValue('Triage 502s').tagName).toBe('INPUT')
    const body = container.querySelector('textarea')
    expect(body).not.toBeNull()
    expect((body as HTMLTextAreaElement).value).toBe(steps)

    await userEvent.click(screen.getByRole('button', { name: /approve & run/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-9', true, {
      title: 'Triage 502s',
      steps,
    })
  })

  it('lets the operator edit args before approving a plain tool call', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('create_document')]} />)
    // A plain tool: display_name === tool_name → editable. Args render as inputs.
    fireConfirm({
      id: 'chat1',
      request_id: 'req-7',
      tool_name: 'create_document',
      tool_input: { filename: 'note.md', dest: 'local' },
      display_name: 'create_document',
      display_input: { filename: 'note.md', dest: 'local' },
      autonomy: 'confirm',
    })
    const filename = screen.getByDisplayValue('note.md')
    await userEvent.clear(filename)
    await userEvent.type(filename, 'edited.md')
    await userEvent.click(screen.getByRole('button', { name: /approve & run/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-7', true, {
      filename: 'edited.md',
      dest: 'local',
    })
  })
})
