import { describe, it, expect } from 'vitest'
import { conversationToBlocks } from './ConsoleView'
import type { ChatConversation } from '../bridge'

const conv = (over: Partial<ChatConversation>): ChatConversation =>
  ({ id: 'c1', title: 't', turns: [], ...over })

describe('conversationToBlocks', () => {
  it('maps each turn to a read-only chat block (label=user, tools then assistant text)', () => {
    const blocks = conversationToBlocks(conv({
      turns: [
        { user: 'do it', assistant: 'done', tools: [{ name: 'deploy', input: { env: 'prod' }, output: 'ok' }] },
        { user: 'thanks', assistant: 'np', tools: [] },
      ],
    }))
    expect(blocks).toHaveLength(2)
    const b = blocks[0]
    expect(b.type).toBe('chat')
    expect(b.label).toBe('do it')
    expect(b.done).toBe(true)
    expect(b.id).toBe('c1:0')
    expect(b.segments[0]).toMatchObject({ kind: 'tool', entry: { name: 'deploy', output: 'ok', running: false } })
    expect(b.segments[1]).toEqual({ kind: 'text', text: 'done' })
  })

  it('omits the text segment when the assistant said nothing', () => {
    const blocks = conversationToBlocks(conv({ turns: [{ user: 'hi', assistant: '', tools: [] }] }))
    expect(blocks[0].segments).toEqual([])
  })

  it('maps usage to the block tokenUsage shape', () => {
    const blocks = conversationToBlocks(conv({
      turns: [{ user: 'q', assistant: 'a', tools: [], usage: { input: 10, output: 5, cache_read: 2, cache_creation: 1 } }],
    }))
    expect(blocks[0].tokenUsage).toEqual({ input: 10, output: 5, cacheRead: 2, cacheCreation: 1 })
  })
})
