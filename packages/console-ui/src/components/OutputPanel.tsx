import { Fragment, useEffect, useRef, useState } from 'react'
import { Button, Input, Space, Tag, Tooltip, Typography, message } from 'antd'
import { bridge } from '../bridge'
import { Markdown } from './Markdown'
import { useIsDark, textColor } from '../ThemeContext'
import {
  ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  RedoOutlined, EditOutlined, CopyOutlined, RobotOutlined, CaretRightOutlined,
  ApiOutlined, DownOutlined, RightOutlined, VerticalAlignBottomOutlined,
  TableOutlined, CodeOutlined, PauseCircleOutlined, StopOutlined,
} from '@ant-design/icons'

const { Text } = Typography

const TRUNCATE_AT = 20

// Theme-aware palette for the output cards (header, body, tool blocks, tables).
// The cards were hard-coded dark; this lets console + trigger cards follow the
// light/dark toggle consistently. Returned by useCardTheme() per component.
interface CardTheme {
  isDark: boolean
  cardBg: string; cardBorder: string; headerBg: string
  text: string; dim: string; faint: string; caret: string; mono: string
  rowAlt: string; tableHeadBorder: string; tableRowBorder: string; tableHead: string
  codeBg: string; toolBg: string; toolBorder: string
  link: string; sub: string
}
function useCardTheme(): CardTheme {
  const isDark = useIsDark()
  return {
    isDark,
    cardBg: isDark ? '#141414' : '#ffffff',
    cardBorder: isDark ? '#2a2a2a' : '#e3e3e3',
    headerBg: isDark ? '#1a1a1a' : '#f5f6f8',
    text: textColor(isDark),
    dim: isDark ? '#777' : '#8c8c8c',
    faint: isDark ? '#555' : '#bbbbbb',
    caret: isDark ? '#444' : '#bbbbbb',
    mono: isDark ? '#d4d4d4' : '#1f1f1f',
    rowAlt: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.025)',
    tableHeadBorder: isDark ? '#2a2a2a' : '#e3e3e3',
    tableRowBorder: isDark ? '#1a1a1a' : '#f0f0f0',
    tableHead: isDark ? '#888' : '#888',
    codeBg: isDark ? '#141414' : '#f5f5f5',
    toolBg: isDark ? '#1a1a1a' : '#f5f6f8',
    toolBorder: isDark ? '#2a2a2a' : '#e3e3e3',
    link: isDark ? '#4fc1ff' : '#0958d9',
    sub: isDark ? '#9ab' : '#5a6b7b',
  }
}

/** Short elapsed string (e.g. "8s", "1m 04s") since an ISO timestamp. */
function fmtElapsed(startedAt: string): string {
  const s = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

/** Compact duration for a tool call (ms → "120ms" / "1.2s" / "1m 5s"). */
function fmtDurationMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

// Approximate Anthropic list prices (USD per million tokens), matched by a
// substring of the model id. Cache reads bill ~0.1× input, cache writes ~1.25×.
// Rates drift — this is a labelled *estimate*; unknown models show no cost.
const PRICING: Array<{ match: RegExp; in: number; out: number }> = [
  { match: /opus/i, in: 15, out: 75 },
  { match: /sonnet/i, in: 3, out: 15 },
  { match: /haiku/i, in: 1, out: 5 },
]

/** Estimated USD cost for a turn, or null when the model isn't in the table. */
function estimateCostUsd(
  model: string | undefined,
  u: { input: number; output: number; cacheRead: number; cacheCreation: number },
): number | null {
  if (!model) return null
  const p = PRICING.find(x => x.match.test(model))
  if (!p) return null
  const m = 1_000_000
  return (
    (u.input * p.in + u.cacheRead * p.in * 0.1 + u.cacheCreation * p.in * 1.25 + u.output * p.out) / m
  )
}

/** Format a small USD amount (e.g. "$0.0021", "$1.34"). */
function fmtUsd(v: number): string {
  if (v < 0.01) return `$${v.toFixed(4)}`
  return `$${v.toFixed(2)}`
}

/** Copy text to the clipboard with an execCommand fallback for WebView2 / file://
 *  contexts where the async clipboard API may be absent. */
function copyToClipboard(text: string): void {
  const viaExec = () => {
    const el = document.createElement('textarea')
    el.value = text
    el.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0'
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => message.success('Copied to clipboard'))
      .catch(() => { viaExec(); message.success('Copied to clipboard') })
  } else {
    viaExec()
    message.success('Copied to clipboard')
  }
}

// ── Smart output detection ────────────────────────────────────────────────────

interface TableSection {
  title?: string
  rows: Record<string, unknown>[]
}

interface ParsedOutput {
  /** 'table' for a JSON array of objects, 'object' for a plain dict, 'sections'
   *  for a dict that wraps one or more arrays-of-objects (grouped tables, or
   *  metadata + rows), 'raw' for everything else (plain text, stderr-only). */
  kind: 'table' | 'object' | 'sections' | 'raw'
  rows?: Record<string, unknown>[]    // kind === 'table'
  obj?:  Record<string, unknown>      // kind === 'object'
  sections?: TableSection[]           // kind === 'sections'
  meta?: Record<string, unknown>      // kind === 'sections' — scalar caption fields
  rawText: string                     // always set — used for "Send to LLM"
}

const isRowArray = (v: unknown): boolean =>
  Array.isArray(v) && (v.length === 0 || (typeof v[0] === 'object' && v[0] !== null && !Array.isArray(v[0])))

/** Try to extract a single JSON value from the stdout lines of a *completed*
 *  run block. Returns null while the block is still running. */
export function parseOutput(lines: OutputLine[]): ParsedOutput {
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
      const entries = Object.entries(parsed as Record<string, unknown>)
      const tableEntries = entries.filter(([, v]) => isRowArray(v))
      const scalarEntries = entries.filter(([, v]) => v === null || typeof v !== 'object')
      // A dict that wraps array(s)-of-objects (+ optional scalar metadata) —
      // e.g. {System:[…], Application:[…]} or {log, level, events:[…]} — renders
      // as titled section tables instead of an [object Object] grid.
      if (tableEntries.length > 0 && tableEntries.length + scalarEntries.length === entries.length) {
        return {
          kind: 'sections',
          sections: tableEntries.map(([title, v]) => ({ title, rows: v as Record<string, unknown>[] })),
          meta: Object.fromEntries(scalarEntries),
          rawText,
        }
      }
      return { kind: 'object', obj: parsed as Record<string, unknown>, rawText }
    }
  } catch {
    // Not JSON — fall through to raw
  }
  return { kind: 'raw', rawText }
}

/** Compact, length-capped JSON — the safety net so nested values never render
 *  as "[object Object]". */
function _compactJson(val: unknown): string {
  const s = JSON.stringify(val)
  return s.length > 120 ? s.slice(0, 117) + '…' : s
}

/** Humanise a value for text/clipboard: round floats, add units for *_mb /
 *  *_kb / *_bytes keys, join scalar arrays, and fall back to compact JSON for
 *  nested objects/arrays (never "[object Object]"). */
export function _humanVal(key: string, val: unknown): string {
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
  if (val === null || val === undefined) return ''
  if (typeof val === 'boolean') return val ? 'true' : 'false'
  if (Array.isArray(val)) {
    // array of scalars → comma list; array of objects → compact JSON
    return val.length > 0 && typeof val[0] === 'object' && val[0] !== null
      ? _compactJson(val)
      : val.map(v => _humanVal(key, v)).join(', ')
  }
  if (typeof val === 'object') return _compactJson(val)
  return String(val)
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

/** Plain-text rendering for grouped/section tables (clipboard). */
function sectionsToText(meta: Record<string, unknown> | undefined, sections: TableSection[]): string {
  const parts: string[] = []
  const metaEntries = meta ? Object.entries(meta) : []
  if (metaEntries.length) parts.push(metaEntries.map(([k, v]) => `${k}: ${_humanVal(k, v)}`).join('  '))
  for (const s of sections) {
    const body = s.rows.length ? tableToText(s.rows) : '(no rows)'
    parts.push(s.title ? `\n${s.title}\n${body}` : body)
  }
  return parts.join('\n').trim()
}

// ── JSON table renderer ───────────────────────────────────────────────────────

/** Render one value: primitives and scalar arrays inline via `_humanVal`; a
 *  nested object or array-of-objects as an expandable disclosure (sub-table or
 *  pretty JSON) so it never collapses to "[object Object]". */
function JsonCell({ keyName, value }: { keyName: string; value: unknown }) {
  const c = useCardTheme()
  const [open, setOpen] = useState(false)
  const isObj = typeof value === 'object' && value !== null
  const isScalarArray = Array.isArray(value)
    && (value.length === 0 || value.every(x => x === null || typeof x !== 'object'))
  // primitives + arrays of scalars render inline as text
  if (!isObj || isScalarArray) return <>{_humanVal(keyName, value)}</>

  const rowArray = isRowArray(value)
  const label = Array.isArray(value) ? `[${(value as unknown[]).length}]` : '{…}'
  return (
    <span>
      <a onClick={() => setOpen(o => !o)} style={{ color: c.link, cursor: 'pointer', whiteSpace: 'nowrap' }}>
        {open ? '▾' : '▸'} {label}
      </a>
      {open && (rowArray
        ? <JsonTable rows={value as Record<string, unknown>[]} />
        : <pre style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-all', color: c.sub, fontSize: 11 }}>
            {JSON.stringify(value, null, 2)}
          </pre>
      )}
    </span>
  )
}

function JsonTable({ rows }: { rows: Record<string, unknown>[] }) {
  const c = useCardTheme()
  if (!rows.length) return null
  const keys = Object.keys(rows[0])
  return (
    <div style={{ overflowX: 'auto', padding: '8px 14px' }}>
      <table style={{
        width: '100%', borderCollapse: 'collapse',
        fontFamily: 'monospace', fontSize: 12, color: c.mono,
      }}>
        <thead>
          <tr>
            {keys.map(k => (
              <th key={k} style={{
                textAlign: 'left', padding: '4px 10px 6px 0',
                borderBottom: `1px solid ${c.tableHeadBorder}`,
                color: c.tableHead, fontWeight: 600, whiteSpace: 'nowrap',
              }}>
                {k.replace(/_/g, ' ')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ background: i % 2 === 0 ? 'transparent' : c.rowAlt }}>
              {keys.map(k => (
                <td key={k} style={{
                  padding: '4px 10px 4px 0',
                  borderBottom: `1px solid ${c.tableRowBorder}`,
                  whiteSpace: 'nowrap',
                }}>
                  <JsonCell keyName={k} value={row[k]} />
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
  const c = useCardTheme()
  return (
    <div style={{ padding: '8px 14px', display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '4px 16px', fontFamily: 'monospace', fontSize: 12 }}>
      {Object.entries(obj).map(([k, v]) => [
        <span key={`k-${k}`} style={{ color: c.tableHead, whiteSpace: 'nowrap' }}>{k.replace(/_/g, ' ')}</span>,
        <span key={`v-${k}`} style={{ color: c.mono }}><JsonCell keyName={k} value={v} /></span>,
      ])}
    </div>
  )
}

/** Render a dict that wraps array(s)-of-objects: optional scalar caption, then
 *  one titled table per array (grouped tables / metadata + rows). */
function JsonSections({ meta, sections }: { meta?: Record<string, unknown>; sections: TableSection[] }) {
  const c = useCardTheme()
  const metaEntries = meta ? Object.entries(meta) : []
  return (
    <div style={{ padding: '8px 14px' }}>
      {metaEntries.length > 0 && (
        <div style={{ fontFamily: 'monospace', fontSize: 12, color: c.dim, marginBottom: 8 }}>
          {metaEntries.map(([k, v]) => `${k.replace(/_/g, ' ')}: ${_humanVal(k, v)}`).join('   ')}
        </div>
      )}
      {sections.map((s, i) => (
        <div key={i} style={{ marginBottom: i < sections.length - 1 ? 14 : 0 }}>
          {s.title && (
            <div style={{ fontFamily: 'monospace', fontSize: 12, color: c.mono, fontWeight: 600 }}>
              {s.title.replace(/_/g, ' ')}
            </div>
          )}
          {s.rows.length > 0
            ? <JsonTable rows={s.rows} />
            : <div style={{ fontFamily: 'monospace', fontSize: 12, color: c.dim, padding: '4px 14px' }}>(no rows)</div>}
        </div>
      ))}
    </div>
  )
}

function _lineColor(l: OutputLine, mono: string): string {
  if (l.stream !== 'stderr') return mono
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
  commandPath?: string[]
}

export interface ToolCallEntry {
  name: string
  input: Record<string, unknown>
  output?: string
  running: boolean
  startedAt?: number   // epoch ms when the tool call began (for per-tool duration)
  durationMs?: number  // set when the tool call finishes
  progress?: string    // latest live progress line (built-in tools: repo clone/grep, denylist fan-out)
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
  stopped?: boolean   // chat turn was halted by the user (distinct from error)
  exitCode?: number
  durationMs?: number
  rerunData?: RerunData
  model?: string      // model the chat turn ran on (for cost estimate)
  tokenUsage?: { input: number; output: number; cacheRead: number; cacheCreation: number }
}

interface OutputPanelProps {
  blocks: InvocationBlock[]
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
}

interface PendingConfirm {
  request_id: string
  id: string          // chat block this confirm belongs to
  tool_name: string   // full host__tool name, matches the tool segment's entry.name
  tool_input: Record<string, unknown>
  // Human-readable view of what's being approved. For the search-mode
  // `run_runnable` wrapper these unwrap to the real runnable + args; for a plain
  // tool call they equal tool_name/tool_input. Optional for back-compat.
  display_name?: string
  display_input?: Record<string, unknown>
  autonomy: string
}

export function OutputPanel({ blocks, onRerun, onAskLlm }: OutputPanelProps) {
  const c = useCardTheme()
  const scrollRef = useRef<HTMLDivElement>(null)
  const isAtBottomRef = useRef(true)
  const blockCountRef = useRef(0)
  const [showScrollDown, setShowScrollDown] = useState(false)

  // Agent autonomy gate: the bridge dispatches runspec:tool_confirm and blocks
  // until we answer via resolve_tool_confirmation. Rendered as a normal element
  // in this (always-mounted, already-rendering) component — not an antd static
  // Modal, which does not render reliably inside the WebView2 host.
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null)

  useEffect(() => {
    const onToolConfirm = (e: Event) => {
      const { id, request_id, tool_name, tool_input, display_name, display_input, autonomy } = (e as CustomEvent).detail as {
        id: string; request_id: string; tool_name: string; tool_input: Record<string, unknown>
        display_name?: string; display_input?: Record<string, unknown>; autonomy: string
      }
      console.log('[runspec] tool_confirm received', { id, request_id, tool_name, autonomy })
      setPendingConfirm({ request_id, id, tool_name, tool_input: tool_input ?? {}, display_name, display_input, autonomy })
    }
    window.addEventListener('runspec:tool_confirm', onToolConfirm)
    return () => window.removeEventListener('runspec:tool_confirm', onToolConfirm)
  }, [])

  // If the turn ends while its confirm prompt is still open — e.g. the user hit
  // Stop instead of answering — drop the now-stale prompt so it doesn't linger.
  useEffect(() => {
    const onEnd = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPendingConfirm(prev => (prev && prev.id === id ? null : prev))
    }
    window.addEventListener('runspec:run_end', onEnd)
    return () => window.removeEventListener('runspec:run_end', onEnd)
  }, [])

  const answerConfirm = (approved: boolean, editedInput?: Record<string, unknown>) => {
    setPendingConfirm(prev => {
      if (prev) {
        console.log('[runspec] resolving tool_confirm', { request_id: prev.request_id, approved, edited: !!editedInput })
        bridge.resolve_tool_confirmation(prev.request_id, approved, editedInput)
          .catch(err => console.error('[runspec] resolve_tool_confirmation failed', err))
      }
      return null
    })
  }

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
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: c.dim }}>
        <div style={{ textAlign: 'center' }}>
          <ThunderboltOutlined style={{ fontSize: 32, marginBottom: 12, display: 'block' }} />
          <Text type="secondary">Type <Text code>/</Text> to run a runnable, or ask the assistant anything</Text>
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
            <BlockCard
              key={block.id}
              block={block}
              onRerun={onRerun}
              onAskLlm={onAskLlm}
              pendingConfirm={pendingConfirm && pendingConfirm.id === block.id ? pendingConfirm : null}
              onAnswerConfirm={answerConfirm}
            />
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
              background: c.headerBg, borderColor: c.cardBorder, color: c.dim,
              fontSize: 11, height: 26, padding: '0 10px',
              boxShadow: c.isDark ? '0 2px 8px rgba(0,0,0,0.5)' : '0 2px 8px rgba(0,0,0,0.15)',
            }}
          >
            latest
          </Button>
        </div>
      )}
    </div>
  )
}

// Edit-buffer encoding for editable confirmations: a plain string stays bare
// (so a path/text isn't shown wrapped in quotes), everything else is JSON.
function jsonValue(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v)
}

// Parse an edited field back to a value: try JSON (so 42/true/[...] round-trip),
// falling back to the raw string (the common case — a path, a name, free text).
function parseEdited(s: string): unknown {
  const t = s.trim()
  if (t === '') return s
  try {
    return JSON.parse(t)
  } catch {
    return s
  }
}

function ToolCallBlock({
  entry,
  awaiting,
  resolved,
  editable,
  onAnswer,
}: {
  entry: ToolCallEntry
  // non-null = this call is parked at the autonomy gate; value is the level
  awaiting?: string | null
  // When parked at the gate, the human-readable view of what's being approved —
  // for search-mode `run_runnable` this is the unwrapped runnable + its real
  // args, so the approval panel shows that instead of the generic envelope.
  resolved?: { name: string; input: Record<string, unknown> } | null
  // The operator may edit the args before approving (editable confirmations).
  // Only when the displayed call equals the real call (a plain/direct tool, not
  // the search-mode `run_runnable` wrapper whose display args would re-wrap lossily).
  editable?: boolean
  onAnswer?: (approved: boolean, editedInput?: Record<string, unknown>) => void
}) {
  const c = useCardTheme()
  const [expanded, setExpanded] = useState(false)
  const args = Object.entries(entry.input)
  // Args shown in the approval panel: the resolved runnable's when we have it.
  const approveArgs = resolved ? Object.entries(resolved.input) : args
  // Edit buffer: arg name → its JSON-encoded string, seeded from approveArgs and
  // reset whenever a new gate parks here. null until the first parked render.
  const [edits, setEdits] = useState<Record<string, string> | null>(null)
  const seedKey = awaiting ? `${entry.name}:${approveArgs.map(([k]) => k).join(',')}` : ''
  useEffect(() => {
    if (awaiting && editable) {
      setEdits(Object.fromEntries(approveArgs.map(([k, v]) => [k, jsonValue(v)])))
    } else {
      setEdits(null)
    }
    // Re-seed when a new confirm parks here (different tool/args); not on keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey, awaiting, editable])

  const approve = () => {
    if (editable && edits) {
      const out: Record<string, unknown> = {}
      for (const [k, s] of Object.entries(edits)) out[k] = parseEdited(s)
      onAnswer?.(true, out)
    } else {
      onAnswer?.(true)
    }
  }

  return (
    <div style={{
      margin: '6px 0',
      border: `1px solid ${awaiting ? (c.isDark ? '#5a4a1a' : '#ffd591') : c.toolBorder}`,
      borderRadius: 6, overflow: 'hidden',
    }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px',
          background: awaiting ? (c.isDark ? '#241f12' : '#fff7e6') : c.toolBg,
          cursor: entry.output !== undefined ? 'pointer' : 'default',
          userSelect: 'none',
        }}
        onClick={() => entry.output !== undefined && setExpanded(x => !x)}
      >
        {awaiting
          ? <PauseCircleOutlined style={{ color: '#e6a23c', fontSize: 12 }} />
          : entry.running
            ? <LoadingOutlined style={{ color: '#1677ff', fontSize: 12 }} />
            : <ApiOutlined style={{ color: '#52c41a', fontSize: 12 }} />}
        <Text code style={{ fontSize: 12, flexShrink: 0 }}>{entry.name.replace(/^[^_]+__/, '')}</Text>
        {args.length > 0 && (
          <Text ellipsis style={{ flex: 1, minWidth: 0, color: c.dim, fontSize: 11 }}>
            {args.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}
          </Text>
        )}
        {awaiting && (
          <Text style={{ color: '#e6a23c', fontSize: 11, marginLeft: 'auto', flexShrink: 0 }}>needs approval</Text>
        )}
        {!awaiting && entry.running && (
          <Text ellipsis style={{ color: c.dim, fontSize: 11, marginLeft: 'auto', flexShrink: 0, maxWidth: '50%', fontFamily: entry.progress ? 'monospace' : undefined }}>
            {entry.progress || 'running…'}
          </Text>
        )}
        {!entry.running && entry.durationMs !== undefined && (
          <Tooltip title="Time this tool call took">
            <Text style={{ color: c.dim, fontSize: 11, marginLeft: 'auto', flexShrink: 0, fontFamily: 'monospace' }}>
              {fmtDurationMs(entry.durationMs)}
            </Text>
          </Tooltip>
        )}
        {entry.output !== undefined && !entry.running && (
          <Text style={{ color: c.dim, fontSize: 11, marginLeft: 'auto', flexShrink: 0 }}>
            {expanded ? '▲ hide output' : '▼ show output'}
          </Text>
        )}
      </div>

      {awaiting && (
        <div style={{ padding: '10px 12px', background: c.isDark ? '#1c1810' : '#fffbe6', borderTop: `1px solid ${c.isDark ? '#5a4a1a' : '#ffe7ba'}` }}>
          <div style={{ fontSize: 12, color: c.isDark ? '#bbb' : '#595959', marginBottom: 10 }}>
            The assistant wants to run{' '}
            {resolved && <Text code style={{ fontSize: 11 }}>{resolved.name.replace(/^[^_]+__/, '')}</Text>}
            {resolved ? ' ' : 'this '}— autonomy is{' '}
            <strong style={{ color: '#e6a23c' }}>{awaiting}</strong>, so it needs your go-ahead.
          </div>
          {approveArgs.length > 0 && editable && edits ? (
            // Editable confirmations: let the operator tweak each arg before
            // approving. One field per arg (JSON-encoded so a number/bool/string
            // round-trips); the edited values run instead of the agent's proposal.
            <div style={{ margin: '0 0 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {approveArgs.map(([k]) => (
                <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Text code style={{ fontSize: 11, flexShrink: 0, minWidth: 90 }}>{k}</Text>
                  <Input
                    size="small"
                    value={edits[k] ?? ''}
                    onChange={e => setEdits(prev => ({ ...(prev ?? {}), [k]: e.target.value }))}
                    style={{ fontFamily: 'monospace', fontSize: 11 }}
                  />
                </div>
              ))}
            </div>
          ) : approveArgs.length > 0 ? (
            // The header preview ellipsises; show the full args here, wrapped, so
            // the operator can see exactly what they're approving — a many-arg
            // runnable used to push this (and the buttons) off-screen.
            <pre style={{
              margin: '0 0 10px', padding: '8px 10px',
              background: c.codeBg, borderRadius: 4,
              fontFamily: 'monospace', fontSize: 11, lineHeight: 1.5, color: c.sub,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              maxHeight: 180, overflowY: 'auto',
            }}>
              {approveArgs.map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join('\n')}
            </pre>
          ) : null}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button type="primary" size="small" icon={<CaretRightOutlined />} onClick={approve}>
              Approve &amp; run
            </Button>
            <Button size="small" danger onClick={() => onAnswer?.(false)}>Deny</Button>
          </div>
        </div>
      )}

      {expanded && entry.output !== undefined && (
        <pre style={{
          margin: 0, padding: '8px 12px',
          fontFamily: 'monospace', fontSize: 12,
          color: c.mono, lineHeight: 1.5,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          background: c.codeBg,
        }}>
          {entry.output || '(no output)'}
        </pre>
      )}
    </div>
  )
}

// Shared chat-transcript renderer: interleaved markdown text + tool-call blocks +
// the live streaming tail. Used by the console BlockCard *and* the Triggers panel
// (RunInbox) so an agent interaction looks identical in both. `pendingConfirm` is
// optional — the console passes it to show the inline approve gate; the Triggers
// panel omits it (it has its own Approve/Deny section) so tool blocks render plain.
export function ChatTranscript({
  segments,
  currentText,
  done,
  pendingConfirm,
  onAnswerConfirm,
}: {
  segments: BlockSegment[]
  currentText: string
  done: boolean
  pendingConfirm?: PendingConfirm | null
  onAnswerConfirm?: (approved: boolean, editedInput?: Record<string, unknown>) => void
}) {
  const c = useCardTheme()
  return (
    <div style={{ padding: '10px 14px' }}>
      {segments.map((seg, i) =>
        seg.kind === 'text' ? (
          <Markdown key={i} color={c.text}>{seg.text}</Markdown>
        ) : (
          <ToolCallBlock
            key={i}
            entry={seg.entry}
            awaiting={
              pendingConfirm && pendingConfirm.tool_name === seg.entry.name && seg.entry.running
                ? pendingConfirm.autonomy
                : null
            }
            resolved={
              pendingConfirm && pendingConfirm.tool_name === seg.entry.name && seg.entry.running && pendingConfirm.display_name
                ? { name: pendingConfirm.display_name, input: pendingConfirm.display_input ?? {} }
                : null
            }
            editable={
              !!pendingConfirm && pendingConfirm.tool_name === seg.entry.name && seg.entry.running
                && (pendingConfirm.display_name ?? pendingConfirm.tool_name) === pendingConfirm.tool_name
            }
            onAnswer={onAnswerConfirm}
          />
        )
      )}
      {(currentText || !done) && (
        <pre style={{
          margin: 0, padding: 0,
          fontFamily: 'monospace', fontSize: 13,
          color: c.mono, lineHeight: 1.6,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>
          {currentText}
          {!done && <span style={{ color: c.faint }}>▌</span>}
        </pre>
      )}
    </div>
  )
}

// Shared structured-output renderer: parse a result string and render the rich
// JSON table / object / section views the run cards use. Returns null when the
// text isn't structured JSON, so callers can fall back to markdown/plain text.
export function StructuredOutput({ text }: { text: string }) {
  const parsed = parseOutput(text.split('\n').map(line => ({ id: '', line, stream: 'stdout' as const })))
  if (parsed.kind === 'table' && parsed.rows) return <JsonTable rows={parsed.rows} />
  if (parsed.kind === 'sections' && parsed.sections) return <JsonSections meta={parsed.meta} sections={parsed.sections} />
  if (parsed.kind === 'object' && parsed.obj) return <JsonObject obj={parsed.obj} />
  return null
}

function BlockCard({
  block,
  onRerun,
  onAskLlm,
  pendingConfirm,
  onAnswerConfirm,
}: {
  block: InvocationBlock
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
  pendingConfirm?: PendingConfirm | null
  onAnswerConfirm?: (approved: boolean) => void
}) {
  const c = useCardTheme()
  const [isEditing, setIsEditing] = useState(false)
  const [editArgs, setEditArgs] = useState<Record<string, string>>({})
  const [collapsed, setCollapsed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [viewRaw, setViewRaw] = useState(false)

  // Tick once a second while this block is still running, so the header's live
  // elapsed timer updates (chat turns aren't in the InFlightStrip, so without
  // this there's no running clock for an agent turn).
  const [, forceTick] = useState(0)
  useEffect(() => {
    if (block.done) return
    const t = setInterval(() => forceTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [block.done])

  const toolCount = block.type === 'chat' ? block.segments.filter(s => s.kind === 'tool').length : 0
  // Edit & resend / resend act on a user chat prompt (the block label).
  const loadToComposer = () => window.dispatchEvent(new CustomEvent('runspec:load_composer', { detail: { text: block.label } }))
  const resend = () => window.dispatchEvent(new CustomEvent('runspec:send_chat', { detail: { message: block.label } }))

  // Collapse-all / expand-all from the transcript header broadcasts to every card.
  useEffect(() => {
    const onCollapseAll = (e: Event) => setCollapsed(!!(e as CustomEvent).detail?.collapsed)
    window.addEventListener('runspec:collapse_all', onCollapseAll)
    return () => window.removeEventListener('runspec:collapse_all', onCollapseAll)
  }, [])

  const args = block.rerunData?.args ?? {}
  const hasArgs = Object.keys(args).length > 0

  // Smart output parsing — only for completed run blocks
  const parsed = (block.type === 'run' && block.done) ? parseOutput(block.lines) : null

  // Truncation — only for completed run blocks with many lines (raw view only)
  const extraLines = block.lines.length - TRUNCATE_AT
  const shouldTruncate = block.type === 'run' && block.done && !showAll && extraLines > 0
  const displayLines = shouldTruncate ? block.lines.slice(0, TRUNCATE_AT) : block.lines

  const statusIcon = !block.done
    ? <LoadingOutlined style={{ color: '#1677ff' }} />
    : block.stopped
      ? <PauseCircleOutlined style={{ color: '#8c8c8c' }} />
      : block.exitCode === 0
        ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
        : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />

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
    } else if (!viewRaw && parsed?.kind === 'sections' && parsed.sections) {
      text = sectionsToText(parsed.meta, parsed.sections)
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
    <div style={{ background: c.cardBg, border: `1px solid ${c.cardBorder}`, borderRadius: 8, overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 14px',
        background: c.headerBg,
        borderBottom: collapsed ? 'none' : `1px solid ${c.cardBorder}`,
        userSelect: 'none',
      }}>
        {/* Collapse toggle */}
        <span
          onClick={() => setCollapsed(v => !v)}
          style={{ color: c.caret, cursor: 'pointer', fontSize: 11, flexShrink: 0, width: 14 }}
        >
          {collapsed
            ? <RightOutlined style={{ fontSize: 10 }} />
            : <DownOutlined style={{ fontSize: 10 }} />}
        </span>

        {statusIcon}
        <Text
          style={{ color: c.text, fontFamily: 'monospace', fontSize: 13, cursor: 'pointer', flex: 1 }}
          onClick={() => setCollapsed(v => !v)}
        >
          {block.label}
        </Text>
        <Text style={{ color: c.dim, fontSize: 11 }}>
          {new Date(block.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </Text>
        {/* Live elapsed clock while the turn runs (chat turns have no InFlightStrip row). */}
        {!block.done && (
          <Text style={{ color: '#1677ff', fontSize: 11, fontFamily: 'monospace' }}>{fmtElapsed(block.startedAt)}</Text>
        )}
        {toolCount > 0 && (
          <Tooltip title={`${toolCount} tool call${toolCount !== 1 ? 's' : ''} this turn`}>
            <Tag style={{ fontSize: 11, marginLeft: 4 }}><ApiOutlined style={{ fontSize: 10 }} /> {toolCount}</Tag>
          </Tooltip>
        )}
        {block.type === 'chat' && !block.done && (
          <Tooltip title="Stop — halt the assistant; no further steps or tool runs">
            <Button
              size="small" danger icon={<StopOutlined />}
              style={{ marginLeft: 4 }}
              onClick={(e) => { e.stopPropagation(); bridge.cancel_chat(block.id) }}
            >
              Stop
            </Button>
          </Tooltip>
        )}
        {block.done && block.durationMs !== undefined && (
          <Tag
            style={{ marginLeft: 4, fontSize: 11 }}
            color={block.stopped ? 'default' : block.exitCode === 0 ? 'green' : 'red'}
          >
            {block.stopped ? 'stopped' : block.exitCode === 0 ? 'ok' : 'error'} · {(block.durationMs / 1000).toFixed(2)}s
          </Tag>
        )}
        {block.tokenUsage && (() => {
          const { input, output, cacheRead, cacheCreation } = block.tokenUsage!
          const promptTotal = input + cacheRead + cacheCreation
          const cached = cacheRead + cacheCreation
          const hitPct = promptTotal > 0 ? Math.round((cacheRead / promptTotal) * 100) : 0
          const cost = estimateCostUsd(block.model, block.tokenUsage!)
          return (
            <>
              <Tooltip title={
                `Prompt ${promptTotal.toLocaleString()} tokens = ` +
                `${input.toLocaleString()} new + ${cacheRead.toLocaleString()} cache read (~0.1×) + ` +
                `${cacheCreation.toLocaleString()} cache write (~1.25×) · ` +
                `Output ${output.toLocaleString()}`
              }>
                <Tag style={{ fontSize: 11, fontFamily: 'monospace', cursor: 'default', marginLeft: 4 }}>
                  ↑{promptTotal.toLocaleString()} ↓{output.toLocaleString()}
                </Tag>
              </Tooltip>
              {cached > 0 && (
                <Tooltip title={
                  `${cacheRead.toLocaleString()} tokens served from cache at ~10% cost` +
                  (cacheCreation > 0 ? ` · ${cacheCreation.toLocaleString()} written to cache this turn` : '')
                }>
                  <Tag color="green" style={{ fontSize: 11, fontFamily: 'monospace', cursor: 'default', marginLeft: 4 }}>
                    ⚡{hitPct}% cached
                  </Tag>
                </Tooltip>
              )}
              {cost !== null && (
                <Tooltip title={`Estimated cost at approximate list prices${block.model ? ` · ${block.model}` : ''}. Rates drift — treat as a guide.`}>
                  <Tag style={{ fontSize: 11, fontFamily: 'monospace', cursor: 'default', marginLeft: 4 }}>
                    ~{fmtUsd(cost)}
                  </Tag>
                </Tooltip>
              )}
            </>
          )
        })()}
        {block.done && block.rerunData && onRerun && (
          <Fragment>
            {hasArgs && !isEditing && (
              <Tooltip title="Edit args and rerun">
                <EditOutlined
                  onClick={startEditing}
                  style={{ color: c.dim, cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                  onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                  onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
                />
              </Tooltip>
            )}
            <Tooltip title="Rerun with same args">
              <RedoOutlined
                onClick={() => onRerun(block.rerunData!)}
                style={{ color: c.dim, cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
              />
            </Tooltip>
          </Fragment>
        )}
        {/* Chat turns: copy / edit / resend the original prompt. */}
        {block.type === 'chat' && block.done && block.label && (
          <Fragment>
            <Tooltip title="Copy this prompt">
              <CopyOutlined
                onClick={(e) => { e.stopPropagation(); copyToClipboard(block.label) }}
                style={{ color: c.dim, cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
              />
            </Tooltip>
            <Tooltip title="Edit & resend — load this prompt into the composer">
              <EditOutlined
                onClick={(e) => { e.stopPropagation(); loadToComposer() }}
                style={{ color: c.dim, cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
              />
            </Tooltip>
            <Tooltip title="Resend this prompt">
              <RedoOutlined
                onClick={(e) => { e.stopPropagation(); resend() }}
                style={{ color: c.dim, cursor: 'pointer', fontSize: 13, marginLeft: 2 }}
                onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
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
              borderBottom: `1px solid ${c.cardBorder}`,
              background: c.rowAlt,
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
                    style={{ color: c.dim, height: 22, padding: '0 5px' }}
                  />
                </Tooltip>
                {onAskLlm && (
                  <Tooltip title="Ask the assistant">
                    <Button
                      type="text" size="small" icon={<RobotOutlined />}
                      onClick={handleAskLlm}
                      style={{ color: c.dim, height: 22, padding: '0 5px' }}
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
                        style={{ color: !viewRaw ? c.link : c.dim, height: 22, padding: '0 5px' }}
                      />
                    </Tooltip>
                    <Tooltip title="Raw output">
                      <Button
                        type="text" size="small" icon={<CodeOutlined />}
                        onClick={() => setViewRaw(true)}
                        style={{ color: viewRaw ? c.link : c.dim, height: 22, padding: '0 5px' }}
                      />
                    </Tooltip>
                  </div>
                )}

                {/* Structured table view */}
                {!viewRaw && parsed?.kind === 'table' && parsed.rows && (
                  <JsonTable rows={parsed.rows} />
                )}

                {/* Structured grouped / section tables */}
                {!viewRaw && parsed?.kind === 'sections' && parsed.sections && (
                  <JsonSections meta={parsed.meta} sections={parsed.sections} />
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
                    color: c.mono, lineHeight: 1.6,
                    whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  }}>
                    {displayLines.map((l, i) => (
                      <span key={i} style={{ color: _lineColor(l, c.mono) }}>
                        {l.line}{'\n'}
                      </span>
                    ))}
                    {!block.done && <span style={{ color: c.faint }}>▌</span>}
                  </pre>
                )}

                {/* Truncation footer — raw view only */}
                {shouldTruncate && (viewRaw || !parsed || parsed.kind === 'raw') && (
                  <div style={{ position: 'relative' }}>
                    {/* gradient fade */}
                    <div style={{
                      position: 'absolute', top: -40, left: 0, right: 0, height: 40,
                      background: `linear-gradient(transparent, ${c.cardBg})`,
                      pointerEvents: 'none',
                    }} />
                    <div
                      onClick={() => setShowAll(true)}
                      style={{
                        padding: '5px 14px 10px',
                        fontSize: 12, color: c.dim, cursor: 'pointer',
                        fontFamily: 'monospace',
                      }}
                      onMouseEnter={e => (e.currentTarget.style.color = c.text)}
                      onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
                    >
                      ↓ {extraLines} more line{extraLines !== 1 ? 's' : ''} — click to expand
                    </div>
                  </div>
                )}
              </Fragment>
            )}

            {/* chat block: interleaved text segments + tool call blocks */}
            {block.type === 'chat' && (
              <ChatTranscript
                segments={block.segments}
                currentText={block.currentText}
                done={block.done}
                pendingConfirm={pendingConfirm}
                onAnswerConfirm={onAnswerConfirm}
              />
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
          { kind: 'tool' as const, entry: { name: tool_name, input: tool_input, running: true, startedAt: Date.now() } },
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
            const durationMs = seg.entry.startedAt ? Date.now() - seg.entry.startedAt : undefined
            return { ...seg, entry: { ...seg.entry, output, running: false, durationMs } }
          }
          return seg
        })
        return { ...b, segments: newSegs }
      }))
    }

    const onToolProgress = (e: Event) => {
      // Live progress for a still-running built-in tool (repo clone/grep, denylist
      // fan-out): turns the silent gap between tool_start and tool_end into a
      // moving status line on the tool segment.
      const { id, tool_name, message } = (e as CustomEvent).detail as {
        id: string; tool_name: string; message: string
      }
      if (!message) return
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        const newSegs = b.segments.map(seg => {
          if (seg.kind === 'tool' && seg.entry.name === tool_name && seg.entry.running) {
            return { ...seg, entry: { ...seg.entry, progress: message } }
          }
          return seg
        })
        return { ...b, segments: newSegs }
      }))
    }

    const onEnd = (e: Event) => {
      const { id, exit_code, duration_ms, stopped } = (e as CustomEvent).detail as { id: string; exit_code: number; duration_ms: number; stopped?: boolean }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        if (b.type === 'chat' && b.currentText) {
          return {
            ...b,
            segments: [...b.segments, { kind: 'text', text: b.currentText }],
            currentText: '',
            done: true, exitCode: exit_code, durationMs: duration_ms, stopped: !!stopped,
          }
        }
        return { ...b, done: true, exitCode: exit_code, durationMs: duration_ms, stopped: !!stopped }
      }))
    }

    const onChatUsage = (e: Event) => {
      const { id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens } =
        (e as CustomEvent).detail as {
          id: string; model?: string; input_tokens: number; output_tokens: number
          cache_read_tokens?: number; cache_creation_tokens?: number
        }
      setBlocks(prev => prev.map(b =>
        b.id === id ? { ...b, model: model || b.model, tokenUsage: {
          input: input_tokens, output: output_tokens,
          cacheRead: cache_read_tokens ?? 0, cacheCreation: cache_creation_tokens ?? 0,
        } } : b
      ))
    }

    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:tool_start', onToolStart)
    window.addEventListener('runspec:tool_end', onToolEnd)
    window.addEventListener('runspec:tool_progress', onToolProgress)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:chat_usage', onChatUsage)
    return () => {
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_start', onToolStart)
      window.removeEventListener('runspec:tool_end', onToolEnd)
      window.removeEventListener('runspec:tool_progress', onToolProgress)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:chat_usage', onChatUsage)
    }
  }, [])

  const addBlock = (block: InvocationBlock) => setBlocks(prev => [...prev, block])
  const clearBlocks = () => setBlocks([])

  return { blocks, addBlock, clearBlocks }
}
