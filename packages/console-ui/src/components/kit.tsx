// Shared building blocks so every tab speaks the same visual language:
//   StatusTag    one status vocabulary (ok / running / waiting / attention /
//                failed / info / neutral), colours checked in both themes
//   IconAction   an icon-only button that must carry a label (tooltip + aria)
//   RowActions   one primary action, quiet secondaries, risky ones in "More"
//   PageToolbar  filters on the left, actions on the right
//   PageHeader   one header style for a tab: title, a short line, actions
//   OnceHint     a tip shown until the operator dismisses it, then never again
//   EmptyState   one line saying why it's empty, plus at most one action

import type { CSSProperties, HTMLAttributes, MouseEvent, ReactNode, Ref, SyntheticEvent } from 'react'
import { cloneElement, isValidElement, useState } from 'react'
import { Button, Dropdown, Modal, Segmented, Tooltip, Typography, theme } from 'antd'
import type { MenuProps } from 'antd'
import { useRiskConfirm } from './risk'
import { CheckCircleFilled, CloseCircleFilled, CloseOutlined, EllipsisOutlined, ExclamationCircleOutlined, InfoCircleOutlined, PauseCircleOutlined, WarningFilled } from '@ant-design/icons'

import {
  dangerColors, statusText, textColor, textMuted, textSecondary, useAccent, useIsDark, withAlpha, MONO_FONT, RADIUS,
} from '../ThemeContext'

const { Text } = Typography

// ── StatusTag ────────────────────────────────────────────────────────────────

export type StatusTone = 'ok' | 'running' | 'waiting' | 'attention' | 'failed' | 'info' | 'neutral'

/** The text colour and tint for a status tone. Every tone has its own fixed
 *  hue, independent of the white-label accent: under a red brand "running" in
 *  the brand colour sat beside "failed" as two pinks. `accent` only moves
 *  failed off a red brand's hue (dangerColors). Pure, for tests. */
export function statusTone(tone: StatusTone, isDark: boolean, accent: string): { color: string; bg: string; border: string } {
  const st = statusText(isDark)
  const color = tone === 'ok' ? st.ok
    : tone === 'running' ? st.running
    : tone === 'waiting' ? st.waiting
    : tone === 'attention' ? st.warn
    : tone === 'failed' ? dangerColors(accent, isDark).text
    : tone === 'info' ? textSecondary(isDark)
    : textMuted(isDark)
  // Outlined, not filled: a tint under the text costs contrast (each colour is
  // chosen to read at 4.5:1 on the bare surfaces), so the tone rides the border.
  return { color, bg: 'transparent', border: withAlpha(color, 0.5) }
}

/** The tone for one of Ant's preset tag colours, for code that still computes
 *  `{ color, text }` (green → ok, processing → running, orange → attention, …). */
export function antTone(color: string | undefined): StatusTone {
  switch (color) {
    case 'green': case 'success': case 'lime': return 'ok'
    case 'processing': return 'running'
    case 'orange': case 'gold': case 'warning': case 'yellow': return 'attention'
    case 'red': case 'error': case 'volcano': case 'magenta': return 'failed'
    case 'blue': case 'geekblue': case 'cyan': case 'purple': return 'info'
    default: return 'neutral'
  }
}

// Waiting and attention are both warm hues; an icon tells them apart for
// anyone who can't (or doesn't) read the colour.
const TONE_ICON: Partial<Record<StatusTone, ReactNode>> = {
  waiting: <PauseCircleOutlined aria-hidden style={{ fontSize: 11 }} />,
  attention: <ExclamationCircleOutlined aria-hidden style={{ fontSize: 11 }} />,
}

export function StatusTag({ tone, children, tip, icon, mono, style, testId, onClick, ref, ...rest }: {
  tone: StatusTone
  children: ReactNode
  tip?: ReactNode
  icon?: ReactNode
  mono?: boolean
  style?: CSSProperties
  testId?: string
  // A clickable tag (a filter shortcut, "open the running job") is a button to
  // the keyboard and screen readers too.
  onClick?: (e: SyntheticEvent) => void
  ref?: Ref<HTMLSpanElement>
} & Omit<HTMLAttributes<HTMLSpanElement>, 'onClick' | 'style' | 'children'>) {
  const isDark = useIsDark()
  const accent = useAccent()
  const c = statusTone(tone, isDark, accent)
  const tag = (
    // Extra props and the ref pass through, so an outer <Tooltip> or Popover
    // can attach to the tag the way it would to an Ant <Tag>.
    <span
      {...rest}
      ref={ref}
      data-testid={testId}
      data-tone={tone}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={onClick ? e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(e) } } : undefined}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap',
        fontSize: 12, lineHeight: '18px', padding: '0 6px', borderRadius: 4,
        color: c.color, background: c.bg, border: `1px solid ${c.border}`,
        fontWeight: tone === 'waiting' || tone === 'failed' ? 600 : 500,
        fontFamily: mono ? MONO_FONT : 'inherit',
        cursor: onClick ? 'pointer' : tip ? 'default' : undefined,
        ...style,
      }}
    >
      {icon ?? TONE_ICON[tone]}{children}
    </span>
  )
  return tip ? <Tooltip title={tip}>{tag}</Tooltip> : tag
}

// ── pressable ────────────────────────────────────────────────────────────────

/** Props that make a clickable row or card (a <div> laid out like one) a
 *  button to the keyboard and screen readers: focusable, Enter/Space activate.
 *  Spread onto the element: `<div {...pressable(() => open(x))}>`. */
export function pressable(onActivate: () => void, opts: { label?: string; expanded?: boolean; current?: boolean } = {}) {
  return {
    role: 'button' as const,
    tabIndex: 0,
    'aria-label': opts.label,
    'aria-expanded': opts.expanded,
    'aria-current': opts.current ? ('true' as const) : undefined,
    onClick: onActivate,
    onKeyDown: (e: { key: string; target: EventTarget; currentTarget: EventTarget; preventDefault: () => void }) => {
      // Only the element itself: Enter in a nested input or button keeps its own meaning.
      if (e.target !== e.currentTarget) return
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onActivate() }
    },
  }
}

/** Give a form control the name its row shows, unless it already has one, so
 *  a switch or select beside a text label isn't read as "switch, off". */
export function withLabel(control: ReactNode, label: string): ReactNode {
  if (!isValidElement(control)) return control
  const props = control.props as Record<string, unknown>
  // Leave alone a control that is already named, or a wrapper (a plain element
  // or anything with children: a field plus its button) whose inner control
  // carries its own name.
  if (props['aria-label'] || props['aria-labelledby'] || props.id) return control
  if (typeof control.type === 'string' || props.children != null) return control
  return cloneElement(control as React.ReactElement<Record<string, unknown>>, { 'aria-label': label })
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
  // Ask before running it, through the shared confirmation dialog (risk.tsx,
  // local level). A string is the question; OK repeats the action's label.
  // (This used antd's static Modal.confirm, which doesn't render under
  // React 19, so the ask, and the action, silently never happened.)
  confirm?: string | { title: string; content?: ReactNode; okText?: string }
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
  const [askRisk, riskHolder] = useRiskConfirm()
  const runAction = async (a: RowAction) => {
    if (!a.confirm) { a.onClick(); return }
    const c = typeof a.confirm === 'string' ? { title: a.confirm } : a.confirm
    const answer = await askRisk({
      level: 'local', title: c.title, action: c.content ?? a.label,
      okText: c.okText ?? a.label, danger: !!a.danger,
    })
    if (answer.ok) a.onClick()
  }
  return (
    <div data-testid={testId} style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', ...style }}>
      {riskHolder}
      {primary && wrap(primary,
        <Button key={primary.key} size="small" icon={primary.icon} disabled={primary.disabled} danger={primary.danger}
          onClick={() => { void runAction(primary) }}>
          {primary.label}
        </Button>)}
      {secondary.map(a => wrap(a,
        <Button key={a.key} size="small" type="text" icon={a.icon} disabled={a.disabled} danger={a.danger}
          onClick={() => { void runAction(a) }} style={{ color: a.danger || a.disabled ? undefined : 'var(--rs-text-secondary)' }}>
          {a.label}
        </Button>))}
      {more.length > 0 && (
        <Dropdown trigger={['click']} menu={{
          items,
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation()
            const a = more.find(m => m.key === key)
            if (a) void runAction(a)
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
        <h2 style={{ margin: 0, fontSize: 18, lineHeight: '26px', fontWeight: 600, color: textColor(isDark) }}>{title}</h2>
        {description && <div style={{ marginTop: 2, fontSize: 13, color: textSecondary(isDark) }}>{description}</div>}
      </div>
      {actions && <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>{actions}</div>}
    </div>
  )
}

// ── SectionSwitch ────────────────────────────────────────────────────────────

/** The sections of a tab that holds several views (Automation: Schedules /
 *  Triggers / Working hours). A count shows beside a section that has
 *  something running or waiting, so it's visible from the others. */
export function SectionSwitch({ label, sections, value, onChange, style }: {
  label: string
  sections: { key: string; label: string; count?: number; countTone?: StatusTone }[]
  value: string
  onChange: (key: string) => void
  style?: CSSProperties
}) {
  return (
    <div data-testid="section-switch" style={{ marginBottom: 16, flexShrink: 0, ...style }}>
      <Segmented
        aria-label={`${label} sections`}
        value={value}
        onChange={v => onChange(String(v))}
        options={sections.map(s => ({
          value: s.key,
          label: (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              {s.label}
              {s.count ? <StatusTag tone={s.countTone ?? 'info'} style={{ lineHeight: '16px', padding: '0 5px' }}>{s.count}</StatusTag> : null}
            </span>
          ),
        }))}
      />
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
/** A done / failed / warning mark for a per-host result line, in the theme's
 *  status tones (never a fixed green or red) and named for screen readers. */
export function StatusIcon({ status, style }: { status: 'ok' | 'failed' | 'warn' | 'done'; style?: CSSProperties }) {
  const isDark = useIsDark()
  const st = statusText(isDark)
  const s = { fontSize: 14, ...style }
  if (status === 'ok') return <CheckCircleFilled role="img" aria-label="done" style={{ color: st.ok, ...s }} />
  if (status === 'done') return <CheckCircleFilled role="img" aria-label="done" style={{ color: textMuted(isDark), ...s }} />
  if (status === 'warn') return <WarningFilled role="img" aria-label="warning" style={{ color: st.warn, ...s }} />
  return <CloseCircleFilled role="img" aria-label="failed" style={{ color: st.error, ...s }} />
}

/** A Settings section's Save, pinned to the bottom of the scrolling content
 *  so it is always in reach (it used to sit mid-page, after one column). */
export function SaveBar({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  const { token } = theme.useToken()
  return (
    <div data-testid="save-bar" style={{
      position: 'sticky', bottom: 0, zIndex: 2, marginTop: 18, padding: '12px 0',
      display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
      background: token.colorBgElevated, borderTop: `1px solid ${token.colorBorderSecondary}`,
    }}>
      {children}
      {hint && <Typography.Text type="secondary" style={{ fontSize: 13 }}>{hint}</Typography.Text>}
    </div>
  )
}

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
