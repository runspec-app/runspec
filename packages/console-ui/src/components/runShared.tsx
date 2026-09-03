import { Button, Tooltip, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'

// Shared run-inbox primitives used by both the Triggers and Schedules tabs (their
// run inbox + detail drawer + event log). Kept in one module so the two views
// render runs identically.

// Run lifecycle statuses, in order. The engine drives triggered→…→done/failed; a
// scheduled direct run starts at `running`. awaiting_human is overlaid
// client-side from a card-scoped tool_confirm (triggers only).
export const COLUMNS: { key: string; label: string }[] = [
  { key: 'triggered', label: 'Triggered' },
  { key: 'awaiting_claim', label: 'Awaiting claim' },
  { key: 'triage', label: 'Triage' },
  { key: 'awaiting_human', label: 'Awaiting human' },
  { key: 'running', label: 'Running' },
  { key: 'done', label: 'Done' },
  { key: 'failed', label: 'Failed' },
]

export const COLUMN_COLOR: Record<string, string> = {
  triggered: '#8c8c8c',
  // A shared-mailbox email parked until an operator claims it (purple, distinct
  // from the orange confirm-gate awaiting_human).
  awaiting_claim: '#722ed1',
  triage: '#1677ff',
  awaiting_human: '#fa8c16',
  running: '#13c2c2',
  done: '#52c41a',
  failed: '#ff4d4f',
}

const STATUS_LABEL: Record<string, string> = Object.fromEntries(COLUMNS.map(c => [c.key, c.label]))

// A status counts as "in flight" (abortable) until a terminal done/failed.
export const RUNNING_STATUSES = new Set(['triggered', 'triage', 'running'])

export function StatusBadge({ status }: { status: string }) {
  const color = COLUMN_COLOR[status] ?? '#8c8c8c'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color, flexShrink: 0, width: 110 }}>
      <span style={{ width: 8, height: 8, borderRadius: 8, background: color }} />
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

// Right-click copy is suppressed outside webview debug mode, so offer an explicit
// Copy control. Selectable text already supports Ctrl+C.
export function CopyButton({ text, title = 'Copy' }: { text: string; title?: string }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      message.success('Copied')
    } catch {
      message.error('Copy failed')
    }
  }
  return (
    <Tooltip title={title}>
      <Button size="small" type="text" icon={<CopyOutlined />} onClick={e => { e.stopPropagation(); copy() }} />
    </Tooltip>
  )
}

export function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return ''
  return new Date(epochSeconds * 1000).toLocaleTimeString()
}
