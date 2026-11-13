// Shared building blocks so every tab speaks the same visual language:
//   StatusTag    one status vocabulary (ok / running / waiting / attention /
//                failed / info / neutral), colours checked in both themes
//   IconAction   an icon-only button that must carry a label (tooltip + aria)
//   RowActions   one primary action, quiet secondaries, risky ones in "More"
//   PageToolbar  filters on the left, actions on the right
//   PageHeader   one header style for a tab: title, a short line, actions
//   OnceHint     a tip shown until the operator dismisses it, then never again
//   EmptyState   one line saying why it's empty, plus at most one action

import type { CSSProperties, MouseEvent, ReactNode } from 'react'
import { useState } from 'react'
import { Button, Dropdown, Modal, Tooltip, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { CloseOutlined, EllipsisOutlined, InfoCircleOutlined } from '@ant-design/icons'

import {
  dangerColors, statusText, textMuted, textSecondary, useAccent, useIsDark, withAlpha, MONO_FONT, RADIUS,
} from '../ThemeContext'

const { Text } = Typography

// ── StatusTag ────────────────────────────────────────────────────────────────

export type StatusTone = 'ok' | 'running' | 'waiting' | 'attention' | 'failed' | 'info' | 'neutral'

/** The text colour and tint for a status tone. `accent` is the readable accent
 *  (useAccent()): running uses it, and failed steps away from it when the brand
 *  is itself a red. Pure, for tests. */
export function statusTone(tone: StatusTone, isDark: boolean, accent: string): { color: string; bg: string; border: string } {
  const st = statusText(isDark)
  const color = tone === 'ok' ? st.ok
    : tone === 'running' ? accent
    : tone === 'waiting' ? st.waiting
    : tone === 'attention' ? st.warn
    : tone === 'failed' ? dangerColors(accent, isDark).text
    : tone === 'info' ? textSecondary(isDark)
    : textMuted(isDark)
  // Outlined, not filled: a tint under the text costs contrast (each colour is
  // chosen to read at 4.5:1 on the bare surfaces), so the tone rides the border.
  return { color, bg: 'transparent', border: withAlpha(color, 0.5) }
}

export function StatusTag({ tone, children, tip, icon, mono, style, testId }: {
  tone: StatusTone
  children: ReactNode
  tip?: ReactNode
  icon?: ReactNode
  mono?: boolean
  style?: CSSProperties
  testId?: string
}) {
  const isDark = useIsDark()
  const accent = useAccent()
  const c = statusTone(tone, isDark, accent)
  const tag = (
    <span
      data-testid={testId}
      data-tone={tone}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap',
        fontSize: 12, lineHeight: '18px', padding: '0 6px', borderRadius: 4,
        color: c.color, background: c.bg, border: `1px solid ${c.border}`,
        fontWeight: tone === 'waiting' || tone === 'failed' ? 600 : 500,
        fontFamily: mono ? MONO_FONT : 'inherit',
        cursor: tip ? 'default' : undefined,
        ...style,
      }}
    >
      {icon}{children}
    </span>
  )
  return tip ? <Tooltip title={tip}>{tag}</Tooltip> : tag
}

// ── IconAction ───────────────────────────────────────────────────────────────

/** An icon-only button. `label` is required: it is the tooltip and what a screen
 *  reader announces, so an icon never ships without a name. */
export function IconAction({ label, icon, onClick, danger, disabled, tip, style, testId }: {
  label: string
  icon: ReactNode
  onClick?: (e: MouseEvent<HTMLElement>) => void
  danger?: boolean
  disabled?: boolean
  // A longer tooltip than the label, when the name alone doesn't explain it.
  tip?: ReactNode
  style?: CSSProperties
  testId?: string
}) {
  return (
    <Tooltip title={tip ?? label}>
      <Button
        type="text" size="small" icon={icon} aria-label={label} danger={danger} disabled={disabled}
        onClick={onClick} data-testid={testId}
        style={{ color: danger || disabled ? undefined : 'var(--rs-text-muted)', ...style }}
      />
    </Tooltip>
  )
}

// ── RowActions ───────────────────────────────────────────────────────────────

export interface RowAction {
  key: string
  label: string
  icon?: ReactNode
  onClick: () => void
  tip?: ReactNode
  disabled?: boolean
  danger?: boolean
  // Ask before running it. A string is the question; Modal.confirm's OK button
  // repeats the action's label.
  confirm?: string | { title: string; content?: ReactNode; okText?: string }
}

function runAction(a: RowAction) {
  if (!a.confirm) { a.onClick(); return }
  const c = typeof a.confirm === 'string' ? { title: a.confirm } : a.confirm
  Modal.confirm({
    title: c.title, content: c.content,
    okText: c.okText ?? a.label, okButtonProps: { danger: !!a.danger },
    cancelText: 'Cancel',
    onOk: a.onClick,
  })
}

/** One visible primary action, quiet secondaries beside it, and everything else
 *  (destructive actions above all) behind a "More actions" menu, so the action a
 *  row is for is never the same weight as the one that deletes it. */
export function RowActions({ primary, secondary = [], more = [], style, testId = 'row-actions' }: {
  primary?: RowAction
  secondary?: RowAction[]
  more?: RowAction[]
  style?: CSSProperties
  testId?: string
}) {
  const items: MenuProps['items'] = more.map(a => ({
    key: a.key, label: a.label, icon: a.icon, danger: a.danger, disabled: a.disabled,
  }))
  const wrap = (a: RowAction, node: ReactNode) => (a.tip ? <Tooltip key={a.key} title={a.tip}>{node}</Tooltip> : node)
  return (
    <div data-testid={testId} style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', ...style }}>
      {primary && wrap(primary,
        <Button key={primary.key} size="small" icon={primary.icon} disabled={primary.disabled} danger={primary.danger}
          onClick={() => runAction(primary)}>
          {primary.label}
        </Button>)}
      {secondary.map(a => wrap(a,
        <Button key={a.key} size="small" type="text" icon={a.icon} disabled={a.disabled} danger={a.danger}
          onClick={() => runAction(a)} style={{ color: a.danger || a.disabled ? undefined : 'var(--rs-text-secondary)' }}>
          {a.label}
        </Button>))}
      {more.length > 0 && (
        <Dropdown trigger={['click']} menu={{
          items,
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation()
            const a = more.find(m => m.key === key)
            if (a) runAction(a)
          },
        }}>
          <span><IconAction label="More actions" icon={<EllipsisOutlined />} testId="row-actions-more" /></span>
        </Dropdown>
      )}
    </div>
  )
}

// ── PageToolbar / PageHeader ─────────────────────────────────────────────────

/** Search and filters on the left, the tab's actions on the right. */
export function PageToolbar({ children, actions, style }: {
  children?: ReactNode
  actions?: ReactNode
  style?: CSSProperties
}) {
  return (
    <div data-testid="page-toolbar" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12, ...style }}>
      {children}
      {actions && <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>{actions}</div>}
    </div>
  )
}

/** One header style for every tab: a title, at most one short line under it,
 *  and the tab's actions on the right. Longer guidance belongs in a OnceHint. */
export function PageHeader({ title, description, actions, style }: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  style?: CSSProperties
}) {
  const isDark = useIsDark()
  return (
    <div data-testid="page-header" style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 12, ...style }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <h2 style={{ margin: 0, fontSize: 18, lineHeight: '26px', fontWeight: 600, color: 'inherit' }}>{title}</h2>
        {description && <div style={{ marginTop: 2, fontSize: 13, color: textSecondary(isDark) }}>{description}</div>}
      </div>
      {actions && <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>{actions}</div>}
    </div>
  )
}

// ── OnceHint ─────────────────────────────────────────────────────────────────

const HINT_PREFIX = 'rs.hint.'

/** True when the operator has dismissed hint `id` (per viewer, in localStorage). */
export function hintSeen(id: string): boolean {
  try { return localStorage.getItem(HINT_PREFIX + id) === '1' } catch { return false }
}

export function markHintSeen(id: string): void {
  try { localStorage.setItem(HINT_PREFIX + id, '1') } catch { /* storage blocked */ }
}

/** A tip shown until the operator dismisses it. Use it instead of a permanent
 *  paragraph: the first visit explains, the hundredth doesn't. */
export function OnceHint({ id, children, style }: { id: string; children: ReactNode; style?: CSSProperties }) {
  const [seen, setSeen] = useState(() => hintSeen(id))
  const isDark = useIsDark()
  if (seen) return null
  return (
    <div data-testid={`hint-${id}`} role="note" style={{
      display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, padding: '6px 6px 6px 10px',
      fontSize: 13, color: textSecondary(isDark),
      background: 'var(--rs-surface-raised)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm,
      ...style,
    }}>
      <InfoCircleOutlined style={{ color: textMuted(isDark), flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0 }}>{children}</span>
      <IconAction label="Dismiss this tip" icon={<CloseOutlined />} testId={`hint-${id}-dismiss`}
        onClick={() => { markHintSeen(id); setSeen(true) }} />
    </div>
  )
}

// ── EmptyState ───────────────────────────────────────────────────────────────

/** One line saying why there is nothing here, and at most one thing to do
 *  about it. Replaces Ant's stock <Empty> illustration. */
export function EmptyState({ message, action, style }: {
  message: ReactNode
  action?: { label: string; onClick: () => void; icon?: ReactNode }
  style?: CSSProperties
}) {
  const isDark = useIsDark()
  return (
    <div data-testid="empty-state" style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8,
      padding: '28px 16px', textAlign: 'center', ...style,
    }}>
      <Text style={{ fontSize: 13, color: textMuted(isDark) }}>{message}</Text>
      {action && <Button size="small" icon={action.icon} onClick={action.onClick}>{action.label}</Button>}
    </div>
  )
}
