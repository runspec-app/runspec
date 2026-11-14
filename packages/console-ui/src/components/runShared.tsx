import { Button, Tooltip, message } from 'antd'
import { CopyOutlined, LoadingOutlined } from '@ant-design/icons'
import { RADIUS, useAccent, useIsDark } from '../ThemeContext'
import { statusTone, type StatusTone } from './kit'
import { fmtClock } from '../when'
import type { SavedRun } from '../bridge'

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

// The status word in the shared vocabulary, so it reads at 4.5:1 in both
// themes; the dot keeps the column's fill colour (a mark needs only 3:1).
const COLUMN_TONE: Record<string, StatusTone> = {
  triggered: 'neutral', awaiting_claim: 'info', triage: 'running', awaiting_human: 'waiting',
  running: 'running', done: 'ok', failed: 'failed',
}

/** A run's status: in flight it spins in the neutral ink (motion, not colour);
 *  otherwise a dot in the status tone's own colour, so the dot reads like its
 *  word (the fixed cyan and green dots were 1.7:1 on light). */
export function StatusBadge({ status }: { status: string }) {
  const text = statusTone(COLUMN_TONE[status] ?? 'neutral', useIsDark(), useAccent()).color
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: text, flexShrink: 0, width: 110 }}>
      {RUNNING_STATUSES.has(status)
        ? <LoadingOutlined aria-hidden spin style={{ fontSize: 10 }} />
        : <span aria-hidden style={{ width: 8, height: 8, borderRadius: RADIUS.md, background: text }} />}
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
      <Button aria-label={title} size="small" type="text" icon={<CopyOutlined />} onClick={e => { e.stopPropagation(); copy() }} />
    </Tooltip>
  )
}

export function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return ''
  return fmtClock(epochSeconds * 1000)
}

/** Where a saved run goes, as the confirmation lists it: its host, its group,
 *  its picked hosts, its failover candidates or its profile. Pure. */
export function savedRunTargets(s: Pick<SavedRun, 'host' | 'group' | 'hosts' | 'selected_hosts' | 'profile'>): string[] {
  if (s.host) return [s.host]
  if (s.group) return [`group ${s.group}`]
  if (s.selected_hosts?.length) return s.selected_hosts
  if (s.hosts?.length) return [`first connected of ${s.hosts.join(', ')}`]
  if (s.profile) return [`profile ${s.profile}`]
  return []
}
