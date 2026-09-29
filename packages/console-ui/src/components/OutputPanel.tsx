import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Checkbox, Dropdown, Input, Tag, Tooltip, Typography, message } from 'antd'
import type { MenuProps } from 'antd'
import { bridge } from '../bridge'
import { Markdown } from './Markdown'
import { useIsDark, textColor, useAccent, surfaces, textFaint, textMuted, textSecondary, MONO_FONT, RADIUS } from '../ThemeContext'
import {
  ThunderboltOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  RedoOutlined, EditOutlined, CopyOutlined, RobotOutlined, CaretRightOutlined,
  ApiOutlined, DownOutlined, RightOutlined, VerticalAlignBottomOutlined,
  TableOutlined, CodeOutlined, PauseCircleOutlined, StopOutlined, EllipsisOutlined, BulbOutlined,
} from '@ant-design/icons'

const { Text } = Typography

const TRUNCATE_AT = 20

// While a run is still streaming, the raw <pre> renders a live tail of at most
// this many lines. Rendering every line as it arrives grows the <pre> without
// bound and reconciles O(n²) DOM, hanging the card on a big dump; the full set
// (subject to the completed-view expand) is shown once the run finishes.
const STREAM_MAX_LINES = 500

// A structured JSON table renders a cell per column per row. Past this many rows
// the initial mount is capped and the rest revealed on demand — a 10k-row
// array-of-objects otherwise mounts tens of thousands of cells at once and hangs
// the card even after the stream completes. Each "show more" reveals up to
// TABLE_ROW_STEP additional rows (appended, so existing rows aren't re-rendered).
const TABLE_ROW_CAP = 200
const TABLE_ROW_STEP = 2000

// A completed run whose output exceeds this many chars renders behind a brief
// "Rendering result…" placeholder: the block flips done, paints the placeholder,
// then commits the heavy parse/table build on a later frame. Without it the
// parse + initial mount runs synchronously in the same commit that marks the
// block done — a multi-second UI freeze with no signal (the in-flight strip,
// which tracks the backend process, is already gone). Small results render
// inline as before (no placeholder flash).
const HEAVY_RENDER_CHARS = 100_000

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
  const accent = useAccent()
  const sf = surfaces(isDark)
  return {
    isDark,
    // Cards sit on the content (raised); their header strip and tool blocks
    // are one level up (overlay); code wells drop to sunken.
    cardBg: sf.raised,
    cardBorder: sf.border,
    headerBg: sf.overlay,
    text: textColor(isDark),
    dim: textMuted(isDark),
    faint: textFaint(isDark),
    caret: textFaint(isDark),
    mono: isDark ? '#d4d4d4' : '#1f1f1f',
    rowAlt: isDark ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.025)',
    tableHeadBorder: sf.border,
    tableRowBorder: sf.divider,
    tableHead: textMuted(isDark),
    codeBg: sf.sunken,
    toolBg: sf.overlay,
    toolBorder: sf.border,
    link: accent,
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

/** Human byte count for a char/byte length (e.g. "3.2 MB", "120 KB", "840 B"). */
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// A non-truncated tool result above this many chars (~12k tokens) is flagged as
// large — it stays in the conversation context for the rest of the chat.
const LARGE_OUTPUT_CHARS = 50_000

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

/** Render one value: primitives and scalar arrays inline as text — with NO hooks,
 *  so a data-heavy table of scalars stays as cheap as plain text. A nested object
 *  or array-of-objects delegates to the stateful disclosure cell below so it
 *  never collapses to "[object Object]". */
function JsonCell({ keyName, value }: { keyName: string; value: unknown }) {
  const isObj = typeof value === 'object' && value !== null
  const isScalarArray = Array.isArray(value)
    && (value.length === 0 || value.every(x => x === null || typeof x !== 'object'))
  if (!isObj || isScalarArray) return <>{_humanVal(keyName, value)}</>
  return <JsonCellDisclosure value={value} />
}

/** Expandable disclosure for a nested object / array-of-objects cell (sub-table
 *  or pretty JSON). Owns the open state + theme subscription so the common
 *  scalar cell — routed away in `JsonCell` before any hook runs — pays for
 *  neither; that's what keeps a wide table of thousands of scalar cells fast. */
function JsonCellDisclosure({ value }: { value: unknown }) {
  const c = useCardTheme()
  const [open, setOpen] = useState(false)
  const rowArray = isRowArray(value)
  const label = Array.isArray(value) ? `[${(value as unknown[]).length}]` : '{…}'
  return (
    <span>
      <a onClick={() => setOpen(o => !o)} style={{ color: c.link, cursor: 'pointer', whiteSpace: 'nowrap' }}>
        {open ? '▾' : '▸'} {label}
      </a>
      {open && (rowArray
        ? <JsonTable rows={value as Record<string, unknown>[]} />
        : <pre style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-all', color: c.sub, fontSize: 12 }}>
            {JSON.stringify(value, null, 2)}
          </pre>
      )}
    </span>
  )
}

function JsonTable({ rows }: { rows: Record<string, unknown>[] }) {
  const c = useCardTheme()
  // Cap the rendered rows; reveal the rest in TABLE_ROW_STEP chunks on demand.
  // Slicing always from 0 keeps earlier rows' keys stable, so expanding appends
  // rows rather than re-rendering the whole table.
  const [limit, setLimit] = useState(TABLE_ROW_CAP)
  if (!rows.length) return null
  const keys = Object.keys(rows[0])
  const shown = rows.length > limit ? rows.slice(0, limit) : rows
  const hidden = rows.length - shown.length
  return (
    <div style={{ overflowX: 'auto', padding: '8px 14px' }}>
      <table style={{
        width: '100%', borderCollapse: 'collapse',
        fontFamily: MONO_FONT, fontSize: 13, color: c.mono,
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
          {shown.map((row, i) => (
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
      {hidden > 0 && (
        <div
          onClick={() => setLimit(l => l + TABLE_ROW_STEP)}
          style={{
            padding: '6px 0 2px', fontSize: 13, color: c.dim,
            cursor: 'pointer', fontFamily: MONO_FONT,
          }}
          onMouseEnter={e => (e.currentTarget.style.color = c.text)}
          onMouseLeave={e => (e.currentTarget.style.color = c.dim)}
        >
          ↓ showing {shown.length.toLocaleString()} of {rows.length.toLocaleString()} rows — show {Math.min(hidden, TABLE_ROW_STEP).toLocaleString()} more
        </div>
      )}
    </div>
  )
}

/** Key-value view for JSON objects. */
function JsonObject({ obj }: { obj: Record<string, unknown> }) {
  const c = useCardTheme()
  return (
    <div style={{ padding: '8px 14px', display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '4px 16px', fontFamily: MONO_FONT, fontSize: 13 }}>
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
        <div style={{ fontFamily: MONO_FONT, fontSize: 13, color: c.dim, marginBottom: 8 }}>
          {metaEntries.map(([k, v]) => `${k.replace(/_/g, ' ')}: ${_humanVal(k, v)}`).join('   ')}
        </div>
      )}
      {sections.map((s, i) => (
        <div key={i} style={{ marginBottom: i < sections.length - 1 ? 14 : 0 }}>
          {s.title && (
            <div style={{ fontFamily: MONO_FONT, fontSize: 13, color: c.mono, fontWeight: 600 }}>
              {s.title.replace(/_/g, ' ')}
            </div>
          )}
          {s.rows.length > 0
            ? <JsonTable rows={s.rows} />
            : <div style={{ fontFamily: MONO_FONT, fontSize: 13, color: c.dim, padding: '4px 14px' }}>(no rows)</div>}
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
  // The model's tool-call id (runspec:tool_start/tool_end `call_id`). Pairs a
  // result with *its* card when several same-name calls run concurrently (the
  // agent loop runs independent runnable calls in parallel). Absent on older
  // backends and on reopened transcripts.
  callId?: string
  input: Record<string, unknown>
  output?: string
  running: boolean
  startedAt?: number   // epoch ms when the tool call began (for per-tool duration)
  durationMs?: number  // set when the tool call finishes
  progress?: string    // latest live progress line (built-in tools: repo clone/grep, denylist fan-out)
  outputChars?: number // full output size (the displayed output is clipped to 2000)
  // Set when the result was cut at the output cap before the model saw it — the
  // model received incomplete (mid-structure) JSON. Carries the original size.
  truncated?: { orig_len: number; cap: number } | null
  // The operator's confirm-gate decision on this call (when it was gated),
  // rendered as a badge on a reopened conversation. Operator-only — not sent to
  // the model. 'denied' means the call never ran.
  operatorDecision?: 'approved' | 'edited' | 'denied'
  // The reasoning summary that led to this call ("why it ran this") and how
  // long it streamed — shown behind the row's bulb. Display-only.
  thinking?: string
  thinkingMs?: number
}

export interface ToolEndDetail {
  call_id?: string
  tool_name: string
  output: string
  output_chars?: number
  truncated?: { orig_len: number; cap: number } | null
  operator_decision?: 'approved' | 'edited' | 'denied' | ''
}

/**
 * Apply a runspec:tool_end to a block's segments: finish exactly ONE running
 * tool card. With a `call_id`, that's the card started with the same id; without
 * one (an older backend), the first running card with the same name. Matching
 * every running same-name card — the old behaviour — was harmless while tool
 * calls ran one at a time, but with concurrent calls the first result to land
 * would have finished all of them with its output.
 */
export function finishToolSegment(segments: BlockSegment[], detail: ToolEndDetail, now: number = Date.now()): BlockSegment[] {
  const { call_id, tool_name, output, output_chars, truncated, operator_decision } = detail
  const idx = segments.findIndex(seg =>
    seg.kind === 'tool' && seg.entry.running && (
      call_id && seg.entry.callId ? seg.entry.callId === call_id : seg.entry.name === tool_name
    ))
  if (idx < 0) return segments
  const seg = segments[idx]
  if (seg.kind !== 'tool') return segments
  const durationMs = seg.entry.startedAt ? now - seg.entry.startedAt : undefined
  // Keep the confirm-gate badge on the live card (the backend sends the
  // decision on tool_end) so an approved/denied action stays marked as gated
  // instead of reverting to look like an autonomous run. "" = the gate never
  // fired, so leave any existing value untouched.
  const done: BlockSegment = {
    ...seg,
    entry: {
      ...seg.entry, output, running: false, durationMs, outputChars: output_chars, truncated: truncated ?? null,
      ...(operator_decision ? { operatorDecision: operator_decision } : {}),
    },
  }
  return [...segments.slice(0, idx), done, ...segments.slice(idx + 1)]
}

export type BlockSegment =
  | { kind: 'text'; text: string }
  | { kind: 'tool'; entry: ToolCallEntry }

export interface InvocationBlock {
  id: string
  // The console session this block belongs to (an interactive chat, or the
  // trigger / schedule session whose id == the run card id). The Console shows
  // one session's blocks at a time; see src/sessions.ts.
  sessionId: string
  type: 'run' | 'chat'
  label: string
  startedAt: string  // ISO string captured when block is created
  lines: OutputLine[]
  segments: BlockSegment[]   // for chat blocks — interleaved text + tool calls
  currentText: string        // currently-streaming text (chat blocks only)
  // The reasoning summary streaming right now (runspec:thinking), shown
  // expanded in the card until the step it led to starts — then it collapses
  // into that tool row's bulb (runspec:tool_start `thinking`) or into the
  // reply's reasoning line (runspec:reply_thinking).
  liveThinking?: string
  replyThinking?: { text: string; ms: number }
  done: boolean
  stopped?: boolean   // chat turn was halted by the user (distinct from error)
  exitCode?: number
  durationMs?: number
  rerunData?: RerunData
  model?: string      // model the chat turn ran on (for cost estimate)
  tokenUsage?: { input: number; output: number; cacheRead: number; cacheCreation: number }
  // Whether this card's content is part of the agent's context. Defaults by type
  // at creation: chat cards start in-context (true), user-run cards start out
  // (false — opt in via the checkbox / "Ask the assistant"). Toggling a chat card
  // off calls bridge.set_turn_included; a ticked run card stages its output into
  // the next chat send. Reversible — the backend keeps the messages and only
  // filters, so re-ticking restores the exchange.
  inContext?: boolean
  // Set once the card's messages have been compacted away (chat_compacted): the
  // exchange is gone from history, so the toggle is retired (shown disabled).
  compacted?: boolean
  // A reopened operator-initiated manual run: rendered for the operator's review
  // but never part of the agent's context. Drives a distinguishing tag and
  // suppresses the in-context checkbox.
  operatorOnly?: boolean
  // Set when this block is a trigger fire mirrored into the Console (from
  // runspec:trigger_card). Drives a distinguishing "Trigger" tag; the run/chat
  // content still streams through the normal id-keyed handlers.
  trigger?: { name: string; source: string }
}

/** Derive an edit-and-rerun target from an agent tool-call entry, or null when
 *  the call isn't a runnable invocation (a built-in, an MCP tool, or a wrapper
 *  we can't resolve). Covers the three `[llm] tool_mode` shapes:
 *   - search   → tool named `run_runnable`, input `{ id, host, args, command? }`
 *   - per_host → tool named `<host>__<name>`, input is the args map (+ `command`)
 *   - dedup    → tool named `<group>__<name>`, input is the args map (+ `command`);
 *                the prefix is a venv/group, resolved to a host App-side.
 *  The returned `host` is the concrete host in search/per_host mode and the
 *  venv/group prefix in dedup mode — App's `onEditInForm` resolves it either way. */
export function rerunFromToolEntry(entry: ToolCallEntry): RerunData | null {
  const name = entry.name
  const input = (entry.input ?? {}) as Record<string, unknown>
  const asArgs = (v: unknown): Record<string, unknown> =>
    v && typeof v === 'object' && !Array.isArray(v) ? { ...(v as Record<string, unknown>) } : {}
  const asPath = (v: unknown): string[] =>
    Array.isArray(v) ? (v as unknown[]).map(String) : []

  // search mode: the generic `run_runnable` envelope carries everything.
  if (name === 'run_runnable') {
    const id = typeof input.id === 'string' ? input.id : ''
    if (!id) return null
    return {
      host: typeof input.host === 'string' ? input.host : '',
      runnable: id,
      args: asArgs(input.args),
      commandPath: asPath(input.command),
    }
  }
  // MCP tools and built-ins are not runnable invocations.
  if (name.startsWith('mcp_')) return null
  // Prefixed runnable tools (dedup/per_host). Match the same `<prefix>__<name>`
  // shape the UI uses to strip the display prefix (`/^[^_]+__/`), so a built-in
  // with only single underscores (e.g. `run_python_sandboxed`) never matches.
  const m = name.match(/^([^_]+)__(.+)$/)
  if (!m) return null
  const rest = { ...input }
  const commandPath = asPath(rest.command)
  delete rest.command
  return { host: m[1], runnable: m[2], args: rest, commandPath }
}

/** "4s" / "<1s" for a thought's streaming time; "" when unknown (0). */
export function fmtThought(ms: number | undefined): string {
  if (!ms || ms <= 0) return ''
  return ms < 1000 ? '<1s' : `${Math.round(ms / 1000)}s`
}

/**
 * What a tool row shows as its name + args. The search-mode wrappers carry
 * the real call inside their input — `run_runnable {id, host, command, args}`
 * and `use_tool {name, input}` — so showing the wrapper name made every row
 * read `run_runnable` and the operator had to dig into the args to see which
 * runnable ran. Unwrap them (like the backend's confirm-card view) and keep
 * the wrapper name as `via` for a tooltip. Other tools: the name minus its
 * `<group>__` / `<host>__` prefix, args as given.
 */
export function toolDisplay(entry: ToolCallEntry): { name: string; args: [string, unknown][]; via?: string } {
  const input = (entry.input ?? {}) as Record<string, unknown>
  const isObj = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === 'object' && !Array.isArray(v)
  if (entry.name === 'run_runnable' && typeof input.id === 'string' && input.id) {
    const cmd = Array.isArray(input.command)
      ? (input.command as unknown[]).map(String).join(' ')
      : typeof input.command === 'string' ? input.command : ''
    // A group-qualified id (`runspec_linux__find-large-files`) — the group may
    // itself contain underscores, so cut at the last `__`.
    const id = input.id.includes('__') ? input.id.slice(input.id.lastIndexOf('__') + 2) : input.id
    const name = [id, cmd].filter(Boolean).join(' ')
    const args: [string, unknown][] = []
    if (typeof input.host === 'string' && input.host) args.push(['host', input.host])
    if (isObj(input.args)) args.push(...Object.entries(input.args))
    return { name, args, via: 'run_runnable' }
  }
  if (entry.name === 'use_tool' && typeof input.name === 'string' && input.name) {
    return {
      name: input.name,
      args: isObj(input.input) ? Object.entries(input.input) : [],
      via: 'use_tool',
    }
  }
  return { name: entry.name.replace(/^[^_]+__/, ''), args: Object.entries(input) }
}

/**
 * The card header's ⋯ menu: the prompt actions for a finished chat turn and
 * the rerun actions for a finished run. Empty while running (nothing to act on
 * yet), so the ⋯ button is hidden then.
 */
export function cardMoreItems(
  block: Pick<InvocationBlock, 'type' | 'done' | 'label' | 'rerunData'>,
  opts: { canRerun: boolean; canEditInForm: boolean },
): NonNullable<MenuProps['items']> {
  if (!block.done) return []
  if (block.type === 'chat') {
    if (!block.label) return []
    return [
      { key: 'copy-prompt', icon: <CopyOutlined />, label: 'Copy prompt' },
      { key: 'edit-resend', icon: <EditOutlined />, label: 'Edit & resend' },
      { key: 'resend', icon: <RedoOutlined />, label: 'Resend' },
    ]
  }
  if (!block.rerunData || !opts.canRerun) return []
  return [
    ...(opts.canEditInForm
      ? [{ key: 'edit-form', icon: <EditOutlined />, label: 'Edit in a form & rerun' }]
      : []),
    { key: 'rerun', icon: <RedoOutlined />, label: 'Rerun with same args' },
  ]
}

/** Serialise a chat transcript (interleaved assistant text + tool calls) to
 *  plain text for the clipboard, so Copy captures the tools the agent ran and
 *  their output — not just the prose. Each tool call renders as a `▶ name args`
 *  header line followed by its (indented-free) output block. */
export function transcriptToText(segments: BlockSegment[], currentText?: string): string {
  const parts: string[] = []
  for (const seg of segments) {
    if (seg.kind === 'text') {
      if (seg.text) parts.push(seg.text)
      continue
    }
    const e = seg.entry
    const short = e.name.replace(/^[^_]+__/, '')
    const args = Object.entries(e.input ?? {})
      .map(([k, v]) => `${k}=${displayArgValue(v)}`)
      .join(' ')
    const header = args ? `▶ ${short} ${args}` : `▶ ${short}`
    const body = e.output !== undefined
      ? (e.output || '(no output)')
      : (e.running ? '(running…)' : '(no output)')
    parts.push(`${header}\n${body}`)
  }
  if (currentText) parts.push(currentText)
  return parts.join('\n\n')
}

interface OutputPanelProps {
  blocks: InvocationBlock[]
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
  // Toggle whether a card's content is in the agent's context (the header
  // checkbox). id = block id; inContext = the new desired state.
  onToggleContext?: (id: string, inContext: boolean) => void
}

export interface PendingConfirm {
  request_id: string
  id: string          // chat block this confirm belongs to
  tool_name: string   // full host__tool name, matches the tool segment's entry.name
  tool_input: Record<string, unknown>
  // Human-readable view of what's being approved. For the search-mode
  // `run_runnable` wrapper these unwrap to the real runnable + args; for a plain
  // tool call they equal tool_name/tool_input. Optional for back-compat.
  display_name?: string
  display_input?: Record<string, unknown>
  // Verb for the Approve button / header ("Approve & <verb>"). "run" for a
  // runnable or executing tool; "save"/"create"/"open"/… for a tool that
  // stores or opens rather than executes. Optional for back-compat with an
  // older backend that didn't send it (falls back to "run").
  confirm_verb?: string
  autonomy: string
  // The gated tool call's id (runspec:tool_confirm `call_id`). Absent on older
  // backends and on gates with no agent tool call (rule runs).
  call_id?: string
}

/**
 * Does this pending confirm belong to this tool card? By call id when both
 * sides carry one — several cards with the same tool name can be running (in
 * search mode every runnable call is `run_runnable`), and name-only matching put
 * the Approve/Deny gate on the wrong (earlier) card. Falls back to the tool
 * name for an older backend. Only a running card can own a gate.
 */
export function confirmOwnsSegment(pc: PendingConfirm | null | undefined, entry: ToolCallEntry): boolean {
  if (!pc || !entry.running) return false
  if (pc.call_id && entry.callId) return pc.call_id === entry.callId
  return pc.tool_name === entry.name
}

// What the currently-active block is doing right now, for the sticky footer:
// a running tool (its short name + any live progress) wins, else the model is
// streaming its reply, else it's thinking/acting between steps.
function activeActivity(block: InvocationBlock): { label: string; tool?: string; progress?: string } {
  if (block.type === 'run') return { label: 'Running', tool: block.label.replace(/^\//, '') }
  for (let i = block.segments.length - 1; i >= 0; i--) {
    const seg = block.segments[i]
    if (seg.kind === 'tool' && seg.entry.running) {
      return { label: 'Running', tool: toolDisplay(seg.entry).name, progress: seg.entry.progress }
    }
  }
  if (block.currentText && block.currentText.length > 0) return { label: 'Responding…' }
  return { label: 'Working…' }
}

// The last non-empty line of a streamed reasoning summary, trimmed for the bar.
export function lastLine(text: string): string {
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ''
}

// How much of the latest reasoning line the activity bar shows (~3 wrapped rows
// at its max width). Longer lines keep their *tail* — the newest words — behind a
// leading ellipsis, since a streaming summary grows at the end.
export const THINKING_MAX_CHARS = 280

// The latest reasoning line, capped to its newest THINKING_MAX_CHARS (cut at a
// word boundary where one is near) for the activity bar.
export function thinkingTail(text: string, max = THINKING_MAX_CHARS): string {
  const line = lastLine(text)
  if (line.length <= max) return line
  let tail = line.slice(-max)
  const space = tail.indexOf(' ')
  if (space > 0 && space < 30) tail = tail.slice(space + 1)
  return `…${tail}`
}

// A sticky bottom activity bar shown while a turn is in flight. Long turns with
// many tool cards push the card's own spinner far above the fold; this keeps a
// live "still working" signal — the current step + elapsed time — pinned at the
// bottom, and doubles as a jump-to-latest button. Its own 1s ticker keeps the
// clock live without re-rendering the whole panel.
function ActivityIndicator({ block, onClick }: { block: InvocationBlock; onClick: () => void }) {
  const c = useCardTheme()
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const act = activeActivity(block)
  // Reasoning summaries (runspec:thinking — sent by the anthropic built-in with
  // [llm] thinking_display = "summarized"). Shown only while the turn is between
  // steps; a new step (a tool starting, text streaming) starts a fresh summary.
  const [thinking, setThinking] = useState('')
  useEffect(() => {
    const onThinking = (e: Event) => {
      const d = (e as CustomEvent<{ id: string; text: string }>).detail
      if (d?.id === block.id && d.text) setThinking(t => (t + d.text).slice(-2000))
    }
    window.addEventListener('runspec:thinking', onThinking)
    return () => window.removeEventListener('runspec:thinking', onThinking)
  }, [block.id])
  useEffect(() => { setThinking('') }, [act.label, act.tool])
  const thinkingLine = act.label === 'Working…' ? thinkingTail(thinking) : ''
  const secs = Math.max(0, Math.floor((Date.now() - new Date(block.startedAt).getTime()) / 1000))
  const elapsed = secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`
  return (
    <div style={{ position: 'absolute', bottom: 12, left: '50%', transform: 'translateX(-50%)', zIndex: 10, maxWidth: '90%' }}>
      <Tooltip title="Jump to the latest activity">
        <div
          onClick={onClick}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
            background: c.headerBg, border: `1px solid ${c.cardBorder}`, borderRadius: 14,
            padding: '4px 12px', maxWidth: '100%',
            boxShadow: 'var(--rs-shadow-md)',
          }}
        >
          <LoadingOutlined style={{ color: 'var(--rs-accent)', fontSize: 13 }} spin />
          <Text style={{ fontSize: 13, color: c.dim, flexShrink: 0 }}>{thinkingLine ? 'Thinking…' : act.label}</Text>
          {thinkingLine && (
            // Wraps up to 3 rows (and widens to 640px) so a longer summary
            // reads in full instead of being cut to one short line; the
            // line-clamp is a backstop — thinkingTail already bounds the text.
            <Text
              data-testid="thinking-summary"
              style={{
                fontSize: 12, color: c.dim, lineHeight: 1.4, maxWidth: 640, minWidth: 0,
                whiteSpace: 'normal', overflowWrap: 'anywhere', overflow: 'hidden',
                display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
              }}
            >
              {thinkingLine}
            </Text>
          )}
          {act.tool && (
            <Text code style={{ fontSize: 12, flexShrink: 0 }}>{act.tool}</Text>
          )}
          {act.progress && (
            <Text ellipsis style={{ fontSize: 12, color: c.dim, fontFamily: MONO_FONT, maxWidth: 220 }}>{act.progress}</Text>
          )}
          <Text style={{ fontSize: 12, color: c.dim, flexShrink: 0, fontFamily: MONO_FONT }}>· {elapsed}</Text>
          <VerticalAlignBottomOutlined style={{ fontSize: 12, color: c.dim, flexShrink: 0 }} />
        </div>
      </Tooltip>
    </div>
  )
}

export function OutputPanel({ blocks, onRerun, onAskLlm, onToggleContext }: OutputPanelProps) {
  const c = useCardTheme()
  const scrollRef = useRef<HTMLDivElement>(null)
  const isAtBottomRef = useRef(true)
  const blockCountRef = useRef(0)
  const [showScrollDown, setShowScrollDown] = useState(false)

  // Autonomy gate: the bridge dispatches runspec:tool_confirm (keyed by the run's
  // chat/card id) and blocks until we answer via resolve_tool_confirmation. Held
  // as a map keyed by that id so several parked runs — e.g. concurrent trigger
  // fires, now mirrored into the Console — each keep their own gate. Rendered as a
  // normal element in this always-mounted component, not an antd static Modal
  // (which does not render reliably inside the WebView2 host).
  const [pendingConfirms, setPendingConfirms] = useState<Record<string, PendingConfirm>>({})

  // Cold-start seed: a run may have parked at the gate before this view mounted (a
  // reopened console, or a trigger that fired while another tab was active).
  // Defensive: tolerate an older backend without the method, or a stubbed bridge.
  useEffect(() => {
    const p = bridge.get_pending_confirms?.()
    if (!p || typeof p.then !== 'function') return
    p.then(gates => {
      if (!gates?.length) return
      setPendingConfirms(prev => {
        const next = { ...prev }
        for (const g of gates) next[g.id] = g
        return next
      })
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const onToolConfirm = (e: Event) => {
      const { id, request_id, tool_name, tool_input, display_name, display_input, confirm_verb, autonomy, call_id } = (e as CustomEvent).detail as {
        id: string; request_id: string; tool_name: string; tool_input: Record<string, unknown>
        display_name?: string; display_input?: Record<string, unknown>; confirm_verb?: string; autonomy: string
        call_id?: string
      }
      setPendingConfirms(prev => ({ ...prev, [id]: { request_id, id, tool_name, tool_input: tool_input ?? {}, display_name, display_input, confirm_verb, autonomy, ...(call_id ? { call_id } : {}) } }))
    }
    // Authoritative "this gate finished" from the backend (approved / denied /
    // timed out) — request-id-precise so a late signal can't clear a newer gate
    // for the same id.
    const onResolved = (e: Event) => {
      const { id, request_id } = (e as CustomEvent).detail as { id: string; request_id: string }
      setPendingConfirms(prev => {
        if (!prev[id] || prev[id].request_id !== request_id) return prev
        const next = { ...prev }; delete next[id]; return next
      })
    }
    // If the run ends while its gate is still open — e.g. the user hit Stop — drop
    // the now-stale prompt so it doesn't linger.
    const onEnd = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPendingConfirms(prev => { if (!prev[id]) return prev; const next = { ...prev }; delete next[id]; return next })
    }
    window.addEventListener('runspec:tool_confirm', onToolConfirm)
    window.addEventListener('runspec:confirm_resolved', onResolved)
    window.addEventListener('runspec:run_end', onEnd)
    return () => {
      window.removeEventListener('runspec:tool_confirm', onToolConfirm)
      window.removeEventListener('runspec:confirm_resolved', onResolved)
      window.removeEventListener('runspec:run_end', onEnd)
    }
  }, [])

  // Stable across renders (only the setter + module-level bridge are closed over)
  // so it doesn't defeat the memoised BlockCard by handing each card a fresh prop
  // on every streamed frame. Keyed by the gate's request_id — each BlockCard binds
  // its own gate's request_id before calling.
  const answerConfirm = useCallback((requestId: string, approved: boolean, editedInput?: Record<string, unknown>) => {
    bridge.resolve_tool_confirmation(requestId, approved, editedInput)
      .catch(err => console.error('[runspec] resolve_tool_confirmation failed', err))
    setPendingConfirms(prev => {
      const hit = Object.entries(prev).find(([, v]) => v.request_id === requestId)
      if (!hit) return prev
      const next = { ...prev }; delete next[hit[0]]; return next
    })
  }, [])

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

  // The turn/run currently in flight (if any), driving the sticky footer.
  const activeBlock = blocks.find(b => !b.done)

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
              onToggleContext={onToggleContext}
              pendingConfirm={pendingConfirms[block.id] ?? null}
              onAnswerConfirm={answerConfirm}
            />
          ))}
        </div>
      </div>

      {/* Sticky activity indicator — pinned while a turn is in flight so the
          "still working" signal survives long tool runs scrolling the card's own
          spinner out of view; doubles as jump-to-latest. When nothing is active,
          the plain jump-to-bottom pill takes over (only when scrolled up). */}
      {activeBlock ? (
        <ActivityIndicator block={activeBlock} onClick={scrollToBottom} />
      ) : showScrollDown ? (
        <div style={{ position: 'absolute', bottom: 12, left: '50%', transform: 'translateX(-50%)', zIndex: 10 }}>
          <Button
            size="small"
            icon={<VerticalAlignBottomOutlined />}
            onClick={scrollToBottom}
            style={{
              background: c.headerBg, borderColor: c.cardBorder, color: c.dim,
              fontSize: 12, height: 26, padding: '0 10px',
              boxShadow: 'var(--rs-shadow-md)',
            }}
          >
            latest
          </Button>
        </div>
      ) : null}
    </div>
  )
}

// Edit-buffer encoding for editable confirmations: a plain string stays bare
// (so a path/text isn't shown wrapped in quotes), a list of lines (e.g. a
// run_python_sandboxed `code` array) is joined with real newlines so multi-line
// source is readable/editable rather than shown as ["…","…"], everything else is JSON.
function jsonValue(v: unknown): string {
  if (typeof v === 'string') return v
  if (Array.isArray(v) && v.every(x => typeof x === 'string')) return (v as string[]).join('\n')
  return JSON.stringify(v)
}

// Read-only display of a confirm arg's value. A multi-line string or a list of
// lines (a code snippet / SQL / document body) renders with its real newlines so
// it's legible instead of a JSON blob full of \n; short scalars keep their compact
// JSON form (so a string still reads as "n1", a number as 1) — unchanged.
function displayArgValue(v: unknown): string {
  if (Array.isArray(v) && v.every(x => typeof x === 'string')) return (v as string[]).join('\n')
  if (typeof v === 'string' && v.includes('\n')) return v
  return JSON.stringify(v)
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
  verb,
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
  // The action verb for the header + Approve button — "run" for a runnable or
  // executing tool, "save"/"create"/"open"/… for a tool that stores or opens
  // rather than executes. Defaults to "run".
  verb?: string
  // The operator may edit the args before approving (editable confirmations).
  // Only when the displayed call equals the real call (a plain/direct tool, not
  // the search-mode `run_runnable` wrapper whose display args would re-wrap lossily).
  editable?: boolean
  onAnswer?: (approved: boolean, editedInput?: Record<string, unknown>) => void
}) {
  const c = useCardTheme()
  const [expanded, setExpanded] = useState(false)
  const v = verb || 'run'
  const args = Object.entries(entry.input)
  // The row's label: search-mode wrappers show the runnable / tool they ran.
  const display = toolDisplay(entry)
  // Edit-and-rerun target when this call is a runnable invocation (search /
  // per_host / dedup); null for built-ins / MCP tools (no affordance).
  const rerun = !entry.running && !awaiting ? rerunFromToolEntry(entry) : null
  // The card can expand to reveal the full command + output whenever there's
  // output OR any args — so the full (un-ellipsised) command is always reachable,
  // not only when the tool returned something.
  const canExpand = entry.output !== undefined || args.length > 0 || !!entry.thinking
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
      // Did the operator actually change anything? Compare the edit buffer to a
      // fresh seed of the original args (both plain strings — this sidesteps the
      // lossy jsonValue→parseEdited round-trip that would otherwise make an
      // untouched value look different). Only send `editedInput` when a field's
      // text really changed, so a bare Approve records "approved", not "edited",
      // and runs the agent's original args verbatim (no round-trip corruption).
      const seed = Object.fromEntries(approveArgs.map(([k, val]) => [k, jsonValue(val)]))
      const changed = Object.keys(edits).some(k => edits[k] !== seed[k])
      if (changed) {
        const out: Record<string, unknown> = {}
        for (const [k, s] of Object.entries(edits)) out[k] = parseEdited(s)
        onAnswer?.(true, out)
        return
      }
    }
    onAnswer?.(true)
  }

  return (
    <div style={{
      margin: '6px 0',
      border: `1px solid ${awaiting ? (c.isDark ? '#5a4a1a' : '#ffd591') : c.toolBorder}`,
      borderRadius: RADIUS.sm, overflow: 'hidden',
    }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px',
          background: awaiting ? (c.isDark ? '#241f12' : '#fff7e6') : c.toolBg,
          cursor: canExpand ? 'pointer' : 'default',
          userSelect: 'none',
        }}
        onClick={() => canExpand && setExpanded(x => !x)}
      >
        {awaiting
          ? <PauseCircleOutlined style={{ color: '#e6a23c', fontSize: 13 }} />
          : entry.running
            ? <LoadingOutlined style={{ color: 'var(--rs-accent)', fontSize: 13 }} />
            : <ApiOutlined style={{ color: '#52c41a', fontSize: 13 }} />}
        {display.via ? (
          <Tooltip title={`Run via ${display.via}`}>
            <Text code style={{ fontSize: 13, flexShrink: 0 }}>{display.name}</Text>
          </Tooltip>
        ) : (
          <Text code style={{ fontSize: 13, flexShrink: 0 }}>{display.name}</Text>
        )}
        {entry.operatorDecision && (
          <Tooltip title={
            entry.operatorDecision === 'denied' ? 'You denied this call — it did not run'
            : entry.operatorDecision === 'edited' ? 'You edited the arguments before approving'
            : 'You approved this call'
          }>
            <Text style={{
              fontSize: 11, flexShrink: 0, fontWeight: 600, textTransform: 'uppercase',
              color: entry.operatorDecision === 'denied' ? '#ff4d4f' : entry.operatorDecision === 'edited' ? '#faad14' : '#52c41a',
            }}>
              {entry.operatorDecision}
            </Text>
          </Tooltip>
        )}
        {display.args.length > 0 && (
          <Text ellipsis style={{ flex: 1, minWidth: 0, color: c.dim, fontSize: 12 }}>
            {display.args.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}
          </Text>
        )}
        {entry.thinking && (
          <Tooltip title="Has reasoning — open the row to see why it ran this">
            <BulbOutlined
              aria-label="Has reasoning"
              style={{ color: c.dim, fontSize: 13, flexShrink: 0, marginLeft: display.args.length > 0 ? 0 : 'auto' }}
            />
          </Tooltip>
        )}
        {awaiting && (
          <Text style={{ color: '#e6a23c', fontSize: 12, marginLeft: 'auto', flexShrink: 0 }}>needs approval</Text>
        )}
        {!awaiting && entry.running && (
          <Text ellipsis style={{ color: c.dim, fontSize: 12, marginLeft: 'auto', flexShrink: 0, maxWidth: '50%', fontFamily: entry.progress ? 'monospace' : undefined }}>
            {entry.progress || 'running…'}
          </Text>
        )}
        {!entry.running && entry.durationMs !== undefined && (
          <Tooltip title="Time this tool call took">
            <Text style={{ color: c.dim, fontSize: 12, marginLeft: 'auto', flexShrink: 0, fontFamily: MONO_FONT }}>
              {fmtDurationMs(entry.durationMs)}
            </Text>
          </Tooltip>
        )}
        {!entry.running && entry.truncated && (
          <Tooltip title={`This tool returned ${fmtBytes(entry.truncated.orig_len)} but was cut to ${fmtBytes(entry.truncated.cap)} before the model saw it — the JSON it received is incomplete. Have the runnable return a summary and let the agent request specifics, or raise [llm] tool_output_cap.`}>
            <Text style={{ color: '#ff4d4f', fontSize: 12, flexShrink: 0, fontWeight: 600 }}>
              ⚠ output cut
            </Text>
          </Tooltip>
        )}
        {!entry.running && !entry.truncated && entry.outputChars !== undefined && entry.outputChars > LARGE_OUTPUT_CHARS && (
          <Tooltip title={`Large result (${fmtBytes(entry.outputChars)}). It stays in the conversation context for the rest of the chat — if it's data-heavy, a summary-first runnable keeps the window lean.`}>
            <Text style={{ color: '#faad14', fontSize: 12, flexShrink: 0, fontFamily: MONO_FONT }}>
              {fmtBytes(entry.outputChars)}
            </Text>
          </Tooltip>
        )}
        {canExpand && !entry.running && (
          <Text style={{ color: c.dim, fontSize: 12, marginLeft: 'auto', flexShrink: 0 }}>
            {expanded ? '▲ hide details' : '▼ show details'}
          </Text>
        )}
        {!awaiting && !entry.running && (
          <Tooltip title="Copy this tool call and its output">
            <CopyOutlined
              onClick={(e) => { e.stopPropagation(); copyToClipboard(transcriptToText([{ kind: 'tool', entry }])) }}
              style={{ color: c.dim, cursor: 'pointer', fontSize: 13, flexShrink: 0, marginLeft: 4 }}
              onMouseEnter={ev => (ev.currentTarget.style.color = c.text)}
              onMouseLeave={ev => (ev.currentTarget.style.color = c.dim)}
            />
          </Tooltip>
        )}
        {rerun && (
          <Tooltip title="Edit in a form & rerun — opens the run dialog pre-filled with these values">
            <EditOutlined
              onClick={(e) => { e.stopPropagation(); window.dispatchEvent(new CustomEvent('runspec:edit_in_form', { detail: rerun })) }}
              style={{ color: c.dim, cursor: 'pointer', fontSize: 13, flexShrink: 0, marginLeft: 4 }}
              onMouseEnter={ev => (ev.currentTarget.style.color = c.text)}
              onMouseLeave={ev => (ev.currentTarget.style.color = c.dim)}
            />
          </Tooltip>
        )}
      </div>

      {awaiting && (
        <div style={{ padding: '10px 12px', background: c.isDark ? '#1c1810' : '#fffbe6', borderTop: `1px solid ${c.isDark ? '#5a4a1a' : '#ffe7ba'}` }}>
          <div style={{ fontSize: 13, color: textSecondary(c.isDark), marginBottom: 10 }}>
            The assistant wants to {v}{' '}
            {resolved && <Text code style={{ fontSize: 12 }}>{resolved.name.replace(/^[^_]+__/, '')}</Text>}
            {resolved ? ' ' : 'this '}— autonomy is{' '}
            <strong style={{ color: '#e6a23c' }}>{awaiting}</strong>, so it needs your go-ahead.
          </div>
          {approveArgs.length > 0 && editable && edits ? (
            // Editable confirmations: let the operator tweak each arg before
            // approving. One field per arg (JSON-encoded so a number/bool/string
            // round-trips); the edited values run instead of the agent's proposal.
            <div style={{ margin: '0 0 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {approveArgs.map(([k]) => {
                // A long or multi-line value (a runbook's `steps`, a document
                // body, a SQL statement) is unreadable and uneditable in a
                // one-line Input — only its first line is reachable. Grow those
                // into a textarea so the operator can actually review what they
                // are approving.
                const val = edits[k] ?? ''
                const multiline = val.includes('\n') || val.length > 80
                return (
                  <div key={k} style={{ display: 'flex', alignItems: multiline ? 'flex-start' : 'center', gap: 8 }}>
                    <Text code style={{ fontSize: 12, flexShrink: 0, minWidth: 90, marginTop: multiline ? 4 : 0 }}>{k}</Text>
                    {multiline ? (
                      <Input.TextArea
                        size="small"
                        value={val}
                        autoSize={{ minRows: 2, maxRows: 20 }}
                        onChange={e => setEdits(prev => ({ ...(prev ?? {}), [k]: e.target.value }))}
                        style={{ fontFamily: MONO_FONT, fontSize: 12 }}
                      />
                    ) : (
                      <Input
                        size="small"
                        value={val}
                        onChange={e => setEdits(prev => ({ ...(prev ?? {}), [k]: e.target.value }))}
                        style={{ fontFamily: MONO_FONT, fontSize: 12 }}
                      />
                    )}
                  </div>
                )
              })}
            </div>
          ) : approveArgs.length > 0 ? (
            // The header preview ellipsises; show the full args here, wrapped, so
            // the operator can see exactly what they're approving — a many-arg
            // runnable used to push this (and the buttons) off-screen. This is the
            // path hit when a sandboxed code call arrives via search-mode use_tool
            // (unwrapped, so not editable): render string / line-array values with
            // their REAL newlines (not JSON-escaped \n) so the code is readable,
            // and auto-grow to a generous cap before scrolling.
            <pre style={{
              margin: '0 0 10px', padding: '8px 10px',
              background: c.codeBg, borderRadius: 4,
              fontFamily: MONO_FONT, fontSize: 12, lineHeight: 1.5, color: c.sub,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              maxHeight: 340, overflowY: 'auto',
            }}>
              {approveArgs.map(([k, v]) => {
                const s = displayArgValue(v)
                return s.includes('\n') ? `${k} =\n${s}` : `${k} = ${s}`
              }).join('\n')}
            </pre>
          ) : null}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button type="primary" size="small" icon={<CaretRightOutlined />} onClick={approve}>
              Approve &amp; {v}
            </Button>
            <Button size="small" danger onClick={() => onAnswer?.(false)}>Deny</Button>
          </div>
        </div>
      )}

      {expanded && (
        <Fragment>
          {/* Why it ran this — the reasoning summary that led to the call. */}
          {entry.thinking && (
            <div style={{ borderTop: `1px solid ${c.toolBorder}`, padding: '6px 12px 8px' }}>
              <div style={{ fontSize: 11, color: c.dim, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4 }}>
                why it ran this{fmtThought(entry.thinkingMs) ? ` · thought for ${fmtThought(entry.thinkingMs)}` : ''}
              </div>
              <div data-testid="tool-thought" style={{ fontSize: 13, lineHeight: 1.55, color: textSecondary(c.isDark), whiteSpace: 'pre-wrap' }}>
                {entry.thinking}
              </div>
            </div>
          )}
          {/* Full command + args — the header line ellipsises, so this is the
              only place the complete (selectable) invocation is visible. Values
              render with their real newlines via displayArgValue. */}
          {args.length > 0 && (
            <div style={{ borderTop: `1px solid ${c.toolBorder}` }}>
              <div style={{ padding: '6px 12px 0', fontSize: 11, color: c.dim, textTransform: 'uppercase', letterSpacing: 0.4 }}>
                command
              </div>
              <pre style={{
                margin: 0, padding: '4px 12px 8px',
                fontFamily: MONO_FONT, fontSize: 13,
                color: c.sub, lineHeight: 1.5,
                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                maxHeight: 260, overflowY: 'auto',
              }}>
                {args.map(([k, val]) => {
                  const s = displayArgValue(val)
                  return s.includes('\n') ? `${k} =\n${s}` : `${k} = ${s}`
                }).join('\n')}
              </pre>
            </div>
          )}
          {entry.output !== undefined && (
            <pre style={{
              margin: 0, padding: '8px 12px',
              fontFamily: MONO_FONT, fontSize: 13,
              color: c.mono, lineHeight: 1.5,
              whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              background: c.codeBg,
              borderTop: args.length > 0 ? `1px solid ${c.toolBorder}` : undefined,
            }}>
              {entry.output || '(no output)'}
            </pre>
          )}
        </Fragment>
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
  liveThinking,
  replyThinking,
  pendingConfirm,
  onAnswerConfirm,
}: {
  segments: BlockSegment[]
  currentText: string
  done: boolean
  liveThinking?: string
  replyThinking?: { text: string; ms: number }
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
          (() => {
            const owns = confirmOwnsSegment(pendingConfirm, seg.entry)
            const pc = owns ? pendingConfirm! : null
            return (
              <ToolCallBlock
                key={i}
                entry={seg.entry}
                awaiting={pc ? pc.autonomy : null}
                resolved={pc && pc.display_name ? { name: pc.display_name, input: pc.display_input ?? {} } : null}
                verb={pc ? pc.confirm_verb : undefined}
                editable={!!pc && (pc.display_name ?? pc.tool_name) === pc.tool_name}
                onAnswer={onAnswerConfirm}
              />
            )
          })()
        )
      )}
      {(currentText || !done) && (
        <pre style={{
          margin: 0, padding: 0,
          fontFamily: MONO_FONT, fontSize: 14,
          color: c.mono, lineHeight: 1.6,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>
          {currentText}
          {!done && <span style={{ color: c.faint }}>▌</span>}
        </pre>
      )}
      {/* The thought streaming now — expanded while the model thinks, hidden
          once it starts writing, then collapsed into the step it led to. */}
      {!done && !currentText && liveThinking && <LiveThought text={liveThinking} />}
      {replyThinking?.text && <ReplyReasoning thought={replyThinking} />}
    </div>
  )
}

/** The reasoning summary streaming right now, shown expanded in the card. The
 *  body scrolls with the newest words pinned at the bottom (column-reverse). */
function LiveThought({ text }: { text: string }) {
  const c = useCardTheme()
  return (
    <div data-testid="live-thought" style={{
      margin: '6px 0', borderRadius: RADIUS.sm, border: `1px solid ${c.toolBorder}`, background: c.codeBg,
      padding: '8px 12px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: c.dim, marginBottom: 4 }}>
        <LoadingOutlined style={{ color: 'var(--rs-accent)', fontSize: 12 }} />
        <BulbOutlined style={{ fontSize: 12 }} />
        Thinking…
      </div>
      <div style={{ maxHeight: 132, overflowY: 'auto', display: 'flex', flexDirection: 'column-reverse' }}>
        <div style={{ fontSize: 13, lineHeight: 1.55, color: textSecondary(c.isDark), whiteSpace: 'pre-wrap' }}>
          {text}
        </div>
      </div>
    </div>
  )
}

/** The thought behind the final reply — one quiet collapsed line under it. */
function ReplyReasoning({ thought }: { thought: { text: string; ms: number } }) {
  const c = useCardTheme()
  const [open, setOpen] = useState(false)
  const secs = fmtThought(thought.ms)
  return (
    <div style={{ marginTop: 6 }}>
      <Button
        type="text" size="small" aria-expanded={open}
        icon={<BulbOutlined />}
        onClick={() => setOpen(o => !o)}
        style={{ color: c.dim, fontSize: 12, paddingInline: 6, marginLeft: -6 }}
      >
        Reasoning behind this reply{secs ? ` · ${secs}` : ''}
      </Button>
      {open && (
        <div data-testid="reply-thought" style={{
          marginTop: 4, borderRadius: RADIUS.sm, border: `1px solid ${c.toolBorder}`, background: c.codeBg,
          padding: '8px 12px', fontSize: 13, lineHeight: 1.55, color: textSecondary(c.isDark), whiteSpace: 'pre-wrap',
        }}>
          {thought.text}
        </div>
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

/** Brief holding view while a large completed result's parse + table build is
 *  deferred a frame, so the transition reads as "rendering…" instead of a frozen
 *  card (or thousands of raw lines flashing before the table replaces them). */
function RenderingPlaceholder({ chars, lines }: { chars: number; lines: number }) {
  const c = useCardTheme()
  return (
    <div style={{
      padding: '18px 14px', display: 'flex', alignItems: 'center', gap: 10,
      color: c.dim, fontFamily: MONO_FONT, fontSize: 13,
    }}>
      <LoadingOutlined style={{ color: 'var(--rs-accent)' }} />
      Rendering result… ({fmtBytes(chars)}{lines > 1 ? `, ${lines.toLocaleString()} lines` : ''})
    </div>
  )
}

// Memoised: a streamed token now re-creates only the streaming block's object
// (useInvocationBlocks' setBlocks keeps stable refs for untouched blocks), so with
// stable callbacks every OTHER card skips re-render. Without this, a long
// transcript re-rendered every card — each re-running its Markdown/parse — on
// every frame of a reply, the bulk of the streaming-time jank. The callbacks it
// receives are useCallback-stabilised (answerConfirm here; handleRerun/handleAskLlm/
// handleToggleContext in ConsoleView) and pendingConfirm is null for every card but
// the active one, so the shallow prop compare actually holds.
const BlockCard = memo(function BlockCard({
  block,
  onRerun,
  onAskLlm,
  onToggleContext,
  pendingConfirm,
  onAnswerConfirm,
}: {
  block: InvocationBlock
  onRerun?: (data: RerunData) => void
  onAskLlm?: (text: string) => void
  onToggleContext?: (id: string, inContext: boolean) => void
  pendingConfirm?: PendingConfirm | null
  onAnswerConfirm?: (requestId: string, approved: boolean, editedInput?: Record<string, unknown>) => void
}) {
  const c = useCardTheme()
  const [collapsed, setCollapsed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [viewRaw, setViewRaw] = useState(false)

  // Bind this card's gate to its own request_id, so the shared resolver knows
  // which parked run to answer. Passed to both the inline segment gate (agent
  // tool calls) and the card-level gate (rule runs / any gate with no segment).
  const answerThis = useCallback((approved: boolean, editedInput?: Record<string, unknown>) => {
    if (pendingConfirm) onAnswerConfirm?.(pendingConfirm.request_id, approved, editedInput)
  }, [pendingConfirm, onAnswerConfirm])
  // Does a running tool segment already own this confirm? Then the inline gate
  // inside ChatTranscript renders it (matched by tool_name) — don't also draw the
  // card-level gate, or the run would show two Approve/Deny panels.
  const segmentMatch = !!pendingConfirm && block.type === 'chat'
    && block.segments.some(s => s.kind === 'tool' && confirmOwnsSegment(pendingConfirm, s.entry))
  // A card-level gate is needed when a confirm is parked for this block but no
  // running tool segment claims it — the rule-mode trigger case (a runnable run,
  // not an agent tool call), and the cold-start seed case (a gate restored before
  // its tool segment streamed).
  const cardGate = pendingConfirm && !segmentMatch ? pendingConfirm : null

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
  // A subcommand runnable's intent lives in its verb path, not in --flag args, so
  // a verb-only run (e.g. `service restart`) has no args. Offer Edit-&-rerun when
  // there are args OR a command path, else the pencil never shows for subcommands.
  const canEditInForm = hasArgs || (block.rerunData?.commandPath?.length ?? 0) > 0

  // Total output size — the heaviness proxy for the deferred render. Only summed
  // once the block is done (short-circuits while streaming, so no per-frame O(n)).
  const outputChars = useMemo(
    () => (block.type === 'run' && block.done)
      ? block.lines.reduce((n, l) => n + l.line.length, 0) : 0,
    [block.type, block.done, block.lines],
  )
  const heavy = outputChars >= HEAVY_RENDER_CHARS

  // Deferred render for a heavy result: on completion, paint a "rendering…"
  // placeholder first, then flip ready two frames later so the placeholder
  // actually paints before the synchronous parse/table build runs. A light
  // result is ready immediately (renderReady stays true — no placeholder flash).
  const [deferredReady, setDeferredReady] = useState(false)
  useEffect(() => {
    if (!heavy) return
    setDeferredReady(false)
    let raf2 = 0
    const raf1 = requestAnimationFrame(() => { raf2 = requestAnimationFrame(() => setDeferredReady(true)) })
    return () => { cancelAnimationFrame(raf1); if (raf2) cancelAnimationFrame(raf2) }
  }, [heavy])
  const renderReady = !heavy || deferredReady

  // Smart output parsing — only for completed run blocks, and (when heavy) only
  // once the deferred tick has painted the placeholder. Memoized: the block's
  // `lines` array is stable once done, so a 520 KB JSON.parse runs once rather
  // than on every re-render (scroll, theme, parent update).
  const parsed = useMemo(
    () => (block.type === 'run' && block.done && renderReady) ? parseOutput(block.lines) : null,
    [block.type, block.done, renderReady, block.lines],
  )
  const deferredPending = block.type === 'run' && block.done && !renderReady

  // Raw-view line windowing:
  // - streaming: render only a live tail (STREAM_MAX_LINES) so a runnable dumping
  //   thousands of lines can't grow the <pre> to an O(n²) reconciliation hang.
  // - completed: show the first TRUNCATE_AT with a "N more — click to expand".
  const total = block.lines.length
  const streaming = block.type === 'run' && !block.done
  const streamCapped = streaming && total > STREAM_MAX_LINES
  const extraLines = total - TRUNCATE_AT
  const shouldTruncate = block.type === 'run' && block.done && !showAll && extraLines > 0
  const displayLines = streamCapped
    ? block.lines.slice(total - STREAM_MAX_LINES)
    : shouldTruncate
      ? block.lines.slice(0, TRUNCATE_AT)
      : block.lines

  const statusIcon = !block.done
    ? <LoadingOutlined style={{ color: 'var(--rs-accent)' }} />
    : block.stopped
      ? <PauseCircleOutlined style={{ color: '#8c8c8c' }} />
      : block.exitCode === 0
        ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
        : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />

  // Edit-and-rerun opens the Forms run dialog pre-filled with this run's values
  // (rich typed inputs), rather than an inline plain-text arg editor. App owns the
  // discovered-runnable lookup and form open.
  const editInForm = () => window.dispatchEvent(new CustomEvent('runspec:edit_in_form', { detail: block.rerunData }))

  // The plain-text transcript, built ON DEMAND (Copy / Ask). For a chat card this
  // interleaves the assistant's prose with each tool call + its output — an
  // O(transcript) string build that concatenates every tool output. Computing it
  // in the render body reran it for every card on every streamed token (and grew
  // O(n²) over the streaming card), which is what made the panel — and, via
  // main-thread contention, typing into the composer — lag while a reply rendered.
  // It's only needed when a handler fires, so build it there.
  const buildLogText = () =>
    block.type === 'chat'
      ? transcriptToText(block.segments, block.currentText)
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
      text = `Output from ${block.label}\n\n${buildLogText()}`
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
    const raw = parsed ? parsed.rawText : buildLogText()
    onAskLlm?.(`Output from ${block.label}\n\n${raw}`)
  }

  const hasContent = block.type === 'chat'
    ? (block.segments.length > 0 || block.currentText.length > 0)
    : block.lines.length > 0

  // Table/Raw toggle — only for a finished run with structured output.
  const showViewToggle = block.type === 'run' && block.done && !deferredPending
    && !!parsed && parsed.kind !== 'raw'

  const moreItems = cardMoreItems(block, { canRerun: !!onRerun, canEditInForm })
  const onMoreClick = (key: string) => {
    if (key === 'copy-prompt') copyToClipboard(block.label)
    else if (key === 'edit-resend') loadToComposer()
    else if (key === 'resend') resend()
    else if (key === 'edit-form') editInForm()
    else if (key === 'rerun' && block.rerunData) onRerun?.(block.rerunData)
  }

  return (
    <div style={{ background: c.cardBg, border: `1px solid ${c.cardBorder}`, borderRadius: RADIUS.md, overflow: 'hidden' }}>
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
          style={{ color: c.caret, cursor: 'pointer', fontSize: 12, flexShrink: 0, width: 14 }}
        >
          {collapsed
            ? <RightOutlined style={{ fontSize: 11 }} />
            : <DownOutlined style={{ fontSize: 11 }} />}
        </span>

        {/* "In context" toggle — whether this card's content is part of the
            agent's context. Only on completed cards (an in-flight turn has no
            meaningful toggle). Chat cards default in-context; user-run cards
            default out (opt in to stage the result into the next prompt).
            Retired once the exchange has been compacted away. */}
        {block.done && onToggleContext && !block.operatorOnly && (
          <Tooltip
            title={
              block.compacted
                ? 'Compacted — this exchange was condensed into a summary and can no longer be toggled'
                : block.type === 'chat'
                  ? 'In the assistant’s context — untick to drop this exchange from what the agent sees'
                  : 'Out of the assistant’s context — tick to include this result with your next message'
            }
          >
            <Checkbox
              checked={block.inContext ?? (block.type === 'chat')}
              disabled={block.compacted}
              onClick={e => e.stopPropagation()}
              onChange={e => onToggleContext(block.id, e.target.checked)}
              style={{ flexShrink: 0 }}
            />
          </Tooltip>
        )}

        {statusIcon}
        <Text
          style={{ color: c.text, fontFamily: MONO_FONT, fontSize: 14, cursor: 'pointer', flex: 1 }}
          onClick={() => setCollapsed(v => !v)}
        >
          {block.label}
        </Text>
        {block.operatorOnly && (
          <Tooltip title="A run you launched yourself — saved for your review, never part of the assistant’s context">
            <Tag color="default" style={{ fontSize: 11, marginLeft: 4 }}>operator run · not sent to agent</Tag>
          </Tooltip>
        )}
        {block.trigger && (
          <Tooltip title={`Fired automatically by the “${block.trigger.name}” trigger (${block.trigger.source})`}>
            <Tag color="gold" style={{ fontSize: 11, marginLeft: 4 }}>trigger: {block.trigger.name}</Tag>
          </Tooltip>
        )}
        <Text style={{ color: c.dim, fontSize: 12 }}>
          {new Date(block.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </Text>
        {/* Live elapsed clock while the turn runs (chat turns have no InFlightStrip row). */}
        {!block.done && (
          <Text style={{ color: 'var(--rs-accent)', fontSize: 12, fontFamily: MONO_FONT }}>{fmtElapsed(block.startedAt)}</Text>
        )}
        {toolCount > 0 && (
          <Tooltip title={`${toolCount} tool call${toolCount !== 1 ? 's' : ''} this turn`}>
            <Tag style={{ fontSize: 12, marginLeft: 4 }}><ApiOutlined style={{ fontSize: 11 }} /> {toolCount}</Tag>
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
        {block.type === 'run' && !block.done && (
          <Tooltip title="Stop — cancel this run">
            <Button
              size="small" danger icon={<StopOutlined />}
              style={{ marginLeft: 4 }}
              onClick={(e) => { e.stopPropagation(); bridge.cancel_invocation(block.id) }}
            >
              Stop
            </Button>
          </Tooltip>
        )}
        {block.done && block.durationMs !== undefined && (
          <Tag
            style={{ marginLeft: 4, fontSize: 12 }}
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
                <Tag style={{ fontSize: 12, fontFamily: MONO_FONT, cursor: 'default', marginLeft: 4 }}>
                  ↑{promptTotal.toLocaleString()} ↓{output.toLocaleString()}
                </Tag>
              </Tooltip>
              {cached > 0 && (
                <Tooltip title={
                  `${cacheRead.toLocaleString()} tokens served from cache at ~10% cost` +
                  (cacheCreation > 0 ? ` · ${cacheCreation.toLocaleString()} written to cache this turn` : '')
                }>
                  <Tag color="green" style={{ fontSize: 12, fontFamily: MONO_FONT, cursor: 'default', marginLeft: 4 }}>
                    ⚡{hitPct}% cached
                  </Tag>
                </Tooltip>
              )}
              {cost !== null && (
                <Tooltip title={`Estimated cost at approximate list prices${block.model ? ` · ${block.model}` : ''}. Rates drift — treat as a guide.`}>
                  <Tag style={{ fontSize: 12, fontFamily: MONO_FONT, cursor: 'default', marginLeft: 4 }}>
                    ~{fmtUsd(cost)}
                  </Tag>
                </Tooltip>
              )}
            </>
          )
        })()}
        {/* Card actions: Copy output + Ask the assistant stay one click away;
            the less-used prompt / rerun actions live in the ⋯ menu so the
            header doesn't grow a row of look-alike icons. */}
        {hasContent && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 2, marginLeft: 2, flexShrink: 0 }}>
            <Tooltip title="Copy output to clipboard">
              <Button
                type="text" size="small" aria-label="Copy output to clipboard" icon={<CopyOutlined />}
                onClick={(e) => { e.stopPropagation(); handleCopy() }}
                style={{ color: c.dim, height: 24, width: 26 }}
              />
            </Tooltip>
            {onAskLlm && (
              <Tooltip title="Ask the assistant about this">
                <Button
                  type="text" size="small" aria-label="Ask the assistant about this" icon={<RobotOutlined />}
                  onClick={(e) => { e.stopPropagation(); handleAskLlm() }}
                  style={{ color: c.dim, height: 24, width: 26 }}
                />
              </Tooltip>
            )}
            {moreItems.length > 0 && (
              <Dropdown
                menu={{ items: moreItems, onClick: ({ key, domEvent }) => { domEvent.stopPropagation(); onMoreClick(key) } }}
                trigger={['click']}
                placement="bottomRight"
              >
                <Button
                  type="text" size="small" aria-label="More actions" icon={<EllipsisOutlined />}
                  onClick={(e) => e.stopPropagation()}
                  style={{ color: c.dim, height: 24, width: 26 }}
                />
              </Dropdown>
            )}
          </span>
        )}
      </div>

      {/* Body — hidden when collapsed */}
      {!collapsed && (
        <Fragment>
          {/* Card-level autonomy gate: a confirm parked for this run with no
              running tool segment to host it (a rule-mode trigger runnable, or a
              gate restored on cold start). Reuses the same Approve/Deny +
              editable-args panel as the inline agent-tool gate by rendering a
              ToolCallBlock in a synthetic awaiting state. */}
          {cardGate && (
            <div style={{ padding: '10px 14px 0' }}>
              <ToolCallBlock
                entry={{ name: cardGate.tool_name, input: cardGate.tool_input, running: false }}
                awaiting={cardGate.autonomy}
                resolved={cardGate.display_name ? { name: cardGate.display_name, input: cardGate.display_input ?? {} } : null}
                verb={cardGate.confirm_verb}
                editable={(cardGate.display_name ?? cardGate.tool_name) === cardGate.tool_name}
                onAnswer={answerThis}
              />
            </div>
          )}
          {/* Output body */}
          <div style={{ position: 'relative' }}>
            {/* Card toolbar — a single right-aligned strip so the Copy/Ask
                controls never overlap the content or crowd the view toggle (the
                old layout floated them absolutely and reserved a magic padding).
                The Table/Raw toggle (structured run output only) sits here too,
                separated from Copy/Ask so the two groups read as distinct. */}
            {showViewToggle && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 4, padding: '4px 8px' }}>
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

            {/* run block: smart JSON table/object or flat log lines */}
            {block.type === 'run' && deferredPending && (
              <RenderingPlaceholder chars={outputChars} lines={total} />
            )}
            {block.type === 'run' && !deferredPending && (
              <Fragment>
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
                  <Fragment>
                    {streamCapped && (
                      <div style={{ padding: '6px 14px 0', fontSize: 12, color: c.dim, fontFamily: MONO_FONT }}>
                        streaming — showing the last {STREAM_MAX_LINES.toLocaleString()} of {total.toLocaleString()} lines
                      </div>
                    )}
                    <pre style={{
                      margin: 0, padding: '10px 14px',
                      fontFamily: MONO_FONT, fontSize: 14,
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
                  </Fragment>
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
                        fontSize: 13, color: c.dim, cursor: 'pointer',
                        fontFamily: MONO_FONT,
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
                liveThinking={block.liveThinking}
                replyThinking={block.replyThinking}
                pendingConfirm={pendingConfirm}
                onAnswerConfirm={answerThis}
              />
            )}
          </div>
        </Fragment>
      )}
    </div>
  )
})

export function useInvocationBlocks() {
  const [blocks, setBlocks] = useState<InvocationBlock[]>([])

  useEffect(() => {
    // Batch runspec:output line events. A runnable that dumps thousands of lines
    // would otherwise fire one setBlocks per line — each `[...b.lines, line]` is
    // an O(n) copy, so streaming is O(n²) and React re-renders the growing <pre>
    // every line, hanging the tab for minutes. We coalesce incoming lines into a
    // per-block buffer and flush them in a single setBlocks once per frame.
    const pending = new Map<string, OutputLine[]>()
    // Streamed chat tokens are coalesced the same way as output lines: one
    // setBlocks per animation frame, not per token. A fast reply arrives dozens
    // of tokens/sec, and each un-batched setBlocks re-rendered the whole
    // transcript synchronously — starving the main thread so keystrokes in the
    // composer (a sibling subtree) couldn't paint until the render committed.
    const pendingTokens = new Map<string, string>()
    const pendingThoughts = new Map<string, string>()
    let raf: number | null = null

    const flush = () => {
      raf = null
      if (pending.size === 0 && pendingTokens.size === 0 && pendingThoughts.size === 0) return
      const drained = new Map(pending)
      const drainedTokens = new Map(pendingTokens)
      const drainedThoughts = new Map(pendingThoughts)
      pending.clear()
      pendingTokens.clear()
      pendingThoughts.clear()
      // Lines and tokens for the same frame commit in ONE setBlocks so a card
      // that received both re-renders once, not twice.
      setBlocks(prev => prev.map(b => {
        let nb = b
        const add = drained.get(b.id)
        if (add && add.length) nb = { ...nb, lines: [...nb.lines, ...add] }
        const thought = drainedThoughts.get(b.id)
        if (thought && nb.type === 'chat') {
          nb = { ...nb, liveThinking: (nb.liveThinking ?? '') + thought }
        }
        const tok = drainedTokens.get(b.id)
        if (tok) {
          if (nb.type === 'chat') {
            nb = { ...nb, currentText: nb.currentText + tok }
          } else {
            // run block receiving tokens (unusual but handle gracefully)
            const last = nb.lines[nb.lines.length - 1]
            nb = last
              ? { ...nb, lines: [...nb.lines.slice(0, -1), { ...last, line: last.line + tok }] }
              : { ...nb, lines: [{ id: b.id, line: tok, stream: 'stdout' }] }
          }
        }
        return nb
      }))
    }

    const scheduleFlush = () => {
      if (raf === null) raf = requestAnimationFrame(flush)
    }

    const onOutput = (e: Event) => {
      const { id, line, stream } = (e as CustomEvent).detail as OutputLine
      const buf = pending.get(id)
      if (buf) buf.push({ id, line, stream })
      else pending.set(id, [{ id, line, stream }])
      scheduleFlush()
    }

    const onToken = (e: Event) => {
      const { id, token } = (e as CustomEvent).detail as { id: string; token: string }
      pendingTokens.set(id, (pendingTokens.get(id) ?? '') + token)
      scheduleFlush()
    }

    // A reasoning summary streaming in — batched with tokens, shown expanded.
    const onThinking = (e: Event) => {
      const { id, text } = (e as CustomEvent).detail as { id: string; text: string }
      if (!text) return
      pendingThoughts.set(id, (pendingThoughts.get(id) ?? '') + text)
      scheduleFlush()
    }

    // The thought behind the final reply: collapse the live box into the
    // reply's reasoning line.
    const onReplyThinking = (e: Event) => {
      flush()
      const { id, text, ms } = (e as CustomEvent).detail as { id: string; text: string; ms?: number }
      setBlocks(prev => prev.map(b =>
        b.id === id ? { ...b, liveThinking: undefined, ...(text ? { replyThinking: { text, ms: ms ?? 0 } } : {}) } : b))
    }

    const onToolStart = (e: Event) => {
      // Drain any buffered tokens into currentText FIRST, so the text that
      // streamed right before this tool call is committed as the pre-tool text
      // segment — not left in the token buffer to be flushed into currentText
      // *after* the tool segment, which split a lead-in sentence around the card
      // (the "…first half / card / …second half" bug). Same ordering fix onEnd
      // already applies; both flush() and the setBlocks below are functional
      // updaters, so flush's drain runs before this reads b.currentText.
      flush()
      const { id, call_id, tool_name, tool_input, thinking, thinking_ms } = (e as CustomEvent).detail as {
        id: string; call_id?: string; tool_name: string; tool_input: Record<string, unknown>
        thinking?: string; thinking_ms?: number
      }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        const newSegs: BlockSegment[] = [
          ...b.segments,
          ...(b.currentText ? [{ kind: 'text' as const, text: b.currentText }] : []),
          { kind: 'tool' as const, entry: {
            name: tool_name, ...(call_id ? { callId: call_id } : {}), input: tool_input, running: true, startedAt: Date.now(),
            ...(thinking ? { thinking, thinkingMs: thinking_ms ?? 0 } : {}),
          } },
        ]
        // The step the thought led to has started: the live box collapses
        // into this row's bulb.
        return { ...b, segments: newSegs, currentText: '', liveThinking: undefined }
      }))
    }

    const onToolEnd = (e: Event) => {
      const detail = (e as CustomEvent).detail as ToolEndDetail & { id: string }
      setBlocks(prev => prev.map(b => {
        if (b.id !== detail.id) return b
        return { ...b, segments: finishToolSegment(b.segments, detail) }
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
      // Drain any buffered output/tokens first so the block is marked done with its
      // full line set + final currentText — otherwise parseOutput would run once on
      // a short set, then again when the trailing batch flushes (a visible flash of
      // the raw view), and trailing tokens could be lost from the closed turn.
      flush()
      const { id, exit_code, duration_ms, stopped } = (e as CustomEvent).detail as { id: string; exit_code: number; duration_ms: number; stopped?: boolean }
      setBlocks(prev => prev.map(b => {
        if (b.id !== id) return b
        if (b.type === 'chat' && b.currentText) {
          return {
            ...b,
            segments: [...b.segments, { kind: 'text', text: b.currentText }],
            currentText: '', liveThinking: undefined,
            done: true, exitCode: exit_code, durationMs: duration_ms, stopped: !!stopped,
          }
        }
        return { ...b, liveThinking: undefined, done: true, exitCode: exit_code, durationMs: duration_ms, stopped: !!stopped }
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

    // Compaction (or an excluded-turn drop) removed some chat turns from history.
    // Their cards can no longer be re-included — mark them compacted so the
    // header checkbox is retired (disabled) rather than a dead toggle.
    const onCompacted = (e: Event) => {
      const { dropped_turns } = (e as CustomEvent).detail as { dropped_turns?: string[] }
      const gone = new Set(dropped_turns ?? [])
      if (gone.size === 0) return
      setBlocks(prev => prev.map(b =>
        gone.has(b.id) ? { ...b, compacted: true, inContext: false } : b
      ))
    }

    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:thinking', onThinking)
    window.addEventListener('runspec:reply_thinking', onReplyThinking)
    window.addEventListener('runspec:tool_start', onToolStart)
    window.addEventListener('runspec:tool_end', onToolEnd)
    window.addEventListener('runspec:tool_progress', onToolProgress)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:chat_usage', onChatUsage)
    window.addEventListener('runspec:chat_compacted', onCompacted)
    return () => {
      if (raf !== null) cancelAnimationFrame(raf)
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:thinking', onThinking)
      window.removeEventListener('runspec:reply_thinking', onReplyThinking)
      window.removeEventListener('runspec:tool_start', onToolStart)
      window.removeEventListener('runspec:tool_end', onToolEnd)
      window.removeEventListener('runspec:tool_progress', onToolProgress)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:chat_usage', onChatUsage)
      window.removeEventListener('runspec:chat_compacted', onCompacted)
    }
  }, [])

  // These are useCallback-stabilised (setBlocks is the only closure, and it's a
  // stable useState setter) so a consumer can depend on them without its own
  // handlers churning every render — which is what lets ConsoleView hand
  // referentially-stable callbacks to the memoised BlockCard.
  const addBlock = useCallback((block: InvocationBlock) => setBlocks(prev => [...prev, block]), [])
  const clearBlocks = useCallback(() => setBlocks([]), [])
  // Idempotent create keyed by id: append only if no block with that id exists.
  // Used for trigger fires — several runspec:trigger_card events arrive per card
  // (TRIGGERED → RUNNING → DONE) and only the first should create the block; the
  // rest reconcile via updateBlock. `make` is called lazily so the block is only
  // built on the creating event.
  const upsertBlock = useCallback((id: string, make: () => InvocationBlock) =>
    setBlocks(prev => (prev.some(b => b.id === id) ? prev : [...prev, make()])), [])
  // Patch a single block by id (a partial or a functional updater). No-op when
  // the id is absent, so a late card update for an evicted block is dropped
  // cleanly.
  const updateBlock = useCallback(
    (id: string, patch: Partial<InvocationBlock> | ((b: InvocationBlock) => InvocationBlock)) =>
      setBlocks(prev => prev.map(b =>
        b.id === id ? (typeof patch === 'function' ? patch(b) : { ...b, ...patch }) : b,
      )), [])
  // Replace a session's transcript (`sessionId` given: only that session's
  // blocks are swapped, other sessions untouched) or the whole transcript —
  // used to render a reopened saved conversation.
  const hydrateBlocks = useCallback((next: InvocationBlock[], sessionId?: string) =>
    setBlocks(prev => (sessionId ? [...prev.filter(b => b.sessionId !== sessionId), ...next] : next)), [])
  // Drop every block of a closed / evicted session.
  const removeSession = useCallback((sessionId: string) =>
    setBlocks(prev => (prev.some(b => b.sessionId === sessionId) ? prev.filter(b => b.sessionId !== sessionId) : prev)), [])
  // Toggle a card's "in context" flag (the header checkbox). Local state only;
  // the actual context effect is applied by the caller (exclude → bridge call,
  // include → staged into the next chat send).
  const setBlockInContext = useCallback((id: string, inContext: boolean) =>
    setBlocks(prev => prev.map(b => (b.id === id ? { ...b, inContext } : b))), [])

  return { blocks, addBlock, upsertBlock, updateBlock, clearBlocks, hydrateBlocks, removeSession, setBlockInContext }
}
