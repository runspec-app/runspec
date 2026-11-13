import type React from 'react'
/**
 * SessionStrip — the chip row in the Console toolbar, one chip per live
 * session (interactive chats + trigger / schedule fires). A chip shows its
 * kind, an ellipsised label (monospace for automation), a running spinner, an
 * amber bell while the run waits for the operator, an unread dot for activity
 * in a session that isn't selected, and a dismiss × once it's finished.
 * The row stays bounded: every chat, the selected session and anything waiting
 * on the operator always show, plus the newest few automation runs; the rest
 * fold into a "+N more" menu (a busy trigger can fire dozens of runs) that also
 * clears the finished ones in one click. Pure presentation: state comes from
 * useSessions + sessionStatus.
 */
import { Button, Dropdown, Popconfirm, Spin, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import { ApiOutlined, BellOutlined, ClockCircleOutlined, CloseOutlined, DownOutlined, LoadingOutlined, MessageOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useIsDark, useAccent, textColor, textMuted, textSecondary, surfaces, withAlpha, MONO_FONT, RADIUS } from '../ThemeContext'
import type { Session, SessionStatus } from '../sessions'

interface SessionStripProps {
  sessions: Session[]
  selectedId: string | null
  statusOf: (s: Session) => SessionStatus
  // Whether a session has transcript blocks — closing an interactive one with
  // blocks asks first (its Console transcript goes; History keeps the chat).
  hasBlocks: (id: string) => boolean
  onSelect: (id: string) => void
  onClose: (id: string) => void
}

// Ant's gold-6 — the semantic "needs attention" colour (same family as the
// existing amber warning states), deliberately not the brand accent.
const WAITING_COLOR = 'var(--rs-waiting)'

const KIND_ICON = {
  chat: MessageOutlined,
  trigger: ThunderboltOutlined,
  schedule: ClockCircleOutlined,
  mcp: ApiOutlined,
} as const

const STATUS_TIP: Record<SessionStatus, string> = {
  running: 'Working',
  waiting: 'Waiting for your input',
  idle: 'Idle',
  done: 'Finished',
}

// Automation chips shown before the rest fold into the overflow menu.
export const VISIBLE_AUTOMATION = 4

/** A chip's text. An automation run whose label carries no time gets its
 *  start time, so two runs of one trigger never read the same. */
export function chipText(s: Session): string {
  const base = s.label || (s.interactive ? 'New chat' : s.kind)
  if (s.interactive || /\b\d\d:\d\d\b/.test(base) || !s.createdAt) return base
  const d = new Date(s.createdAt)
  return `${base} · ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * Split sessions into the chips shown inline and the overflow menu. Always
 * inline: interactive chats, the selected session, and any session waiting on
 * the operator (an Approve/Deny must never hide in a menu). Other automation:
 * the newest `max` inline, the rest overflow (newest first). Inline keeps the
 * list's own order so chips don't jump around.
 */
export function partitionSessions(
  sessions: Session[],
  statusOf: (s: Session) => SessionStatus,
  selectedId: string | null,
  max = VISIBLE_AUTOMATION,
): { inline: Session[]; overflow: Session[] } {
  const pinned = (s: Session) => s.interactive || s.id === selectedId || statusOf(s) === 'waiting'
  const rest = sessions.filter(s => !pinned(s)).sort((a, b) => b.createdAt - a.createdAt)
  const shown = new Set(rest.slice(0, max).map(s => s.id))
  return {
    inline: sessions.filter(s => pinned(s) || shown.has(s.id)),
    overflow: rest.slice(max),
  }
}

export function SessionStrip({ sessions, selectedId, statusOf, hasBlocks, onSelect, onClose }: SessionStripProps) {
  const isDark = useIsDark()
  const accent = useAccent()
  const sf = surfaces(isDark)
  if (sessions.length === 0) return null
  const parts = partitionSessions(sessions, statusOf, selectedId)
  // A chip waiting for you comes first, so it's never the one scrolled away.
  const inline = [...parts.inline.filter(s => statusOf(s) === 'waiting'), ...parts.inline.filter(s => statusOf(s) !== 'waiting')]
  const overflow = parts.overflow
  const finishedOverflow = overflow.filter(s => {
    const st = statusOf(s)
    return st === 'idle' || st === 'done'
  })
  const overflowItems: MenuProps['items'] = [
    ...overflow.map(s => {
      const Icon = KIND_ICON[s.kind] ?? MessageOutlined
      const status = statusOf(s)
      return {
        key: s.id,
        icon: <Icon style={{ fontSize: 12 }} />,
        label: (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, maxWidth: 360 }}>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: MONO_FONT, fontSize: 12 }}>
              {s.label || s.kind}
            </span>
            {status === 'running' && <LoadingOutlined spin style={{ fontSize: 11 }} />}
            {s.unread && <span aria-label="unread" style={{ width: 6, height: 6, borderRadius: 3, background: accent, flexShrink: 0 }} />}
          </span>
        ),
        title: `${s.label} — ${STATUS_TIP[status]}`,
      }
    }),
    ...(finishedOverflow.length > 0
      ? [
          { type: 'divider' as const },
          { key: '__close_finished', icon: <CloseOutlined style={{ fontSize: 11 }} />, label: `Close ${finishedOverflow.length} finished` },
        ]
      : []),
  ]
  const onOverflowClick: MenuProps['onClick'] = ({ key }) => {
    if (key === '__close_finished') finishedOverflow.forEach(s => onClose(s.id))
    else onSelect(String(key))
  }
  const overflowUnread = overflow.some(s => s.unread)

  const onChipKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return
    const chips = Array.from(e.currentTarget.parentElement?.querySelectorAll<HTMLElement>('[data-testid="session-chip"]') ?? [])
    const i = chips.indexOf(e.currentTarget)
    const go = (j: number) => { e.preventDefault(); chips[(j + chips.length) % chips.length]?.focus() }
    if (e.key === 'ArrowRight') go(i + 1)
    else if (e.key === 'ArrowLeft') go(i - 1)
    else if (e.key === 'Home') go(0)
    else if (e.key === 'End') go(chips.length - 1)
    else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      const id = e.currentTarget.dataset.sessionId
      if (id) onSelect(id)
    }
  }

  return (
    <div
      role="tablist"
      aria-label="Sessions"
      style={{
        display: 'flex', alignItems: 'center', gap: 6,
        flex: 1, minWidth: 0, marginRight: 'auto',
        overflowX: 'auto', overflowY: 'hidden', scrollbarWidth: 'thin',
        padding: '2px 0',
      }}
    >
      {inline.map(s => {
        const status = statusOf(s)
        const selected = s.id === selectedId
        const Icon = KIND_ICON[s.kind] ?? MessageOutlined
        const finished = status === 'idle' || status === 'done'
        const muted = finished && !selected
        const labelColor = selected ? textColor(isDark) : muted ? textMuted(isDark) : textSecondary(isDark)
        const label = chipText(s)
        const closeBtn = (
          <Button
            size="small" type="text"
            aria-label="Close session"
            icon={<CloseOutlined style={{ fontSize: 10 }} />}
            onClick={e => { e.stopPropagation() }}
            style={{ padding: '0 2px', height: 16, minWidth: 16, lineHeight: 1, color: textMuted(isDark) }}
          />
        )
        return (
          <div
            key={s.id}
            role="tab"
            aria-selected={selected}
            className="rs-session-chip"
            data-testid="session-chip"
            data-session-id={s.id}
            data-status={status}
            data-selected={selected ? 'true' : 'false'}
            data-unread={s.unread ? 'true' : 'false'}
            // Roving focus: Tab lands on the selected chip (the first if none is),
            // arrows move between chips, Enter or Space opens one.
            tabIndex={selected || (!inline.some(x => x.id === selectedId) && s === inline[0]) ? 0 : -1}
            onKeyDown={onChipKey}
            onClick={() => onSelect(s.id)}
            title={`${label} — ${STATUS_TIP[status]}`}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0,
              height: 24, padding: '0 8px', cursor: 'pointer', userSelect: 'none',
              borderRadius: RADIUS.sm,
              border: `1px solid ${selected ? accent : sf.border}`,
              background: selected ? withAlpha(accent, 0.15) : 'transparent',
              color: labelColor, fontSize: 12,
              maxWidth: 220,
            }}
          >
            <Icon style={{ fontSize: 12, color: selected ? accent : labelColor, flexShrink: 0 }} />
            <span style={{
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0,
              fontFamily: s.interactive ? undefined : MONO_FONT,
            }}>
              {label}
            </span>
            {status === 'running' && (
              <Spin size="small" indicator={<LoadingOutlined style={{ fontSize: 11, color: selected ? accent : labelColor }} spin />} />
            )}
            {status === 'waiting' && (
              <span data-testid="chip-waiting" style={{ display: 'inline-flex', alignItems: 'center', gap: 3, color: WAITING_COLOR, fontWeight: 600, flexShrink: 0 }}>
                <BellOutlined aria-hidden style={{ fontSize: 12 }} />
                waiting for you
              </span>
            )}
            {s.unread && !selected && (
              <span aria-label="unread" style={{ width: 6, height: 6, borderRadius: 3, background: accent, flexShrink: 0 }} />
            )}
            {finished && (
              s.interactive && hasBlocks(s.id) ? (
                <Popconfirm
                  title="Close this chat?"
                  description="Its transcript leaves the Console (the saved conversation stays in History)."
                  okText="Close" cancelText="Keep"
                  placement="bottom"
                  onConfirm={e => { e?.stopPropagation(); onClose(s.id) }}
                  onCancel={e => e?.stopPropagation()}
                >
                  {closeBtn}
                </Popconfirm>
              ) : (
                <Button
                  size="small" type="text"
                  aria-label="Close session"
                  icon={<CloseOutlined style={{ fontSize: 10 }} />}
                  onClick={e => { e.stopPropagation(); onClose(s.id) }}
                  style={{ padding: '0 2px', height: 16, minWidth: 16, lineHeight: 1, color: textMuted(isDark) }}
                />
              )
            )}
          </div>
        )
      })}
      {overflow.length > 0 && (
        <Dropdown menu={{ items: overflowItems, onClick: onOverflowClick }} trigger={['click']} placement="bottomRight">
          <Button
            size="small"
            data-testid="session-overflow"
            aria-label={`${overflow.length} more sessions`}
            style={{ flexShrink: 0, height: 24, fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }}
          >
            +{overflow.length} more
            {overflowUnread && <span aria-label="unread in overflow" style={{ width: 6, height: 6, borderRadius: 3, background: accent }} />}
            <DownOutlined style={{ fontSize: 10 }} />
          </Button>
        </Dropdown>
      )}
    </div>
  )
}
