import type { ReactNode } from 'react'
import { Button, Drawer, Empty, message } from 'antd'
import { StopOutlined } from '@ant-design/icons'
import { type RunCard } from '../bridge'
import { useIsDark, textColor, textMuted, MONO_FONT, RADIUS } from '../ThemeContext'
import { Markdown } from './Markdown'
import { ChatTranscript, StructuredOutput, parseOutput, transcriptToText, type BlockSegment } from './OutputPanel'
import { StatusBadge, CopyButton, fmtTime, COLUMN_COLOR, RUNNING_STATUSES } from './runShared'

// A live/finished agent transcript for one card: interleaved text + tool-call
// segments (built by the Triggers view from token/tool_start/tool_end events), so
// the panel can render the same rich interaction the console does.
export interface CardTranscript {
  segments: BlockSegment[]
  currentText: string
  done: boolean
}

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
  // Rich per-card transcript (interleaved text + tool calls). When present for a
  // card, the drawer renders it via the shared ChatTranscript so an agent
  // interaction shows its tool calls — same as the console. Schedules omit it.
  transcripts?: Record<string, CardTranscript>
  emptyText?: string
  renderRowExtra?: (card: RunCard) => ReactNode
  renderDrawerExtra?: (card: RunCard) => ReactNode
  // Rendered in the Drawer's pinned footer (below the scrolling body) so an
  // action that must stay reachable — the Triggers approve/deny gate — is always
  // visible instead of scrolling out of view above a long transcript. Return
  // null/undefined for no footer.
  renderDrawerFooter?: (card: RunCard) => ReactNode
}

export function RunInbox({
  cards,
  selected,
  onSelect,
  onStop,
  statusOf,
  streams = {},
  transcripts = {},
  emptyText = 'No runs yet.',
  renderRowExtra,
  renderDrawerExtra,
  renderDrawerFooter,
}: RunInboxProps) {
  const isDark = useIsDark()
  const dim = textMuted(isDark)
  const colBg = 'var(--rs-surface-sunken)'
  const border = 'var(--rs-border)'

  const status = (card: RunCard) => (statusOf ? statusOf(card) : card.column)
  const runs = Object.values(cards).sort((a, b) => b.created_at - a.created_at)
  const selectedCard = selected ? cards[selected] : undefined
  // Pinned footer content (e.g. the approve/deny gate) — null when none, so antd
  // renders no empty footer bar.
  const drawerFooter = selectedCard && renderDrawerFooter ? renderDrawerFooter(selectedCard) : null

  return (
    <div style={{ color: textColor(isDark) }}>
      {runs.length === 0 ? (
        <Empty description={<span style={{ color: dim }}>{emptyText}</span>} />
      ) : (
        <div style={{ border: `1px solid ${border}`, borderRadius: RADIUS.sm }}>
          {runs.map((card, i) => (
            <div
              key={card.card_id}
              onClick={() => onSelect(card.card_id)}
              style={{
                display: 'flex', gap: 8, alignItems: 'center', padding: '8px 10px',
                borderBottom: i < runs.length - 1 ? `1px solid ${border}` : undefined,
                cursor: 'pointer', fontSize: 13,
                background: selected === card.card_id ? colBg : undefined,
              }}
            >
              <StatusBadge status={status(card)} />
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}>
                {card.summary}
              </span>
              <span style={{ color: dim, flexShrink: 0 }}>{card.trigger}</span>
              {renderRowExtra?.(card)}
              <span style={{ color: dim, fontFamily: MONO_FONT, flexShrink: 0 }}>{fmtTime(card.created_at)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Run detail — full event, output, timeline, plus any injected sections */}
      <Drawer
        open={!!selectedCard}
        onClose={() => onSelect(null)}
        width="min(1040px, 94vw)"
        title={selectedCard?.summary}
        footer={drawerFooter || undefined}
        styles={{ footer: { padding: 0 } }}
      >
        {selectedCard && (() => {
          const card = selectedCard
          const result = card.detail?.result as string | undefined
          const error = card.detail?.error as string | undefined
          const body = result || streams[card.card_id]
          // Prefer the rich transcript (tool calls + markdown) when we captured the
          // agent interaction live this session; otherwise fall back to the finalised
          // body string — as a structured table if it's JSON, else markdown.
          const transcript = transcripts[card.card_id]
          const hasTranscript = !!transcript && (transcript.segments.length > 0 || transcript.currentText.length > 0)
          // Is the finalised body structured JSON (→ rich table) or plain prose (→ markdown)?
          const bodyIsStructured = !!body && !hasTranscript &&
            ['table', 'object', 'sections'].includes(
              parseOutput(body.split('\n').map(line => ({ id: '', line, stream: 'stdout' as const }))).kind,
            )
          return (
            <div style={{ fontSize: 14, color: textColor(isDark) }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                <StatusBadge status={status(card)} />
                <span style={{ color: dim }}>{card.trigger} · {card.source}:{card.event_type}</span>
                {card.detail?.owner ? <span style={{ color: dim }}>· owner {String(card.detail.owner)}</span> : null}
                <span style={{ color: dim, fontFamily: MONO_FONT, marginLeft: 'auto' }}>{fmtTime(card.created_at)}</span>
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

              {(hasTranscript || body) && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 13 }}>Output</span>
                    {/* When we have the rich transcript, copy the interleaved
                        text + tool calls + their output — not just the prose body. */}
                    {hasTranscript ? (
                      <CopyButton text={transcriptToText(transcript!.segments, transcript!.currentText)} title="Copy output" />
                    ) : body ? (
                      <CopyButton text={body} title="Copy output" />
                    ) : null}
                  </div>
                  <div style={{ marginBottom: 16 }}>
                    {hasTranscript ? (
                      <ChatTranscript
                        segments={transcript!.segments}
                        currentText={transcript!.currentText}
                        done={transcript!.done}
                      />
                    ) : bodyIsStructured ? (
                      <StructuredOutput text={body!} />
                    ) : (
                      <Markdown color={textColor(isDark)}>{body!}</Markdown>
                    )}
                  </div>
                </>
              )}

              {card.timeline && card.timeline.length > 0 && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 13 }}>Timeline</span>
                    <CopyButton
                      text={card.timeline.map(e => `${fmtTime(e.ts)}  ${e.text}`).join('\n')}
                      title="Copy timeline"
                    />
                  </div>
                  <div style={{ borderLeft: `2px solid ${border}`, paddingLeft: 10 }}>
                    {card.timeline.map((entry, i) => (
                      <div key={i} style={{ marginBottom: 3 }}>
                        <span style={{ color: dim, fontFamily: MONO_FONT, marginRight: 8 }}>{fmtTime(entry.ts)}</span>
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
