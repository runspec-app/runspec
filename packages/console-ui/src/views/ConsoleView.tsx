import { useEffect, useRef, useState } from 'react'
import { Button, Drawer, Empty, List, Popconfirm, Tag, Tooltip, Typography, message } from 'antd'
import { CloseOutlined, LoadingOutlined, PlusOutlined, ClockCircleOutlined, HistoryOutlined, DeleteOutlined } from '@ant-design/icons'
import { bridge, type ChatConversation, type ConversationMeta, type InFlightRecord, type Runnable } from '../bridge'
import { OutputPanel, useInvocationBlocks, type RerunData, type InvocationBlock, type BlockSegment } from '../components/OutputPanel'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

// Render a saved conversation's provider-agnostic transcript back into chat
// blocks the OutputPanel can show — one block per turn (label = the user's
// message; tool calls + the assistant's prose as segments). Read-only: every
// block is `done`, nothing streams.
export function conversationToBlocks(conv: ChatConversation): InvocationBlock[] {
  return (conv.turns || []).map((t, i) => {
    const segments: BlockSegment[] = []
    for (const tool of t.tools || []) {
      segments.push({ kind: 'tool', entry: { name: tool.name, input: tool.input || {}, output: tool.output, running: false } })
    }
    if (t.assistant && t.assistant.trim()) segments.push({ kind: 'text', text: t.assistant })
    const u = (t.usage || undefined) as Record<string, number> | undefined
    return {
      id: `${conv.id}:${i}`,
      type: 'chat' as const,
      label: t.user || '(no message)',
      startedAt: t.ts || conv.updated || new Date().toISOString(),
      lines: [],
      segments,
      currentText: '',
      done: true,
      model: t.model,
      tokenUsage: u ? { input: u.input || 0, output: u.output || 0, cacheRead: u.cache_read || 0, cacheCreation: u.cache_creation || 0 } : undefined,
    }
  })
}

function fmtWhen(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

interface ConsoleViewProps {
  inFlight: InFlightRecord[]
  pendingChat?: string | null
  onChatSent?: () => void
}

function elapsed(startedAt: string): string {
  const ms = Date.now() - new Date(startedAt).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function InFlightStrip({ inFlight }: { inFlight: InFlightRecord[] }) {
  const isDark = useIsDark()
  const [, setTick] = useState(0)
  const ref = useRef<ReturnType<typeof setInterval> | undefined>(undefined)
  useEffect(() => {
    ref.current = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(ref.current)
  }, [])

  if (inFlight.length === 0) return null

  const bg     = isDark ? '#0d1f0d' : '#f6ffed'
  const border = isDark ? '#1a3a1a' : '#b7eb8f'
  const nameCol = isDark ? '#d4d4d4' : '#1a1a1a'

  const handleCancel = async (id: string) => {
    await bridge.cancel_invocation(id)
  }

  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 8,
      padding: '8px 12px',
      background: bg, border: `1px solid ${border}`, borderRadius: 6,
      marginBottom: 4,
    }}>
      {inFlight.map(r => {
        // Built-in agent tools (repo_*, outlook_*, denylist, …) show their label
        // verbatim ("repo_sync: runspec"); a runnable run keeps the /name form.
        const isTool = r.kind === 'builtin_tool'
        return (
        <span key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <LoadingOutlined style={{ color: '#52c41a', fontSize: 11 }} spin />
          <Text style={{ fontFamily: 'monospace', fontSize: 12, color: nameCol }}>{isTool ? r.runnable : `/${r.runnable}`}</Text>
          {isTool && (
            <Tag color="green" style={{ fontSize: 11, margin: 0 }}>agent</Tag>
          )}
          {r.trigger === 'schedule' && (
            <Tooltip title="Ran on a schedule">
              <ClockCircleOutlined style={{ color: '#8c8c8c', fontSize: 11 }} />
            </Tooltip>
          )}
          {r.host && <Tag style={{ fontSize: 11, margin: 0 }}>{r.host}</Tag>}
          {r.group && <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>{r.group}</Tag>}
          <Text type="secondary" style={{ fontSize: 11 }}>{r.operator}</Text>
          <Text style={{ fontSize: 11, color: '#52c41a' }}>{elapsed(r.startedAt)}</Text>
          <Popconfirm
            title="Cancel this run?"
            onConfirm={() => handleCancel(r.id)}
            okText="Cancel run" okButtonProps={{ danger: true }}
            cancelText="Keep running"
            placement="bottom"
          >
            <Button size="small" type="text" danger icon={<CloseOutlined style={{ fontSize: 10 }} />}
              style={{ padding: '0 4px', height: 18, lineHeight: 1 }} />
          </Popconfirm>
        </span>
        )
      })}
    </div>
  )
}

export function ConsoleView({ inFlight, pendingChat, onChatSent }: ConsoleViewProps) {
  const { blocks, addBlock, clearBlocks, hydrateBlocks } = useInvocationBlocks()
  const addBlockRef = useRef(addBlock)
  addBlockRef.current = addBlock

  // Saved-conversation browser (chat history) state.
  const [convOpen, setConvOpen] = useState(false)
  const [convs, setConvs] = useState<ConversationMeta[]>([])
  const [convLoading, setConvLoading] = useState(false)
  // The reopened-and-resumed conversation, if any — new messages continue it.
  const [viewing, setViewing] = useState<{ id: string; title: string } | null>(null)

  const refreshConvs = async () => {
    setConvLoading(true)
    try {
      setConvs(await bridge.list_conversations())
    } catch {
      setConvs([])
    } finally {
      setConvLoading(false)
    }
  }

  const openConversations = async () => {
    setConvOpen(true)
    await refreshConvs()
  }

  // Reopen a saved conversation: render its transcript and resume it on the
  // backend so the next message continues it (history rebuilt as text).
  const openConversation = async (meta: ConversationMeta) => {
    try {
      const conv = await bridge.load_conversation(meta.id)
      hydrateBlocks(conversationToBlocks(conv))
      const res = await bridge.resume_conversation(meta.id)
      if (!res.ok) { message.error(res.error || 'Could not resume conversation'); return }
      setViewing({ id: meta.id, title: conv.title || meta.title })
      setConvOpen(false)
      if (res.compacted) {
        message.info(res.summarized
          ? 'Resumed — older turns were summarized to fit context (the saved transcript is unchanged).'
          : 'Resumed — older turns were condensed to fit context (the saved transcript is unchanged).')
      }
    } catch {
      message.error('Could not open conversation')
    }
  }

  const deleteConversation = async (id: string) => {
    await bridge.delete_conversation(id)
    if (viewing?.id === id) { await bridge.clear_chat(); clearBlocks(); setViewing(null) }
    await refreshConvs()
  }

  // Reset the conversation: clear the transcript and the model's memory of
  // prior turns (the agent chat is otherwise one rolling conversation).
  const handleNewChat = async () => {
    await bridge.clear_chat()
    clearBlocks()
    setViewing(null)
  }

  // Tell the command bar which chat turn (if any) is in flight, so its send
  // button can become a Stop control while the assistant is working.
  const activeChatId = blocks.find(b => b.type === 'chat' && !b.done)?.id ?? null
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('runspec:chat_active', { detail: { id: activeChatId } }))
  }, [activeChatId])

  // Invoked from the App-level command bar
  useEffect(() => {
    const onInvoke = async (e: Event) => {
      const { runnable, args, commandPath = [], credentialIds } = (e as CustomEvent).detail as { runnable: Runnable; args: Record<string, unknown>; commandPath?: string[]; credentialIds?: string[] }
      const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
      const label = `/${cmd} on ${runnable.host}`
      const id = await bridge.invoke_runnable(runnable.host, runnable.name, args, commandPath, runnable.group, credentialIds && credentialIds.length ? credentialIds : undefined)
      addBlockRef.current({ id, type: 'run', label, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false, rerunData: { host: runnable.host, runnable: runnable.name, args, commandPath } })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:invoke_runnable', onInvoke)
    return () => window.removeEventListener('runspec:invoke_runnable', onInvoke)
  }, [])

  // Chat sent from the App-level command bar
  useEffect(() => {
    const onChat = async (e: Event) => {
      const { message } = (e as CustomEvent).detail as { message: string }
      const id = await bridge.send_chat(message)
      addBlockRef.current({ id, type: 'chat', label: message, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:send_chat', onChat)
    return () => window.removeEventListener('runspec:send_chat', onChat)
  }, [])

  // A runbook fired from the `$` menu's run modal: the bridge renders the body with
  // the operator's values + tags the turn with the runbook, then runs it like a chat
  // turn. We show the rendered message as the chat block label.
  useEffect(() => {
    const onRunRunbook = async (e: Event) => {
      const { runbookId, values, extra } = (e as CustomEvent).detail as {
        runbookId: string; values: Record<string, unknown>; extra: string
      }
      const res = await bridge.run_runbook(runbookId, values, extra)
      if (res.error || !res.chat_id) {
        message.error(res.error || 'Could not start runbook')
        return
      }
      addBlockRef.current({ id: res.chat_id, type: 'chat', label: res.message || `$${runbookId}`, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:run_runbook', onRunRunbook)
    return () => window.removeEventListener('runspec:run_runbook', onRunRunbook)
  }, [])

  // post_to_chat triggers mirror their run into the Console transcript as a
  // finished block matching the trigger mode, so a triggered run looks just
  // like the same command run by hand — only the source differs:
  //   kind="run"  → a standard slash-command run card (mode=rule)
  //   kind="chat" → an agent/chat card (mode=agent)
  // (The bridge dispatches runspec:trigger_note with id/kind/label/text.)
  useEffect(() => {
    const onTriggerNote = (e: Event) => {
      const { id, kind, label, text } = (e as CustomEvent).detail as {
        id?: string; kind?: string; label?: string; text?: string
      }
      if (!text && !label) return
      const blockId = id || `trigger-${Date.now()}`
      const startedAt = new Date().toISOString()
      if (kind === 'run') {
        // Render the runnable output as run-block lines; surface a non-zero exit
        // from the "[exit N]" prefix the gate adds so it reads like a real run.
        let body = text || ''
        let exitCode = 0
        const m = /^\[exit (\d+)\]\n?/.exec(body)
        if (m) { exitCode = Number(m[1]); body = body.slice(m[0].length) }
        const lines = body
          ? body.split('\n').map(line => ({ id: blockId, line, stream: 'stdout' as const }))
          : []
        addBlockRef.current({
          id: blockId, type: 'run', label: label || 'trigger run',
          startedAt, lines, segments: [], currentText: '',
          done: true, exitCode,
        })
      } else {
        addBlockRef.current({
          id: blockId, type: 'chat', label: label || 'trigger',
          startedAt, lines: [], segments: [{ kind: 'text', text: text || '' }],
          currentText: '', done: true,
        })
      }
    }
    window.addEventListener('runspec:trigger_note', onTriggerNote)
    return () => window.removeEventListener('runspec:trigger_note', onTriggerNote)
  }, [])

  // Rerun dispatched by History → App → here
  useEffect(() => {
    const onRerun = async (e: Event) => {
      const { host, runnable, args, commandPath = [] } = (e as CustomEvent).detail as RerunData
      const cmd = commandPath.length > 0 ? `${runnable} ${commandPath.join(' ')}` : runnable
      const label = `/${cmd} on ${host} (rerun)`
      const id = await bridge.invoke_runnable(host, runnable, args, commandPath)
      addBlockRef.current({ id, type: 'run', label, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false, rerunData: { host, runnable, args, commandPath } })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:rerun', onRerun)
    return () => window.removeEventListener('runspec:rerun', onRerun)
  }, [])

  // Ask LLM from History: pendingChat prop triggers a chat send
  const sendChat = async (message: string) => {
    const id = await bridge.send_chat(message)
    addBlockRef.current({ id, type: 'chat', label: message, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
  }
  const sendChatRef = useRef(sendChat)
  sendChatRef.current = sendChat

  useEffect(() => {
    if (pendingChat) {
      sendChatRef.current(pendingChat)
      onChatSent?.()
    }
  }, [pendingChat, onChatSent])

  const handleRerun = (data: RerunData) => {
    window.dispatchEvent(new CustomEvent('runspec:rerun', { detail: data }))
  }

  const handleAskLlm = (text: string) => sendChatRef.current(text)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, gap: 8 }}>
      <InFlightStrip inFlight={inFlight} />
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 6 }}>
        {viewing && (
          <Tag color="blue" style={{ fontSize: 11, marginRight: 'auto', maxWidth: '60%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            Resumed: {viewing.title} — your next message continues it
          </Tag>
        )}
        {blocks.length > 0 && (
          <>
            <Button
              size="small" type="text"
              onClick={() => window.dispatchEvent(new CustomEvent('runspec:collapse_all', { detail: { collapsed: true } }))}
              style={{ fontSize: 11, color: '#888' }}
            >
              Collapse all
            </Button>
            <Button
              size="small" type="text"
              onClick={() => window.dispatchEvent(new CustomEvent('runspec:collapse_all', { detail: { collapsed: false } }))}
              style={{ fontSize: 11, color: '#888' }}
            >
              Expand all
            </Button>
          </>
        )}
        <Tooltip title="Browse and reopen past conversations">
          <Button size="small" type="text" icon={<HistoryOutlined />} onClick={openConversations} style={{ fontSize: 11, color: '#888' }}>
            History
          </Button>
        </Tooltip>
        {blocks.length > 0 && (
          <Tooltip title="Clear the transcript and the assistant's memory of this conversation">
            <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat}>
              New chat
            </Button>
          </Tooltip>
        )}
      </div>
      <OutputPanel blocks={blocks} onRerun={handleRerun} onAskLlm={handleAskLlm} />

      <Drawer
        title="Conversations"
        placement="right"
        width={420}
        open={convOpen}
        onClose={() => setConvOpen(false)}
        styles={{ body: { padding: '8px 0' } }}
      >
        {convs.length === 0 && !convLoading ? (
          <Empty description="No saved conversations yet" style={{ marginTop: 40 }} />
        ) : (
          <List
            loading={convLoading}
            dataSource={convs}
            renderItem={(c) => (
              <List.Item
                style={{ padding: '8px 16px', cursor: 'pointer' }}
                onClick={() => openConversation(c)}
                actions={[
                  <Popconfirm
                    key="del"
                    title="Delete this conversation?"
                    onConfirm={(e) => { e?.stopPropagation(); deleteConversation(c.id) }}
                    onCancel={(e) => e?.stopPropagation()}
                    okText="Delete" okButtonProps={{ danger: true }}
                  >
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Text
                      style={{ fontSize: 13 }}
                      editable={{
                        onChange: (v) => { const t = v.trim(); if (t && t !== c.title) { bridge.rename_conversation(c.id, t).then(refreshConvs) } },
                        triggerType: ['icon'],
                      }}
                    >
                      {c.title || 'Untitled'}
                    </Text>
                  }
                  description={
                    <div>
                      <Text type="secondary" style={{ fontSize: 11 }} ellipsis>{c.preview}</Text>
                      <div style={{ fontSize: 10, color: '#999', marginTop: 2 }}>
                        {fmtWhen(c.updated)} · {c.turn_count} message{c.turn_count === 1 ? '' : 's'}
                      </div>
                    </div>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </div>
  )
}
