import { describe, it, expect } from 'vitest'
import { transcriptToText } from './OutputPanel'
import type { BlockSegment, ToolCallEntry } from './OutputPanel'

const tool = (e: Partial<ToolCallEntry> & { name: string }): BlockSegment => ({
  kind: 'tool',
  entry: { input: {}, running: false, ...e },
})
const text = (t: string): BlockSegment => ({ kind: 'text', text: t })

describe('transcriptToText', () => {
  it('interleaves assistant text with tool calls and their output', () => {
    const out = transcriptToText([
      text('Let me deploy that.'),
      tool({ name: 'web1__deploy', input: { env: 'prod' }, output: 'rolled out' }),
      text('Done.'),
    ])
    expect(out).toContain('Let me deploy that.')
    expect(out).toContain('▶ deploy')          // display name has the prefix stripped
    expect(out).toContain('env=')               // the args are included
    expect(out).toContain('rolled out')         // the tool output is included
    expect(out).toContain('Done.')
  })

  it('marks a running tool call without output', () => {
    const out = transcriptToText([tool({ name: 'x__slow', running: true, output: undefined })])
    expect(out).toContain('(running…)')
  })

  it('shows (no output) for a finished tool call with empty output', () => {
    const out = transcriptToText([tool({ name: 'x__q', output: '' })])
    expect(out).toContain('(no output)')
  })

  it('appends the streaming tail text', () => {
    const out = transcriptToText([text('first')], 'still typing')
    expect(out).toContain('first')
    expect(out).toContain('still typing')
  })

  it('drops empty text segments', () => {
    const out = transcriptToText([text(''), tool({ name: 'a__b', output: 'z' })])
    expect(out.startsWith('▶')).toBe(true)
  })
})
