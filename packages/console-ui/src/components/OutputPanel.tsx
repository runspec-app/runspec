import { useEffect, useRef, useState } from 'react'
import { Button, Input, Space, Tag, Tooltip, Typography, message } from 'antd'
import {
  ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  RedoOutlined, EditOutlined, CopyOutlined, RobotOutlined, CaretRightOutlined,
} from '@ant-design/icons'

const { Text } = Typography

function _lineColor(l: OutputLine): string {
  if (l.stream !== 'stderr') return '#d4d4d4'
  const m = l.line.match(/\((\d+) warnings?,\s*(\d+) errors?\)/)
  if (m) {
    const errors = parseInt(m[2])
    const warnings = parseInt(m[1])
    if (errors > 0) return '#ff7875'
    if (warnings > 0) return '#faad14'
    return '#52c41a'
  }
  return '#ff7875'
}

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
  startedAt: string  // ISO string captured when block is created
  lines: OutputLine[]
  done: boolean
  exitCode?: number
  durationMs?: number
  rerunData?: RerunData
}

interface OutputPanelProps {
  blocks: InvocationBlock[]
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
}

export function OutputPanel({ blocks, onRerun, onAskLlm }: OutputPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const isAtBottomRef = useRef(true)
  const blockCountRef = useRef(0)

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const isNewBlock = blocks.length > blockCountRef.current
    blockCountRef.current = blocks.length
    if (isNewBlock || isAtBottomRef.current) {
      el.scrollTop = el.scrollHeight
    }
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
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}
    >
      <div style={{ padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
        {blocks.map(block => (
          <BlockCard key={block.id} block={block} onRerun={onRerun} onAskLlm={onAskLlm} />
        ))}
      </div>
    </div>
  )
}

function BlockCard({
  block,
  onRerun,
  onAskLlm,
}: {
  block: InvocationBlock
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
}) {
  const [isEditing, setIsEditing] = useState(false)
  const [editArgs, setEditArgs] = useState<Record<string, string>>({})

  const args = block.rerunData?.args ?? {}
  const hasArgs = Object.keys(args).length > 0

  const icon = block.done
    ? block.exitCode === 0
      ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
      : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
    : <LoadingOutlined style={{ color: '#1677ff' }} />

  const startEditing = () => {
    setEditArgs(Object.fromEntries(Object.entries(args).map(([k, v]) => [k, String(v)])))
    setIsEditing(true)
  }

  const handleEditRun = () => {
    onRerun?.({ ...block.rerunData!, args: editArgs })
    setIsEditing(false)
  }

  const logText = block.lines.map(l => l.line).join('\n')

  const handleCopy = () => {
    navigator.clipboard.writeText(`Output from ${block.label}\n\n${logText}`)
    message.success('Copied to clipboard')
  }

  const handleAskLlm = () => {
    onAskLlm?.(`Output from ${block.label}\n\n${logText}`)
  }

  return (
    <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 14px',
        background: '#1a1a1a',
        borderBottom: '1px solid #2a2a2a',
      }}>
        {icon}
        <Text style={{ color: '#d4d4d4', fontFamily: 'monospace', fontSize: 13 }}>{block.label}</Text>
        <Text style={{ color: '#555', fontSize: 11, marginLeft: 6 }}>
          {new Date(block.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </Text>
        {block.done && block.durationMs !== undefined && (
          <Tag style={{ marginLeft: 'auto', fontSize: 11 }} color={block.exitCode === 0 ? 'green' : 'red'}>
            {block.exitCode === 0 ? 'ok' : 'error'} · {(block.durationMs / 1000).toFixed(2)}s
          </Tag>
        )}
        {block.done && block.rerunData && onRerun && (
          <>
            {hasArgs && !isEditing && (
              <Tooltip title="Edit args and rerun">
                <EditOutlined
                  onClick={startEditing}
                  style={{
                    color: '#555', cursor: 'pointer', fontSize: 13,
                    marginLeft: block.durationMs !== undefined ? 6 : 'auto',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.color = '#d4d4d4')}
                  onMouseLeave={e => (e.currentTarget.style.color = '#555')}
                />
              </Tooltip>
            )}
            <Tooltip title="Rerun with same args">
              <RedoOutlined
                onClick={() => onRerun(block.rerunData!)}
                style={{
                  color: '#555', cursor: 'pointer', fontSize: 13,
                  marginLeft: (!hasArgs || isEditing) && block.durationMs !== undefined ? 6 : (hasArgs ? 6 : 'auto'),
                }}
                onMouseEnter={e => (e.currentTarget.style.color = '#d4d4d4')}
                onMouseLeave={e => (e.currentTarget.style.color = '#555')}
              />
            </Tooltip>
          </>
        )}
      </div>

      {/* Edit args form */}
      {isEditing && (
        <div style={{
          padding: '12px 14px',
          borderBottom: '1px solid #2a2a2a',
          background: 'rgba(255,255,255,0.02)',
        }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {Object.entries(editArgs).map(([k, v]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <Text code style={{ fontSize: 12, minWidth: 140, flexShrink: 0 }}>--{k}</Text>
                <Input
                  size="small"
                  value={v}
                  onChange={e => setEditArgs(a => ({ ...a, [k]: e.target.value }))}
                  style={{ maxWidth: 340, fontFamily: 'monospace', fontSize: 12 }}
                />
              </div>
            ))}
          </div>
          <Space style={{ marginTop: 10 }}>
            <Button size="small" type="primary" icon={<CaretRightOutlined />} onClick={handleEditRun}>
              Run with edits
            </Button>
            <Button size="small" onClick={() => setIsEditing(false)}>Cancel</Button>
          </Space>
        </div>
      )}

      {/* Log output */}
      <div style={{ position: 'relative' }}>
        {block.lines.length > 0 && (
          <div style={{ position: 'absolute', top: 5, right: 6, display: 'flex', gap: 2, zIndex: 1 }}>
            <Tooltip title="Copy to clipboard">
              <Button
                type="text" size="small" icon={<CopyOutlined />}
                onClick={handleCopy}
                style={{ color: '#555', height: 22, padding: '0 5px' }}
              />
            </Tooltip>
            {onAskLlm && (
              <Tooltip title="Ask the LLM">
                <Button
                  type="text" size="small" icon={<RobotOutlined />}
                  onClick={handleAskLlm}
                  style={{ color: '#555', height: 22, padding: '0 5px' }}
                />
              </Tooltip>
            )}
          </div>
        )}
        <pre style={{
          margin: 0, padding: '10px 14px',
          fontFamily: 'monospace', fontSize: 13,
          color: '#d4d4d4', lineHeight: 1.6,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          maxHeight: 480, overflowY: 'auto',
        }}>
          {block.lines.map((l, i) => (
            <span key={i} style={{ color: _lineColor(l) }}>
              {l.line}{'\n'}
            </span>
          ))}
          {!block.done && <span style={{ color: '#555' }}>▌</span>}
        </pre>
      </div>
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
