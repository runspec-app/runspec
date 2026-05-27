import { useEffect, useRef, useState } from 'react'
import { Button, Input, Space, Tag, Tooltip, Typography, message } from 'antd'
import {
  ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  RedoOutlined, EditOutlined, CopyOutlined, RobotOutlined, CaretRightOutlined,
  ApiOutlined, DownOutlined, RightOutlined, VerticalAlignBottomOutlined,
} from '@ant-design/icons'

const { Text } = Typography

const TRUNCATE_AT = 20

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

export interface ToolCallEntry {
  name: string
  input: Record<string, unknown>
  output?: string
  running: boolean
}

export type BlockSegment =
  | { kind: 'text'; text: string }
  | { kind: 'tool'; entry: ToolCallEntry }

export interface InvocationBlock {
  id: string
  type: 'run' | 'chat'
  label: string
  startedAt: string  // ISO string captured when block is created
  lines: OutputLine[]
  segments: BlockSegment[]   // for chat blocks — interleaved text + tool calls
  currentText: string        // currently-streaming text (chat blocks only)
  done: boolean
  exitCode?: number
  durationMs?: number
  rerunData?: RerunData
  tokenUsage?: { input: number; output: number }
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
  const [showScrollDown, setShowScrollDown] = useState(false)

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
    isAtBottomRef.current = atBottom
    setShowScrollDown(!atBottom)
  }

  const scrollToBottom = () => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
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
    <div style={{ position: 'relative', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
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

      {/* Jump-to-bottom pill — appears when scrolled up */}
      {showScrollDown && (
        <div style={{ position: 'absolute', bottom: 12, left: '50%', transform: 'translateX(-50%)', zIndex: 10 }}>
          <Button
            size="small"
            icon={<VerticalAlignBottomOutlined />}
            onClick={scrollToBottom}
            style={{
              background: '#1a1a1a', borderColor: '#333', color: '#888',
              fontSize: 11, height: 26, padding: '0 10px',
              boxShadow: '0 2px 8px rgba(0,0,0,0.5)',
            }}
          >
            latest
          </Button>
        </div>
      )}
    </div>
  )
}

function ToolCallBlock({ entry }: { entry: ToolCallEntry }) {
  const [expanded, setExpanded] = useState(false)
  const args = Object.entries(entry.input)

  return (
    <div style={{ margin: '6px 0', border: '1px solid #2a2a2a', borderRadius: 6, overflow: 'hidden' }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px',
          background: '#1a1a1a',
          cursor: entry.output !== undefined ? 'pointer' : 'default',
          userSelect: 'none',
        }}
        onClick={() => entry.output !== undefined && setExpanded(x => !x)}
      >
        {entry.running
          ? <LoadingOutlined style={{ color: '#1677ff', fontSize: 12 }} />
          : <ApiOutlined style={{ color: '#52c41a', fontSize: 12 }} />}
        <Text code style={{ fontSize: 12 }}>{entry.name.replace(/^[^_]+__/, '')}</Text>
        {args.length > 0 && (
          <Text style={{ color: '#555', fontSize: 11 }}>
            {args.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}
          </Text>
        )}
        {entry.running && (
          <Text style={{ color: '#555', fontSize: 11, marginLeft: 'auto' }}>running…</Text>
        )}
        {entry.output !== undefined && !entry.running && (
          <Text style={{ color: '#555', fontSize: 11, marginLeft: 'auto' }}>
            {expanded ? '▲ hide output' : '▼ show output'}
          </Text>
        )}
      </div>
      {expanded && entry.output !== undefined && (
        <pre style={{
          margin: 0, padding: '8px 12px',
          fontFamily: 'monospace', fontSize: 12,
          color: '#d4d4d4', lineHeight: 1.5,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          background: '#141414',
        }}>
          {entry.output || '(no output)'}
        </pre>
      )}
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
  const [collapsed, setCollapsed] = useState(false)
  const [showAll, setShowAll] = useState(false)

  const args = block.rerunData?.args ?? {}
  const hasArgs = Object.keys(args).length > 0

  // Truncation — only for completed run blocks with many lines
  const extraLines = block.lines.length - TRUNCATE_AT
  const shouldTruncate = block.type === 'run' && block.done && !showAll && extraLines > 0
  const displayLines = shouldTruncate ? block.lines.slice(0, TRUNCATE_AT) : block.lines

  const statusIcon = block.done
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

  const logText = block.type === 'chat'
    ? [
        ...block.segments.filter((s): s is Extract<BlockSegment, { kind: 'text' }> => s.kind === 'text').map(s => s.text),
        block.currentText,
      ].filter(Boolean).join('\n')
    : block.lines.map(l => l.line).join('\n')

  const handleCopy = () => {
    navigator.clipboard.writeText(`Output from ${block.label}\n\n${logText}`)
    message.success('Copied to clipboard')
  }

  const handleAskLlm = () => {
    onAskLlm?.(`Output from ${block.label}\n\n${logText}`)
  }

  const hasContent = block.type === 'chat'
    ? (block.segments.length > 0 || block.currentText.length > 0)
    : block.lines.length > 0

  return (
    <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 14px',
        background: '#1a1a1a',
        borderBottom: collapsed ? 'none' : '1px solid #2a2a2a',
        userSelect: 'none',
      }}>
        {/* Collapse toggle */}
        <span
          onClick={() => setCollapsed(c => !c)}
          style={{ color: '#444', cursor: 'pointer', fontSize: 11, flexShrink: 0, width: 14 }}
        >
          {collapsed
            ? <RightOutlined style={{ fontSize: 10 }} />
            : <DownOutlined style={{ fontSize: 10 }} />}
        </span>

        {statusIcon}
        <Text
          style={{ color: '#d4d4d4', fontFamily: 'monospace', fontSize: 13, cursor: 'pointer', flex: 1 }}
          onClick={() => setCollapsed(c => !c)}
        >
          {block.label}
        </Text>
        <Text style={{ color: '#555', fontSize: 11 }}>
          {new Date(block.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </Text>
        {block.done && block.durationMs !== undefined && (
          <Tag style={{ marginLeft: 4, fontSize: 11 }} color={block.exitCode === 0 ? 'green' : 'red'}>
            {block.exitCode === 0 ? 'ok' : 'error'} · {(block.durationMs / 1000).toFixed(2)}s
          </Tag>
        )}
        {block.tokenUsage && (
          <Tooltip title={`Input tokens: ${block.tokenUsage.input.toLocaleString()} · Output tokens: ${block.tokenUsage.output.toLocaleString()}`}>
            <Tag style={{ fontSize: 11, fontFamily: 'monospace', cursor: 'default', marginLeft: 4 }}>
              ↑{block.tokenUsage.input.toLocaleString()} ↓{block.tokenUsage.output.toLocaleString()}
            </Tag>
          </Tooltip>
        )}
        {block.done && block.rerunData && onRerun && (
          <>
            {hasArgs && !isEditing && (
              <Tooltip title="Edit args and rerun">
                <EditOutlined
                  onClick={startEditing}
                  style={{ color: '#555', cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                  onMouseEnter={e => (e.currentTarget.style.color = '#d4d4d4')}
                  onMouseLeave={e => (e.currentTarget.style.color = '#555')}
                />
              </Tooltip>
            )}
            <Tooltip title="Rerun with same args">
              <RedoOutlined
                onClick={() => onRerun(block.rerunData!)}
                style={{ color: '#555', cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                onMouseEnter={e => (e.currentTarget.style.color = '#d4d4d4')}
                onMouseLeave={e => (e.currentTarget.style.color = '#555')}
              />
            </Tooltip>
          </>
        )}
      </div>

      {/* Body — hidden when collapsed */}
      {!collapsed && (
        <>
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

          {/* Output body */}
          <div style={{ position: 'relative' }}>
            {hasContent && (
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

            {/* run block: flat log lines with truncation */}
            {block.type === 'run' && (
              <>
                <pre style={{
                  margin: 0, padding: '10px 14px',
                  fontFamily: 'monospace', fontSize: 13,
                  color: '#d4d4d4', lineHeight: 1.6,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                }}>
                  {displayLines.map((l, i) => (
                    <span key={i} style={{ color: _lineColor(l) }}>
                      {l.line}{'\n'}
                    </span>
                  ))}
                  {!block.done && <span style={{ color: '#555' }}>▌</span>}
                </pre>

                {/* Truncation footer */}
                {shouldTruncate && (
                  <div style={{ position: 'relative' }}>
                    {/* gradient fade over the last few lines */}
                    <div style={{
                      position: 'absolute', top: -40, left: 0, right: 0, height: 40,
                      background: 'linear-gradient(transparent, #141414)',
                      pointerEvents: 'none',
                    }} />
                    <div
                      onClick={() => setShowAll(true)}
                      style={{
                        padding: '5px 14px 10px',
                        fontSize: 12, color: '#555', cursor: 'pointer',
                        fontFamily: 'monospace',
                      }}
                      onMouseEnter={e => (e.currentTarget.style.color = '#888')}
                      onMouseLeave={e => (e.currentTarget.style.color = '#555')}
                    >
                      ↓ {extraLines} more line{extraLines !== 1 ? 's' : ''} — click to expand
                    </div>
                  </div>
                )}
              </>
            )}

            {/* chat block: interleaved text segments + tool call blocks */}
            {block.type === 'chat' && (
              <div style={{ padding: '10px 14px' }}>
                {block.segments.map((seg, i) =>
                  seg.kind === 'text' ? (
                    <pre key={i} style={{
                      margin: 0, padding: 0,
                      fontFamily: 'monospace', fontSize: 13,
                      color: '#d4d4d4', lineHeight: 1.6,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                    }}>
                      {seg.text}
                    </pre>
                  ) : (
                    <ToolCallBlock key={i} entry={seg.entry} />
                  )
                )}
                {(block.currentText || !block.done) && (
                  <pre style={{
                    margin: 0, padding: 0,
                    fontFamily: 'monospace', fontSize: 13,
                    color: '#d4d4d4', lineHeight: 1.6,
                    whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  }}>
                    {block.currentText}
                    {!block.done && <span style={{ color: '#555' }}>▌</span>}
                  </pre>
                )}
              </div>
            )}
          </div>
        </>
      )}
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
        if (b.type === 'chat') {
          return { ...b, currentText: b.currentText + token }
        }
        // run block receiving tokens (unusual but handle gracefully)
        const last = b.lines[b.lines.length - 1]
        if (last) {
          return { ...b, lines: [...b.lines.slice(0, -1), { ...last, line: last.line + token }] }
        }
        return { ...b, lines: [{ id, line: token, stream: 'stdout' }] }
      }))
    }

    const onToolStart = (e: Event) => {
      const { id, tool_name, tool_input } = (e as CustomEvent).detail as {
        id: string; tool_name: string; tool_input: Record<string, unknown>
      }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        const newSegs: BlockSegment[] = [
          ...b.segments,
          ...(b.currentText ? [{ kind: 'text' as const, text: b.currentText }] : []),
          { kind: 'tool' as const, entry: { name: tool_name, input: tool_input, running: true } },
        ]
        return { ...b, segments: newSegs, currentText: '' }
      }))
    }

    const onToolEnd = (e: Event) => {
      const { id, tool_name, output } = (e as CustomEvent).detail as {
        id: string; tool_name: string; output: string
      }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        const newSegs = b.segments.map(seg => {
          if (seg.kind === 'tool' && seg.entry.name === tool_name && seg.entry.running) {
            return { ...seg, entry: { ...seg.entry, output, running: false } }
          }
          return seg
        })
        return { ...b, segments: newSegs }
      }))
    }

    const onEnd = (e: Event) => {
      const { id, exit_code, duration_ms } = (e as CustomEvent).detail as { id: string; exit_code: number; duration_ms: number }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        if (b.type === 'chat' && b.currentText) {
          return {
            ...b,
            segments: [...b.segments, { kind: 'text', text: b.currentText }],
            currentText: '',
            done: true, exitCode: exit_code, durationMs: duration_ms,
          }
        }
        return { ...b, done: true, exitCode: exit_code, durationMs: duration_ms }
      }))
    }

    const onChatUsage = (e: Event) => {
      const { id, input_tokens, output_tokens } = (e as CustomEvent).detail as {
        id: string; input_tokens: number; output_tokens: number
      }
      setBlocks(prev => prev.map(b =>
        b.id === id ? { ...b, tokenUsage: { input: input_tokens, output: output_tokens } } : b
      ))
    }

    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:tool_start', onToolStart)
    window.addEventListener('runspec:tool_end', onToolEnd)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:chat_usage', onChatUsage)
    return () => {
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_start', onToolStart)
      window.removeEventListener('runspec:tool_end', onToolEnd)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:chat_usage', onChatUsage)
    }
  }, [])

  const addBlock = (block: InvocationBlock) => setBlocks(prev => [...prev, block])
  const clearBlocks = () => setBlocks([])

  return { blocks, addBlock, clearBlocks }
}
