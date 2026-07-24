import { Collapse } from 'antd'
import { BellOutlined } from '@ant-design/icons'
import { type TriggerEvent } from '../bridge'
import { useIsDark, textMuted } from '../ThemeContext'
import { COLUMN_COLOR, fmtTime } from './runShared'

// The event-audit feed: every event seen + its disposition (fired / skipped by
// the working-hours rota / no match), newest first. Shared by the Triggers and
// Schedules tabs — both record events in the same shape (TriggerEvent), so the
// one widget renders both feeds.
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
          <div style={{ maxHeight: 260, overflowY: 'auto', border: `1px solid ${border}`, borderRadius: 6 }}>
            {events.map((ev, i) => {
              const disposition =
                ev.matched.length > 0
                  ? { color: COLUMN_COLOR.done, text: `fired: ${ev.matched.join(', ')}` }
                  : ev.skipped.length > 0
                    ? { color: COLUMN_COLOR.awaiting_human, text: `skipped (working hours): ${ev.skipped.join(', ')}` }
                    : { color: dim, text: 'no match' }
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
                  <span style={{ color: disposition.color, flexShrink: 0 }}>{disposition.text}</span>
                </div>
              )
            })}
          </div>
        ),
      }]}
    />
  )
}
