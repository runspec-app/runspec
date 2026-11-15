import type React from 'react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
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
import { useRiskConfirm } from './risk'

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
  const hhmm = (t: number) => {
    const d = new Date(t)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }
  // A chat with nothing sent yet: "New · 14:05", so two fresh chats can be told
  // apart. Its first message becomes its name.
  if (s.interactive && !s.label) return s.createdAt ? `New · ${hhmm(s.createdAt)}` : 'New'
  const base = s.label || s.kind
  if (s.interactive || /\b\d\d:\d\d\b/.test(base) || !s.createdAt) return base
  return `${base} · ${hhmm(s.createdAt)}`
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
  const [askRisk, riskHolder] = useRiskConfirm()
  const isDark = useIsDark()
  const accent = useAccent()
  const sf = surfaces(isDark)
  const rowRef = useRef<HTMLDivElement>(null)
  // Chips keep their order (waiting ones don't jump to the front); instead the
  // selected chip, and a chip that starts waiting, scroll into view.
  const waitingKey = sessions.filter(s => statusOf(s) === 'waiting').map(s => s.id).join(',')
  const seenWaiting = useRef<Set<string>>(new Set())
  useEffect(() => {
    const row = rowRef.current
    if (!row || !selectedId) return
    showChip(row, chipEl(row, selectedId))
  }, [selectedId, sessions.length])
  // Chips scrolled out of the row join the "+N more" menu, so its count is
  // everything you can't see.
  const [scrolledOff, setScrolledOff] = useState<string[]>([])
  const measure = () => {
    const row = rowRef.current
    if (!row) return
    const box = row.getBoundingClientRect()
    const off = Array.from(row.querySelectorAll<HTMLElement>('[data-session-id]'))
      .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && (r.left < box.left - 1 || r.right > box.right + 1) })
      .map(el => el.dataset.sessionId!)
    setScrolledOff(prev => (prev.join(',') === off.join(',') ? prev : off))
  }
  const measureRef = useRef(measure)
  measureRef.current = measure
  useLayoutEffect(() => { measure() })
  // A resize can leave the selected chip outside the row: bring it back.
  const hasRow = sessions.length > 0
  useEffect(() => {
    const row = rowRef.current
    if (!row) return
    const onScroll = () => measureRef.current()
    row.addEventListener('scroll', onScroll, { passive: true })
    let ro: ResizeObserver | undefined
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        showChip(row, row.querySelector<HTMLElement>('[aria-selected="true"]'))
        measureRef.current()
      })
      ro.observe(row)
    }
    return () => { row.removeEventListener('scroll', onScroll); ro?.disconnect() }
  }, [hasRow])
  useEffect(() => {
    const row = rowRef.current
    const ids = waitingKey ? waitingKey.split(',') : []
    const fresh = ids.filter(id => !seenWaiting.current.has(id))
    seenWaiting.current = new Set(ids)
    if (!row || fresh.length === 0) return
    showChip(row, chipEl(row, fresh[fresh.length - 1]))
  }, [waitingKey])
  if (sessions.length === 0) return null
  const parts = partitionSessions(sessions, statusOf, selectedId)
  const inline = parts.inline
  // The menu: chips scrolled out of view, then the folded-away runs.
  const overflow = [...inline.filter(s => scrolledOff.includes(s.id)), ...parts.overflow]
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

  // The chips scroll; "+N more" sits outside the scroller so it never scrolls
  // away. On a narrow Console the strip takes a row of its own (index.css).
  return (
    <div className="rs-session-strip" style={{ display: 'flex', alignItems: 'center', gap: 6, flex: '1 1 240px', minWidth: 0, marginRight: 'auto' }}>
    {riskHolder}
    <div
      ref={rowRef}
      role="tablist"
      aria-label="Sessions"
      style={{
        display: 'flex', alignItems: 'center', gap: 6,
        flex: '0 1 auto', minWidth: 0,
        overflowX: 'auto', overflowY: 'hidden', scrollbarWidth: 'thin',
        // Scrolling by hand settles on whole chips; scrolls the row makes itself
        // land on whole chips too (wholeChipScroll).
        scrollSnapType: 'x proximity',
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
              display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0, scrollSnapAlign: 'start',
              height: 26, padding: '0 8px', cursor: 'pointer', userSelect: 'none',
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
                <Button
                  size="small" type="text"
                  aria-label={`Close ${label}`}
                  icon={<CloseOutlined style={{ fontSize: 10 }} />}
                  onClick={async e => {
                    e.stopPropagation()
                    const a = await askRisk({
                      level: 'local', title: 'Close this chat?', action: label,
                      consequence: 'Its transcript leaves the Console; the saved conversation stays in History.',
                      okText: 'Close chat', danger: false,
                    })
                    if (a.ok) onClose(s.id)
                  }}
                  style={CLOSE_STYLE(textMuted(isDark))}
                />
              ) : (
                <Button
                  size="small" type="text"
                  aria-label={`Close ${label}`}
                  icon={<CloseOutlined style={{ fontSize: 10 }} />}
                  onClick={e => { e.stopPropagation(); onClose(s.id) }}
                  style={CLOSE_STYLE(textMuted(isDark))}
                />
              )
            )}
          </div>
        )
      })}
    </div>
      {overflow.length > 0 && (
        <Dropdown menu={{ items: overflowItems, onClick: onOverflowClick }} trigger={['click']} placement="bottomRight">
          <Button
            size="small"
            data-testid="session-overflow"
            aria-label={`${overflow.length} more session${overflow.length === 1 ? '' : 's'}`}
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

/**
 * Where to scroll the chip row so the target chip is fully shown and the row
 * starts on a whole chip (never a sliver of one). `starts`/`widths` are each
 * chip's offset and width inside the row's content; pure, for tests.
 */
export function wholeChipScroll(starts: number[], widths: number[], view: number, current: number, target: number): number {
  const left = starts[target], right = left + widths[target]
  let want = current
  if (left < current) want = left
  else if (right > current + view) want = right - view
  // The first chip starting at or after `want` that still shows the target.
  const snapped = starts.find(st => st >= want - 0.5 && st <= left + 0.5)
  return snapped ?? left
}

/** Scroll the row to whole chips with this one in full view. */
function showChip(row: HTMLElement, chip: HTMLElement | null | undefined) {
  if (!chip) return
  const box = row.getBoundingClientRect()
  const chips = Array.from(row.querySelectorAll<HTMLElement>('[data-session-id]'))
  const starts = chips.map(c => c.getBoundingClientRect().left - box.left + row.scrollLeft)
  const widths = chips.map(c => c.getBoundingClientRect().width)
  const i = chips.indexOf(chip)
  if (i < 0) return
  row.scrollLeft = wholeChipScroll(starts, widths, row.clientWidth, row.scrollLeft, i)
}

const chipEl = (row: HTMLElement, id: string) =>
  Array.from(row.querySelectorAll<HTMLElement>('[data-session-id]')).find(el => el.dataset.sessionId === id)

// The close × fills the chip's height: a 24px target (it was 16px).
const CLOSE_STYLE = (color: string): React.CSSProperties => ({
  padding: 0, height: 24, width: 24, minWidth: 24, marginRight: -6, lineHeight: 1, color,
})
