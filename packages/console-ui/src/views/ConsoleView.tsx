import { useEffect, useRef, useState } from 'react'
import { Tag, Typography } from 'antd'
import { LoadingOutlined } from '@ant-design/icons'
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
  const ref = useRef<ReturnType<typeof setInterval>>()
  useEffect(() => {
    ref.current = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(ref.current)
  }, [])

  if (inFlight.length === 0) return null

  const bg     = isDark ? '#0d1f0d' : '#f6ffed'
  const border = isDark ? '#1a3a1a' : '#b7eb8f'
  const nameCol = isDark ? '#d4d4d4' : '#1a1a1a'

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
          <Tag style={{ fontSize: 11, margin: 0 }}>{r.host}</Tag>
          <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>{r.group}</Tag>
          <Text type="secondary" style={{ fontSize: 11 }}>{r.operator}</Text>
          <Text style={{ fontSize: 11, color: '#52c41a' }}>{elapsed(r.startedAt)}</Text>
        </span>
      ))}
    </div>
  )
}

export function ConsoleView({ inFlight, pendingChat, onChatSent }: ConsoleViewProps) {
  const { blocks, addBlock } = useInvocationBlocks()
  const addBlockRef = useRef(addBlock)
  addBlockRef.current = addBlock

  // Invoked from the App-level command bar
  useEffect(() => {
    const onInvoke = async (e: Event) => {
      const { runnable, args, commandPath = [] } = (e as CustomEvent).detail as { runnable: Runnable; args: Record<string, unknown>; commandPath?: string[] }
      const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
      const label = `/${cmd} on ${runnable.host}`
      const id = await bridge.invoke_runnable(runnable.host, runnable.name, args, commandPath)
      addBlockRef.current({ id, type: 'run', label, startedAt: new Date().toISOString(), lines: [], done: false, rerunData: { host: runnable.host, runnable: runnable.name, args } })
    }
    window.addEventListener('runspec:invoke_runnable', onInvoke)
    return () => window.removeEventListener('runspec:invoke_runnable', onInvoke)
  }, [])

  // Chat sent from the App-level command bar
  useEffect(() => {
    const onChat = async (e: Event) => {
      const { message } = (e as CustomEvent).detail as { message: string }
      const id = await bridge.send_chat(message)
      addBlockRef.current({ id, type: 'chat', label: message, startedAt: new Date().toISOString(), lines: [], done: false })
    }
    window.addEventListener('runspec:send_chat', onChat)
    return () => window.removeEventListener('runspec:send_chat', onChat)
  }, [])

  // Rerun dispatched by History → App → here
  useEffect(() => {
    const onRerun = async (e: Event) => {
      const { host, runnable, args } = (e as CustomEvent).detail as RerunData
      const label = `/${runnable} on ${host} (rerun)`
      const id = await bridge.invoke_runnable(host, runnable, args)
      addBlockRef.current({ id, type: 'run', label, startedAt: new Date().toISOString(), lines: [], done: false, rerunData: { host, runnable, args } })
    }
    window.addEventListener('runspec:rerun', onRerun)
    return () => window.removeEventListener('runspec:rerun', onRerun)
  }, [])

  // Ask LLM from History: pendingChat prop triggers a chat send
  const sendChat = async (message: string) => {
    const id = await bridge.send_chat(message)
    addBlockRef.current({ id, type: 'chat', label: message, startedAt: new Date().toISOString(), lines: [], done: false })
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
      <OutputPanel blocks={blocks} onRerun={handleRerun} onAskLlm={handleAskLlm} />
    </div>
  )
}
