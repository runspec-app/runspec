import { useEffect, useRef, useState } from 'react'
import { Tag, Typography } from 'antd'
import { LoadingOutlined } from '@ant-design/icons'
import { bridge, type InFlightRecord, type Runnable } from '../bridge'
import { CommandInput } from '../components/CommandInput'
import { OutputPanel, useInvocationBlocks } from '../components/OutputPanel'

const { Text } = Typography

interface ConsoleViewProps {
  inFlight: InFlightRecord[]
}

function elapsed(startedAt: string): string {
  const ms = Date.now() - new Date(startedAt).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function InFlightStrip({ inFlight }: { inFlight: InFlightRecord[] }) {
  // Re-render every second so elapsed time ticks
  const [, setTick] = useState(0)
  const ref = useRef<ReturnType<typeof setInterval>>()
  useEffect(() => {
    ref.current = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(ref.current)
  }, [])

  if (inFlight.length === 0) return null

  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 8,
      padding: '8px 12px',
      background: '#0d1f0d',
      border: '1px solid #1a3a1a',
      borderRadius: 6,
      marginBottom: 4,
    }}>
      {inFlight.map(r => (
        <span key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <LoadingOutlined style={{ color: '#52c41a', fontSize: 11 }} spin />
          <Text style={{ fontFamily: 'monospace', fontSize: 12, color: '#d4d4d4' }}>
            /{r.runnable}
          </Text>
          <Tag style={{ fontSize: 11, margin: 0 }}>{r.host}</Tag>
          <Text type="secondary" style={{ fontSize: 11 }}>{r.operator}</Text>
          <Text style={{ fontSize: 11, color: '#52c41a' }}>{elapsed(r.startedAt)}</Text>
        </span>
      ))}
    </div>
  )
}

export function ConsoleView({ inFlight }: ConsoleViewProps) {
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const { blocks, addBlock } = useInvocationBlocks()

  useEffect(() => {
    bridge.get_runnables('local').then(setRunnables)
  }, [])

  const handleRunRunnable = async (runnable: Runnable, args: Record<string, unknown>) => {
    const label = `/${runnable.name} on ${runnable.host}`
    setInputHistory(h => [...h, `/${runnable.name}`])
    const id = await bridge.invoke_runnable(runnable.host, runnable.name, args)
    addBlock({ id, type: 'run', label, lines: [], done: false })
  }

  const handleSendChat = async (message: string) => {
    setInputHistory(h => [...h, message])
    const id = await bridge.send_chat(message)
    addBlock({ id, type: 'chat', label: message, lines: [], done: false })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 8 }}>
      <InFlightStrip inFlight={inFlight} />
      <OutputPanel blocks={blocks} />
      <CommandInput
        runnables={runnables}
        onRunRunnable={handleRunRunnable}
        onSendChat={handleSendChat}
        history={inputHistory}
      />
    </div>
  )
}
