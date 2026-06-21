import type { ReactNode } from 'react'
import { Button, Drawer, Empty, message } from 'antd'
import { StopOutlined } from '@ant-design/icons'
import { type RunCard } from '../bridge'
import { useIsDark, textColor } from '../ThemeContext'
import { Markdown } from './Markdown'
import { StatusBadge, CopyButton, fmtTime, COLUMN_COLOR, RUNNING_STATUSES } from './runShared'

// The run inbox: a newest-first list of run cards + a detail drawer (status,
// agent/run output, timeline, Stop). Shared by the Triggers and Schedules tabs;
// the trigger-only bits (approve/deny gate, portal request, draft reply) are
// injected by the Triggers wrapper via renderDrawerExtra, and the per-row Review
// button via renderRowExtra. Schedules pass neither — a scheduled run has no
// agent and no gate.
export interface RunInboxProps {
  cards: Record<string, RunCard>
  selected: string | null
  onSelect: (cardId: string | null) => void
  // Stop an in-flight run (Triggers → cancel_chat; Schedules → cancel_invocation).
  onStop?: (cardId: string) => void
  // Per-card status override (Triggers overlay awaiting_human from a confirm gate;
  // Schedules just use the card's column).
  statusOf?: (card: RunCard) => string
  // Live in-progress body keyed by card_id (Triggers: agent tokens; Schedules:
  // streamed output). The finalised body is read from card.detail.result.
  streams?: Record<string, string>
  emptyText?: string
  renderRowExtra?: (card: RunCard) => ReactNode
  renderDrawerExtra?: (card: RunCard) => ReactNode
}

export function RunInbox({
  cards,
  selected,
  onSelect,
  onStop,
  statusOf,
  streams = {},
  emptyText = 'No runs yet.',
  renderRowExtra,
  renderDrawerExtra,
}: RunInboxProps) {
  const isDark = useIsDark()
  const dim = isDark ? '#888' : '#999'
  const colBg = isDark ? '#141414' : '#f0f0f0'
  const border = isDark ? '#303030' : '#e8e8e8'

  const status = (card: RunCard) => (statusOf ? statusOf(card) : card.column)
  const runs = Object.values(cards).sort((a, b) => b.created_at - a.created_at)
  const selectedCard = selected ? cards[selected] : undefined

  return (
    <div style={{ color: textColor(isDark) }}>
      {runs.length === 0 ? (
        <Empty description={<span style={{ color: dim }}>{emptyText}</span>} />
      ) : (
        <div style={{ border: `1px solid ${border}`, borderRadius: 6 }}>
          {runs.map((card, i) => (
            <div
              key={card.card_id}
              onClick={() => onSelect(card.card_id)}
              style={{
                display: 'flex', gap: 8, alignItems: 'center', padding: '8px 10px',
                borderBottom: i < runs.length - 1 ? `1px solid ${border}` : undefined,
                cursor: 'pointer', fontSize: 12,
                background: selected === card.card_id ? colBg : undefined,
              }}
            >
              <StatusBadge status={status(card)} />
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}>
                {card.summary}
              </span>
              <span style={{ color: dim, flexShrink: 0 }}>{card.trigger}</span>
              {renderRowExtra?.(card)}
              <span style={{ color: dim, fontFamily: 'monospace', flexShrink: 0 }}>{fmtTime(card.created_at)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Run detail — full event, output, timeline, plus any injected sections */}
      <Drawer
        open={!!selectedCard}
        onClose={() => onSelect(null)}
        width="min(820px, 92vw)"
        title={selectedCard?.summary}
      >
        {selectedCard && (() => {
          const card = selectedCard
          const result = card.detail?.result as string | undefined
          const error = card.detail?.error as string | undefined
          const body = result || streams[card.card_id]
          return (
            <div style={{ fontSize: 13, color: textColor(isDark) }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                <StatusBadge status={status(card)} />
                <span style={{ color: dim }}>{card.trigger} · {card.source}:{card.event_type}</span>
                {card.detail?.owner ? <span style={{ color: dim }}>· owner {String(card.detail.owner)}</span> : null}
                <span style={{ color: dim, fontFamily: 'monospace', marginLeft: 'auto' }}>{fmtTime(card.created_at)}</span>
                {onStop && RUNNING_STATUSES.has(card.column) && (
                  <Button
                    size="small" danger icon={<StopOutlined />}
                    onClick={() => { onStop(card.card_id); message.info('Stopping run…') }}
                  >
                    Stop
                  </Button>
                )}
              </div>

              {renderDrawerExtra?.(card)}

              {error && (
                <div style={{ color: COLUMN_COLOR.failed, marginBottom: 16, whiteSpace: 'pre-wrap' }}>{error}</div>
              )}

              {body && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 12 }}>Output</span>
                    <CopyButton text={body} title="Copy output" />
                  </div>
                  <div style={{ marginBottom: 16 }}>
                    <Markdown color={textColor(isDark)}>{body}</Markdown>
                  </div>
                </>
              )}

              {card.timeline && card.timeline.length > 0 && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 12 }}>Timeline</span>
                    <CopyButton
                      text={card.timeline.map(e => `${fmtTime(e.ts)}  ${e.text}`).join('\n')}
                      title="Copy timeline"
                    />
                  </div>
                  <div style={{ borderLeft: `2px solid ${border}`, paddingLeft: 10 }}>
                    {card.timeline.map((entry, i) => (
                      <div key={i} style={{ marginBottom: 3 }}>
                        <span style={{ color: dim, fontFamily: 'monospace', marginRight: 8 }}>{fmtTime(entry.ts)}</span>
                        {entry.text}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )
        })()}
      </Drawer>
    </div>
  )
}
