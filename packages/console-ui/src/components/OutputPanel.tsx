import { Fragment, useEffect, useRef, useState } from 'react'
import { Button, Input, Space, Tag, Tooltip, Typography, message } from 'antd'
import {
  ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  RedoOutlined, EditOutlined, CopyOutlined, RobotOutlined, CaretRightOutlined,
  ApiOutlined, DownOutlined, RightOutlined, VerticalAlignBottomOutlined,
  TableOutlined, CodeOutlined,
} from '@ant-design/icons'

const { Text } = Typography

const TRUNCATE_AT = 20

// ── Smart output detection ────────────────────────────────────────────────────

interface ParsedOutput {
  /** 'table' when stdout is a JSON array of uniform objects, 'object' for a JSON
   *  dict, 'raw' for everything else (plain text, mixed output, stderr-only). */
  kind: 'table' | 'object' | 'raw'
  rows?: Record<string, unknown>[]    // kind === 'table'
  obj?:  Record<string, unknown>      // kind === 'object'
  rawText: string                     // always set — used for "Send to LLM"
}

/** Try to extract a single JSON value from the stdout lines of a *completed*
 *  run block. Returns null while the block is still running. */
function parseOutput(lines: OutputLine[]): ParsedOutput {
  const stdout = lines
    .filter(l => l.stream === 'stdout')
    .map(l => l.line)
    .join('\n')
    .trim()
  const rawText = lines.map(l => l.line).join('\n')

  if (!stdout) return { kind: 'raw', rawText }

  try {
    const parsed = JSON.parse(stdout)
    if (Array.isArray(parsed) && parsed.length > 0 && typeof parsed[0] === 'object' && parsed[0] !== null) {
      return { kind: 'table', rows: parsed as Record<string, unknown>[], rawText }
    }
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return { kind: 'object', obj: parsed as Record<string, unknown>, rawText }
    }
  } catch {
    // Not JSON — fall through to raw
  }
  return { kind: 'raw', rawText }
}

/** Humanise a number value: round floats, add units for *_mb / *_kb / *_bytes keys */
function _humanVal(key: string, val: unknown): string {
  if (typeof val === 'number') {
    const k = key.toLowerCase()
    if (k.endsWith('_mb')) {
      const gb = val / 1024
      return gb >= 1 ? `${gb.toFixed(1)} GB` : `${val.toFixed(0)} MB`
    }
    if (k.endsWith('_kb')) return val >= 1024 ? `${(val / 1024).toFixed(1)} MB` : `${val} KB`
    if (k.endsWith('_bytes')) return val >= 1_073_741_824
      ? `${(val / 1_073_741_824).toFixed(1)} GB`
      : val >= 1_048_576 ? `${(val / 1_048_576).toFixed(1)} MB` : `${val} B`
    if (k.includes('pct') || k.includes('percent')) return `${val}%`
    if (k.includes('seconds') || k.endsWith('_s')) {
      if (val >= 86400) return `${(val / 86400).toFixed(1)}d`
      if (val >= 3600) return `${(val / 3600).toFixed(1)}h`
      if (val >= 60) return `${(val / 60).toFixed(1)}m`
      return `${val}s`
    }
    return Number.isInteger(val) ? String(val) : val.toFixed(2)
  }
  return String(val ?? '')
}

/** Build a plain-text table string (for clipboard). */
function tableToText(rows: Record<string, unknown>[]): string {
  if (!rows.length) return ''
  const keys = Object.keys(rows[0])
  const widths = keys.map(k =>
    Math.max(k.length, ...rows.map(r => _humanVal(k, r[k]).length))
  )
  const header = keys.map((k, i) => k.padEnd(widths[i])).join('  ')
  const sep    = widths.map(w => '-'.repeat(w)).join('  ')
  const body   = rows.map(r =>
    keys.map((k, i) => _humanVal(k, r[k]).padEnd(widths[i])).join('  ')
  )
  return [header, sep, ...body].join('\n')
}

/** Human-readable summary for a JSON object. */
function objectToText(obj: Record<string, unknown>): string {
  return Object.entries(obj)
    .map(([k, v]) => `${k}: ${_humanVal(k, v)}`)
    .join('\n')
}

// ── JSON table renderer ───────────────────────────────────────────────────────

function JsonTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return null
  const keys = Object.keys(rows[0])
  return (
    <div style={{ overflowX: 'auto', padding: '8px 14px' }}>
      <table style={{
        width: '100%', borderCollapse: 'collapse',
        fontFamily: 'monospace', fontSize: 12, color: '#d4d4d4',
      }}>
        <thead>
          <tr>
            {keys.map(k => (
              <th key={k} style={{
                textAlign: 'left', padding: '4px 10px 6px 0',
                borderBottom: '1px solid #2a2a2a',
                color: '#888', fontWeight: 600, whiteSpace: 'nowrap',
              }}>
                {k.replace(/_/g, ' ')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)' }}>
              {keys.map(k => (
                <td key={k} style={{
                  padding: '4px 10px 4px 0',
                  borderBottom: '1px solid #1a1a1a',
                  whiteSpace: 'nowrap',
                }}>
                  {_humanVal(k, row[k])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Key-value view for JSON objects. */
function JsonObject({ obj }: { obj: Record<string, unknown> }) {
  return (
    <div style={{ padding: '8px 14px', display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '4px 16px', fontFamily: 'monospace', fontSize: 12 }}>
      {Object.entries(obj).map(([k, v]) => [
        <span key={`k-${k}`} style={{ color: '#888', whiteSpace: 'nowrap' }}>{k.replace(/_/g, ' ')}</span>,
        <span key={`v-${k}`} style={{ color: '#d4d4d4' }}>{_humanVal(k, v)}</span>,
      ])}
    </div>
  )
}

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
  const [viewRaw, setViewRaw] = useState(false)

  const args = block.rerunData?.args ?? {}
  const hasArgs = Object.keys(args).length > 0

  // Smart output parsing — only for completed run blocks
  const parsed = (block.type === 'run' && block.done) ? parseOutput(block.lines) : null

  // Truncation — only for completed run blocks with many lines (raw view only)
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
    : (parsed?.rawText ?? block.lines.map(l => l.line).join('\n'))

  const handleCopy = () => {
    let text: string
    if (!viewRaw && parsed?.kind === 'table' && parsed.rows) {
      text = tableToText(parsed.rows)
    } else if (!viewRaw && parsed?.kind === 'object' && parsed.obj) {
      text = objectToText(parsed.obj)
    } else {
      text = `Output from ${block.label}\n\n${logText}`
    }

    // execCommand fallback for WebView2 / file:// contexts where clipboard API may be absent
    const copyViaExec = (t: string) => {
      const el = document.createElement('textarea')
      el.value = t
      el.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0'
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
    }

    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text)
        .then(() => message.success('Copied to clipboard'))
        .catch(() => { copyViaExec(text); message.success('Copied to clipboard') })
    } else {
      copyViaExec(text)
      message.success('Copied to clipboard')
    }
  }

  const handleAskLlm = () => {
    // Always send raw JSON to LLM — structured data is more useful than formatted table
    const raw = parsed ? parsed.rawText : logText
    onAskLlm?.(`Output from ${block.label}\n\n${raw}`)
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
          <Fragment>
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
          </Fragment>
        )}
      </div>

      {/* Body — hidden when collapsed */}
      {!collapsed && (
        <Fragment>
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

            {/* run block: smart JSON table/object or flat log lines */}
            {block.type === 'run' && (
              <Fragment>
                {/* View toggle — only shown when structured JSON was detected */}
                {parsed && parsed.kind !== 'raw' && block.done && (
                  <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '4px 58px 0 14px', gap: 2 }}>
                    <Tooltip title="Table view">
                      <Button
                        type="text" size="small" icon={<TableOutlined />}
                        onClick={() => setViewRaw(false)}
                        style={{ color: !viewRaw ? '#1677ff' : '#555', height: 22, padding: '0 5px' }}
                      />
                    </Tooltip>
                    <Tooltip title="Raw output">
                      <Button
                        type="text" size="small" icon={<CodeOutlined />}
                        onClick={() => setViewRaw(true)}
                        style={{ color: viewRaw ? '#1677ff' : '#555', height: 22, padding: '0 5px' }}
                      />
                    </Tooltip>
                  </div>
                )}

                {/* Structured table view */}
                {!viewRaw && parsed?.kind === 'table' && parsed.rows && (
                  <JsonTable rows={parsed.rows} />
                )}

                {/* Structured key-value view */}
                {!viewRaw && parsed?.kind === 'object' && parsed.obj && (
                  <JsonObject obj={parsed.obj} />
                )}

                {/* Raw / plain-text view — shown when: still running, raw output, or user toggled raw */}
                {(viewRaw || !parsed || parsed.kind === 'raw') && (
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
                )}

                {/* Truncation footer — raw view only */}
                {shouldTruncate && (viewRaw || !parsed || parsed.kind === 'raw') && (
                  <div style={{ position: 'relative' }}>
                    {/* gradient fade */}
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
              </Fragment>
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
        </Fragment>
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
