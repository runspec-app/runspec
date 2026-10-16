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
const WAITING_COLOR = '#faad14'

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
  const { inline, overflow } = partitionSessions(sessions, statusOf, selectedId)
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
        const label = s.label || (s.interactive ? 'New chat' : s.kind)
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
              <Tooltip title={STATUS_TIP.waiting}>
                <BellOutlined aria-label="waiting for input" style={{ fontSize: 12, color: WAITING_COLOR, flexShrink: 0 }} />
              </Tooltip>
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
