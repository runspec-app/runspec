import { useEffect, useRef, useState } from 'react'
import { Tag, Tooltip, Typography } from 'antd'
import { ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, RedoOutlined } from '@ant-design/icons'

const { Text } = Typography

export interface OutputLine {
  id: string
  line: string
  stream: 'stdout' | 'stderr'
}

export interface RerunData {
  host: string
  runnable: string
  args: Record<string, unknown>
}

export interface InvocationBlock {
  id: string
  type: 'run' | 'chat'
  label: string
  lines: OutputLine[]
  done: boolean
  exitCode?: number
  durationMs?: number
  rerunData?: RerunData
}

interface OutputPanelProps {
  blocks: InvocationBlock[]
  onRerun?: (data: RerunData) => void
}

export function OutputPanel({ blocks, onRerun }: OutputPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [blocks])

  if (blocks.length === 0) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555' }}>
        <div style={{ textAlign: 'center' }}>
          <ThunderboltOutlined style={{ fontSize: 32, marginBottom: 12, display: 'block' }} />
          <Text type="secondary">Type <Text code>/</Text> to run a runnable, or ask the LLM anything</Text>
        </div>
      </div>
    )
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      {blocks.map(block => (
        <BlockCard key={block.id} block={block} onRerun={onRerun} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}

function BlockCard({ block, onRerun }: { block: InvocationBlock; onRerun?: (data: RerunData) => void }) {
  const icon = block.done
    ? block.exitCode === 0
      ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
      : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
    : <LoadingOutlined style={{ color: '#1677ff' }} />

  return (
    <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 14px',
        background: '#1a1a1a',
        borderBottom: '1px solid #2a2a2a',
      }}>
        {icon}
        <Text style={{ color: '#d4d4d4', fontFamily: 'monospace', fontSize: 13 }}>{block.label}</Text>
        {block.done && block.durationMs !== undefined && (
          <Tag style={{ marginLeft: 'auto', fontSize: 11 }} color={block.exitCode === 0 ? 'green' : 'red'}>
            {block.exitCode === 0 ? 'ok' : 'error'} · {(block.durationMs / 1000).toFixed(2)}s
          </Tag>
        )}
        {block.done && block.rerunData && onRerun && (
          <Tooltip title="Rerun with same args">
            <RedoOutlined
              onClick={() => onRerun(block.rerunData!)}
              style={{ color: '#555', cursor: 'pointer', fontSize: 13, marginLeft: block.durationMs !== undefined ? 6 : 'auto' }}
              onMouseEnter={e => (e.currentTarget.style.color = '#d4d4d4')}
              onMouseLeave={e => (e.currentTarget.style.color = '#555')}
            />
          </Tooltip>
        )}
      </div>
      <pre style={{
        margin: 0, padding: '10px 14px',
        fontFamily: 'monospace', fontSize: 13,
        color: '#d4d4d4', lineHeight: 1.6,
        whiteSpace: 'pre-wrap', wordBreak: 'break-all',
      }}>
        {block.lines.map((l, i) => (
          <span key={i} style={{ color: l.stream === 'stderr' ? '#ff7875' : '#d4d4d4' }}>
            {l.line}{'\n'}
          </span>
        ))}
        {!block.done && <span style={{ color: '#555' }}>▌</span>}
      </pre>
    </div>
  )
}

export function useInvocationBlocks() {
  const [blocks, setBlocks] = useState<InvocationBlock[]>([])

  useEffect(() => {
    const onOutput = (e: Event) => {
      const { id, line, stream } = (e as CustomEvent).detail as OutputLine
      setBlocks(prev => prev.map(b =>
        b.id === id ? { ...b, lines: [...b.lines, { id, line, stream }] } : b
      ))
    }
    const onToken = (e: Event) => {
      const { id, token } = (e as CustomEvent).detail as { id: string; token: string }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        const last = b.lines[b.lines.length - 1]
        if (last) {
          const updated = [...b.lines.slice(0, -1), { ...last, line: last.line + token }]
          return { ...b, lines: updated }
        }
        return { ...b, lines: [{ id, line: token, stream: 'stdout' }] }
      }))
    }
    const onEnd = (e: Event) => {
      const { id, exit_code, duration_ms } = (e as CustomEvent).detail as { id: string; exit_code: number; duration_ms: number }
      setBlocks(prev => prev.map(b =>
        b.id === id ? { ...b, done: true, exitCode: exit_code, durationMs: duration_ms } : b
      ))
    }
    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:run_end', onEnd)
    return () => {
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:run_end', onEnd)
    }
  }, [])

  const addBlock = (block: InvocationBlock) => setBlocks(prev => [...prev, block])
  const clearBlocks = () => setBlocks([])

  return { blocks, addBlock, clearBlocks }
}
