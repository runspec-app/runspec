/**
 * ScheduleScopeCards — the four option cards across the top of the Schedules tab:
 * All / This console / Host cron / Host daemon. Each is a full clickable tile
 * (icon, title, one line on what it is, live counts) so the choice reads as a
 * choice; the selected one takes the accent border and fill, and a card with
 * problems shows an amber count.
 */
import type { ReactNode } from 'react'
import { Spin, theme } from 'antd'
import { AppstoreOutlined, DesktopOutlined, ScheduleOutlined, CloudServerOutlined, WarningFilled } from '@ant-design/icons'
import { RADIUS, useAccent, useIsDark, textColor, textMuted, textSecondary, withAlpha } from '../ThemeContext'
import type { ScheduleScope, ScopeCount } from './allSchedules'

interface CardDef {
  scope: ScheduleScope
  title: string
  line: string
  icon: ReactNode
}

export const SCOPE_CARDS: CardDef[] = [
  { scope: 'all', title: 'All', line: 'Everything that fires, here and on the profile’s hosts', icon: <AppstoreOutlined /> },
  { scope: 'console', title: 'This console', line: 'Fires from this desktop while the console is open', icon: <DesktopOutlined /> },
  { scope: 'cron', title: 'Host cron', line: 'Each venv’s crontab, installed by runspec-scheduler sync', icon: <ScheduleOutlined /> },
  { scope: 'daemon', title: 'Host daemon', line: 'runspec-scheduler run, kept alive on the host', icon: <CloudServerOutlined /> },
]

interface ScheduleScopeCardsProps {
  value: ScheduleScope
  onChange: (v: ScheduleScope) => void
  counts: Record<ScheduleScope, ScopeCount>
  /** Host counts are still being read for the first time. */
  hostsPending: boolean
  /** No venv uses the daemon backend (yet). */
  noDaemons: boolean
}

export function ScheduleScopeCards({ value, onChange, counts, hostsPending, noDaemons }: ScheduleScopeCardsProps) {
  const isDark = useIsDark()
  const accent = useAccent()
  const { token } = theme.useToken()

  const countLine = (c: CardDef) => {
    const n = counts[c.scope]
    const pending = hostsPending && c.scope !== 'console'
    if (pending) return <span><Spin size="small" style={{ marginRight: 6 }} />reading hosts…</span>
    if (c.scope === 'daemon' && noDaemons) return <span>none yet</span>
    return <span>{n.schedules} schedule{n.schedules === 1 ? '' : 's'}</span>
  }

  return (
    <div
      role="radiogroup"
      aria-label="Which schedules"
      style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12, marginBottom: 16 }}
    >
      {SCOPE_CARDS.map(c => {
        const selected = c.scope === value
        const problems = counts[c.scope].problems
        return (
          <button
            key={c.scope}
            type="button"
            role="radio"
            aria-checked={selected}
            data-scope={c.scope}
            className="rs-scope-card"
            onClick={() => onChange(c.scope)}
            style={{
              textAlign: 'left',
              cursor: 'pointer',
              padding: '12px 14px',
              borderRadius: RADIUS.md,
              border: `${selected ? 2 : 1}px solid ${selected ? accent : 'var(--rs-border-strong)'}`,
              margin: selected ? 0 : 1,  // keep the size steady when the border thickens
              background: selected ? withAlpha(accent, isDark ? 0.14 : 0.08) : 'var(--rs-surface-raised)',
              boxShadow: selected ? 'none' : 'var(--rs-shadow-sm)',
              color: textColor(isDark),
              font: 'inherit',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              minWidth: 0,
            }}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 18, color: selected ? accent : textSecondary(isDark), lineHeight: 1 }}>{c.icon}</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{c.title}</span>
              {problems > 0 && !(hostsPending && c.scope !== 'console') && (
                <span
                  aria-label={`${problems} problem${problems === 1 ? '' : 's'}`}
                  title={`${problems} problem${problems === 1 ? '' : 's'}`}
                  style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 4, padding: '0 7px', borderRadius: 10, fontSize: 12, fontWeight: 600, background: token.colorWarning, color: 'rgba(0,0,0,0.88)' }}
                >
                  <WarningFilled style={{ fontSize: 11 }} />{problems}
                </span>
              )}
            </span>
            <span style={{ fontSize: 12, color: textSecondary(isDark), lineHeight: 1.4 }}>{c.line}</span>
            <span style={{ fontSize: 12, color: textMuted(isDark) }}>{countLine(c)}</span>
          </button>
        )
      })}
    </div>
  )
}
