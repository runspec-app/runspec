import { useEffect, useRef, useState } from 'react'
import { Button, Popconfirm, Tag, Tooltip, Typography, message } from 'antd'
import { CloseOutlined, LoadingOutlined, PlusOutlined, ClockCircleOutlined } from '@ant-design/icons'
import { bridge, type InFlightRecord, type Runnable } from '../bridge'
import { OutputPanel, useInvocationBlocks, type RerunData } from '../components/OutputPanel'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

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
      {inFlight.map(r => (
        <span key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <LoadingOutlined style={{ color: '#52c41a', fontSize: 11 }} spin />
          <Text style={{ fontFamily: 'monospace', fontSize: 12, color: nameCol }}>/{r.runnable}</Text>
          {r.trigger === 'schedule' && (
            <Tooltip title="Ran on a schedule">
              <ClockCircleOutlined style={{ color: '#8c8c8c', fontSize: 11 }} />
            </Tooltip>
          )}
          <Tag style={{ fontSize: 11, margin: 0 }}>{r.host}</Tag>
          <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>{r.group}</Tag>
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
      ))}
    </div>
  )
}

export function ConsoleView({ inFlight, pendingChat, onChatSent }: ConsoleViewProps) {
  const { blocks, addBlock, clearBlocks } = useInvocationBlocks()
  const addBlockRef = useRef(addBlock)
  addBlockRef.current = addBlock

  // Reset the conversation: clear the transcript and the model's memory of
  // prior turns (the agent chat is otherwise one rolling conversation).
  const handleNewChat = async () => {
    await bridge.clear_chat()
    clearBlocks()
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

  // A skill fired from the `$` menu's run modal: the bridge renders the body with
  // the operator's values + tags the turn with the skill, then runs it like a chat
  // turn. We show the rendered message as the chat block label.
  useEffect(() => {
    const onRunSkill = async (e: Event) => {
      const { skillId, values, extra } = (e as CustomEvent).detail as {
        skillId: string; values: Record<string, unknown>; extra: string
      }
      const res = await bridge.run_skill(skillId, values, extra)
      if (res.error || !res.chat_id) {
        message.error(res.error || 'Could not start skill')
        return
      }
      addBlockRef.current({ id: res.chat_id, type: 'chat', label: res.message || `$${skillId}`, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:run_skill', onRunSkill)
    return () => window.removeEventListener('runspec:run_skill', onRunSkill)
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
      {blocks.length > 0 && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 6 }}>
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
          <Tooltip title="Clear the transcript and the assistant's memory of this conversation">
            <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat}>
              New chat
            </Button>
          </Tooltip>
        </div>
      )}
      <OutputPanel blocks={blocks} onRerun={handleRerun} onAskLlm={handleAskLlm} />
    </div>
  )
}
