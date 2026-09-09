import { Collapse } from 'antd'
import { BellOutlined } from '@ant-design/icons'
import { type TriggerEvent } from '../bridge'
import { useIsDark, textMuted } from '../ThemeContext'
import { COLUMN_COLOR, fmtTime } from './runShared'

// The event-audit feed: every event seen + its disposition (fired / skipped,
// with the reason — screen locked / working hours / already handled — / no
// match), newest first. Shared by the Triggers and Schedules tabs — both record
// events in the same shape (TriggerEvent), so the one widget renders both feeds.

// Build the human-readable disposition for one event. Skips are grouped by their
// reason (skip_reasons; older records without it fall back to "working hours",
// which was the only skip reason before lock auto-suspend), and once_per_thread
// repeats (deduped) read as "already handled".
export function disposition(ev: TriggerEvent, dim: string): { color: string; text: string } {
  if (ev.matched.length > 0) {
    return { color: COLUMN_COLOR.done, text: `fired: ${ev.matched.join(', ')}` }
  }
  const byReason: Record<string, string[]> = {}
  for (const name of ev.skipped) {
    const reason = ev.skip_reasons?.[name] || 'working hours'
    ;(byReason[reason] ||= []).push(name)
  }
  const groups = Object.entries(byReason).map(
    ([reason, names]) => `skipped (${reason}): ${names.join(', ')}`,
  )
  if (ev.deduped && ev.deduped.length > 0) {
    groups.push(`skipped (already handled): ${ev.deduped.join(', ')}`)
  }
  if (groups.length > 0) {
    return { color: COLUMN_COLOR.awaiting_human, text: groups.join('  ·  ') }
  }
  return { color: dim, text: 'no match' }
}

export function EventLog({
  events,
  label = 'Event log',
  hint = 'every event seen, matched or not',
}: {
  events: TriggerEvent[]
  label?: string
  hint?: string
}) {
  const isDark = useIsDark()
  const dim = textMuted(isDark)
  const border = isDark ? '#303030' : '#e8e8e8'

  return (
    <Collapse
      size="small"
      defaultActiveKey={['events']}
      style={{ marginTop: 20 }}
      items={[{
        key: 'events',
        label: (
          <span><BellOutlined /> {label}<span style={{ color: dim, fontSize: 12 }}> · {hint}</span></span>
        ),
        children: events.length === 0 ? (
          <div style={{ color: dim, fontSize: 12 }}>No events yet.</div>
        ) : (
          <div style={{ maxHeight: 360, overflowY: 'auto', border: `1px solid ${border}`, borderRadius: 6 }}>
            {events.map((ev, i) => {
              const disp = disposition(ev, dim)
              return (
                <div
                  key={ev.id || i}
                  style={{
                    display: 'flex', gap: 8, alignItems: 'baseline', padding: '4px 8px',
                    borderBottom: i < events.length - 1 ? `1px solid ${border}` : undefined,
                    fontSize: 12,
                  }}
                >
                  <span style={{ color: dim, fontFamily: 'monospace', flexShrink: 0 }}>{fmtTime(ev.ts)}</span>
                  <span style={{ color: dim, flexShrink: 0 }}>{ev.source}:{ev.type}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ev.summary}</span>
                  <span style={{ color: disp.color, flexShrink: 0 }}>{disp.text}</span>
                </div>
              )
            })}
          </div>
        ),
      }]}
    />
  )
}
