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
    sessionId: 's1',
    type: 'chat',
    label: 'do the thing',
    startedAt: new Date().toISOString(),
    lines: [],
    segments: [{ kind: 'tool', entry: { name: toolName, input: { id: 'fleet__drain-node' }, running: true } }],
    currentText: '',
    done: false,
  }
}

// A rule-mode trigger run: a plain run block with no tool segment at all. Its
// confirm has nothing to bind to inside the transcript, so the card-level gate
// must render it.
function ruleRunBlock(id: string): InvocationBlock {
  return {
    id,
    sessionId: 's1',
    type: 'run',
    label: '/drain-node',
    startedAt: new Date().toISOString(),
    lines: [],
    segments: [],
    currentText: '',
    done: false,
    trigger: { name: 'nightly', source: 'outlook' },
  }
}

function fireConfirm(detail: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new CustomEvent('runspec:tool_confirm', { detail }))
  })
}
function fireResolved(detail: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new CustomEvent('runspec:confirm_resolved', { detail }))
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
    // In the approval panel — and, unwrapped from run_runnable, in the tool
    // row and the bottom activity bar too.
    expect(screen.getAllByText('drain-node').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/node = "n1"/)).toBeTruthy()
  })

  it('puts the gate on the card with the matching call id, not the first same-name card', () => {
    // Two run_runnable cards running at once (search mode: every runnable call
    // is run_runnable). Name-only matching put the gate on BOTH — visibly on the
    // earlier (top) card. With call_id, only the gated call's own card shows it.
    const block = chatBlockWithRunningTool('run_runnable')
    block.segments = [
      { kind: 'tool', entry: { name: 'run_runnable', callId: 'call_a', input: { id: 'disk-usage' }, running: true } },
      { kind: 'tool', entry: { name: 'run_runnable', callId: 'call_b', input: { id: 'drain-node' }, running: true } },
    ]
    const { container } = render(<OutputPanel blocks={[block]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-1',
      call_id: 'call_b',
      tool_name: 'run_runnable',
      tool_input: { id: 'drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    const approves = screen.getAllByRole('button', { name: /approve & run/i })
    expect(approves).toHaveLength(1)
    // …and it's inside the second (bottom) card's block, after the first card.
    const text = container.textContent ?? ''
    expect(text.indexOf('disk-usage')).toBeLessThan(text.indexOf('Approve'))
  })

  it('falls back to a card-level gate when the confirm matches no running segment', () => {
    // Historically (0.91.0) a confirm whose tool_name didn't match the running
    // segment rendered nothing — wedging the run with no way to approve. The
    // card-level gate is the backstop: a confirm keyed to this block's id always
    // gets a reachable Approve/Deny (bound by request_id), whether the segment
    // name differs or the segment simply hasn't streamed yet.
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-1',
      tool_name: 'drain-node',
      tool_input: { host: 'a', node: 'n1' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^deny$/i })).toBeTruthy()
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
      // save_runbook stores a runbook, it does not execute it — the backend sends
      // "save" so the button reads "Approve & save", not "Approve & run".
      confirm_verb: 'save',
      autonomy: 'confirm',
    })
    // Short value → plain input; multi-line value → textarea, carrying every line.
    // (Queried directly: getByDisplayValue normalises whitespace, which is exactly
    // the newlines this test is about.)
    expect(screen.getByDisplayValue('Triage 502s').tagName).toBe('INPUT')
    const body = container.querySelector('textarea')
    expect(body).not.toBeNull()
    expect((body as HTMLTextAreaElement).value).toBe(steps)

    // The button reflects the verb: "save", not the misleading "run".
    expect(screen.queryByRole('button', { name: /approve & run/i })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /approve & save/i }))
    // Approving without touching the fields is NOT an edit — the gate is answered
    // with no editedInput, so the backend records "approved" (not "edited") and
    // runs the agent's original args untouched.
    expect(resolveConfirm).toHaveBeenCalledWith('req-9', true, undefined)
  })

  it('shows the account the call runs as when the backend names one', () => {
    // A shared venv's tools run as its owner (run_as → @owner), not the login
    // account — the card must say whose identity the operator is approving.
    render(<OutputPanel blocks={[chatBlockWithRunningTool('cron_list')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-ra',
      tool_name: 'cron_list',
      tool_input: { host: 'deb2' },
      display_name: 'cron_list',
      display_input: { host: 'deb2' },
      runs_as: 'svc-ops',
      autonomy: 'confirm',
    })
    expect(screen.getByTestId('confirm-runs-as').textContent).toBe('Runs as svc-ops')
  })

  it('omits the runs-as line when the call runs as the login account', () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('cron_list')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-nora',
      tool_name: 'cron_list',
      tool_input: {},
      display_name: 'cron_list',
      display_input: {},
      autonomy: 'confirm',
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    expect(screen.queryByTestId('confirm-runs-as')).toBeNull()
  })

  it('approving an editable confirm without changing anything is not an edit', async () => {
    // The bug: an editable confirm always shipped its (unchanged) edit buffer as
    // editedInput, so a bare Approve got labelled "Edited" even though the operator
    // only clicked confirm. Now an untouched buffer answers with no editedInput.
    render(<OutputPanel blocks={[chatBlockWithRunningTool('create_document')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-noop',
      tool_name: 'create_document',
      tool_input: { filename: 'note.md', dest: 'local' },
      display_name: 'create_document',
      display_input: { filename: 'note.md', dest: 'local' },
      confirm_verb: 'create',
      autonomy: 'confirm',
    })
    await userEvent.click(screen.getByRole('button', { name: /approve & create/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-noop', true, undefined)
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
      confirm_verb: 'create',
      autonomy: 'confirm',
    })
    const filename = screen.getByDisplayValue('note.md')
    await userEvent.clear(filename)
    await userEvent.type(filename, 'edited.md')
    await userEvent.click(screen.getByRole('button', { name: /approve & create/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-7', true, {
      filename: 'edited.md',
      dest: 'local',
    })
  })

  it('shows sandboxed code in a non-editable confirm with REAL newlines, not escaped \\n', () => {
    // A search-mode use_tool wrapper: display_name (run_python_sandboxed) ≠ the
    // segment's tool_name (use_tool) → not editable → the read-only <pre>. Before
    // the fix it rendered JSON.stringify(code), i.e. one line full of \n that the
    // operator couldn't read. Now the code shows with its real line breaks.
    const { container } = render(<OutputPanel blocks={[chatBlockWithRunningTool('use_tool')]} />)
    const code = 'import os\nprint(os.getcwd())'
    fireConfirm({
      id: 'chat1',
      request_id: 'req-c',
      tool_name: 'use_tool',
      tool_input: { name: 'run_python_sandboxed', input: { code } },
      display_name: 'run_python_sandboxed',
      display_input: { code },
      autonomy: 'confirm',
    })
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    const text = (pre as HTMLPreElement).textContent || ''
    expect(text).toContain('import os\nprint(os.getcwd())') // real newline
    expect(text).not.toContain('\\n') // not the JSON-escaped form
  })

  it('joins an array-of-lines code value for display in the confirm', () => {
    const { container } = render(<OutputPanel blocks={[chatBlockWithRunningTool('use_tool')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-a',
      tool_name: 'use_tool',
      tool_input: { name: 'run_python_sandboxed', input: { code: ['a = 2', 'print(a)'] } },
      display_name: 'run_python_sandboxed',
      display_input: { code: ['a = 2', 'print(a)'] },
      autonomy: 'confirm',
    })
    const text = (container.querySelector('pre') as HTMLPreElement).textContent || ''
    expect(text).toContain('a = 2\nprint(a)')
    expect(text).not.toContain('["a = 2"') // not shown as a raw JSON array
  })

  it('renders a card-level gate for a rule run with no tool segment, and answers it', async () => {
    render(<OutputPanel blocks={[ruleRunBlock('t-card-1')]} />)
    expect(screen.queryByRole('button', { name: /approve & run/i })).toBeNull()

    // A rule-mode trigger runnable is gated: the confirm is keyed to the run's
    // card id, and there is no running tool segment to host it.
    fireConfirm({
      id: 't-card-1',
      request_id: 'req-card',
      tool_name: 'drain-node',
      tool_input: { host: 'web-01' },
      display_name: 'drain-node',
      display_input: { host: 'web-01' },
      autonomy: 'confirm',
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^deny$/i })).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /approve & run/i }))
    // display_name === tool_name → editable, but nothing was changed → no edits.
    expect(resolveConfirm).toHaveBeenCalledWith('req-card', true, undefined)
  })

  it('clears the card-level gate on runspec:confirm_resolved (timeout / other console)', () => {
    render(<OutputPanel blocks={[ruleRunBlock('t-card-2')]} />)
    fireConfirm({
      id: 't-card-2',
      request_id: 'req-timeout',
      tool_name: 'drain-node',
      tool_input: { host: 'web-01' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
    // The backend resolves the gate itself (timeout / another console) — the gate
    // must clear without the operator clicking.
    fireResolved({ id: 't-card-2', request_id: 'req-timeout' })
    expect(screen.queryByRole('button', { name: /approve & run/i })).toBeNull()
  })

  it('does not double-render: an agent tool segment gate stays a single panel', () => {
    // A chat block whose running tool segment matches the confirm — the inline
    // segment gate handles it, so the card-level gate must NOT also draw.
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-single',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    // Exactly one Approve button, not two.
    expect(screen.getAllByRole('button', { name: /approve & run/i }).length).toBe(1)
  })

  it('falls back to "run" when the backend sends no confirm_verb', () => {
    // Back-compat: an older backend omits confirm_verb → the button stays
    // "Approve & run" (a runnable/executing tool, the safe default).
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-5',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeTruthy()
  })

  it('sends an optional decline reason with Deny so the agent can be redirected', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-deny',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    const box = screen.getByRole('textbox', { name: /reason for denying/i })
    await userEvent.type(box, '  drain web-02 instead  ')
    // The button says the reason goes along.
    await userEvent.click(screen.getByRole('button', { name: /deny with reason/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-deny', false, undefined, 'drain web-02 instead')
  })

  it('a plain Deny sends no reason', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-plain',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    await userEvent.type(screen.getByRole('textbox', { name: /reason for denying/i }), '   ')
    await userEvent.click(screen.getByRole('button', { name: /^deny$/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-plain', false, undefined)
  })

  it('Enter in the reason box denies with the reason; Shift+Enter is a newline', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-enter',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    const box = screen.getByRole('textbox', { name: /reason for denying/i })
    await userEvent.type(box, 'not now{Shift>}{Enter}{/Shift}check the queue first')
    expect(resolveConfirm).not.toHaveBeenCalled()
    await userEvent.type(box, '{Enter}')
    expect(resolveConfirm).toHaveBeenCalledWith('req-enter', false, undefined, 'not now\ncheck the queue first')
  })

  it('Approve ignores a typed reason', async () => {
    render(<OutputPanel blocks={[chatBlockWithRunningTool('run_runnable')]} />)
    fireConfirm({
      id: 'chat1',
      request_id: 'req-appr',
      tool_name: 'run_runnable',
      tool_input: { id: 'fleet__drain-node' },
      display_name: 'drain-node',
      autonomy: 'confirm',
    })
    await userEvent.type(screen.getByRole('textbox', { name: /reason for denying/i }), 'meh')
    await userEvent.click(screen.getByRole('button', { name: /approve & run/i }))
    expect(resolveConfirm).toHaveBeenCalledWith('req-appr', true, undefined)
  })
})
