/**
 * One approval card, wherever something waits for a person: the assistant's
 * call in the transcript (OutputPanel) and a webhook a host is holding
 * (WebhookApprovals). Same panel, same buttons, same reason field, and the
 * same keyboard: Alt+A / Alt+D answer the first card on screen
 * (shortcuts.ts answers the first `[data-rs-confirm-gate]`), and that card
 * says so with a chip, so you can see which one a keypress hits.
 */
import { useEffect, useState, type ReactNode, type RefObject } from 'react'
import { Button, Input, Tooltip } from 'antd'
import { CaretRightOutlined } from '@ant-design/icons'
import { approveColors, textMuted, textSecondary, useIsDark, MONO_FONT, RADIUS } from '../ThemeContext'
import { gateShown, shortcutHint } from '../shortcuts'
import { StatusTag } from './kit'

/** The waiting panel: a warm tint, the same for every kind of approval. */
export const APPROVAL_BORDER = 'color-mix(in srgb, var(--rs-waiting-tint) 45%, var(--rs-border))'
export const APPROVAL_BG = 'color-mix(in srgb, var(--rs-waiting-tint) 8%, var(--rs-surface-raised))'

const GATE = '[data-rs-confirm-gate]'
const GATES_CHANGED = 'runspec:gates_changed'

/** Where a gate stands among the waiting gates on the current tab, in the
 *  order the keyboard answers them (document order). Gates on hidden tabs
 *  don't count: a card you can't see isn't in your queue. Pure DOM, for tests. */
export function gatePosition(el: Element | null, doc: Document = document): { index: number; total: number } {
  const all = [...doc.querySelectorAll(GATE)].filter(gateShown)
  return { index: el ? all.indexOf(el) : -1, total: all.length }
}

/** Keeps a card's place in the queue current as cards come and go. */
export function useGatePosition(ref: RefObject<HTMLElement | null>): { index: number; total: number } {
  const [pos, setPos] = useState({ index: -1, total: 0 })
  useEffect(() => {
    const update = () => {
      const p = gatePosition(ref.current)
      setPos(prev => (prev.index === p.index && prev.total === p.total ? prev : p))
    }
    update()
    window.addEventListener(GATES_CHANGED, update)
    // Tell the others a gate arrived (and, on unmount, left).
    window.dispatchEvent(new Event(GATES_CHANGED))
    return () => {
      window.removeEventListener(GATES_CHANGED, update)
      setTimeout(() => window.dispatchEvent(new Event(GATES_CHANGED)), 0)
    }
  }, [ref])
  return pos
}

/** "1 of 3 waiting" and, on the card the keyboard will answer, the keys. */
export function QueueMark({ index, total }: { index: number; total: number }) {
  const isDark = useIsDark()
  if (index < 0) return null
  return (
    <span data-testid="approval-queue" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: textMuted(isDark), flexShrink: 0 }}>
      {total > 1 && <span>{index + 1} of {total} waiting</span>}
      {index === 0 && (
        <Tooltip title={`${shortcutHint('approve')} approves and ${shortcutHint('deny')} denies this one`}>
          <span data-testid="approval-keys" style={{
            fontFamily: MONO_FONT, fontSize: 11, padding: '0 6px', borderRadius: RADIUS.sm,
            border: '1px solid var(--rs-field)', color: textSecondary(isDark),
          }}>{shortcutHint('approve')}</span>
        </Tooltip>
      )}
    </span>
  )
}

/** The decision row every approval card ends with: an optional reason (opened
 *  by "Add a reason…"), a green Approve & <verb>, and a neutral Deny. Deny
 *  always answers at once, so the keyboard shortcut keeps working. */
export function ApprovalActions({
  verb, onApprove, onDeny, reason, setReason, reasonHint, reasonLabel = 'Reason (optional)', busy, edited, extra,
}: {
  verb: string
  onApprove: () => void
  onDeny: () => void
  reason: string
  setReason: (s: string) => void
  /** What happens to the reason: "The assistant gets this with the denial." */
  reasonHint: string
  /** The reason field's name for screen readers. */
  reasonLabel?: string
  busy?: '' | 'approve' | 'deny'
  edited?: boolean
  extra?: ReactNode
}) {
  const isDark = useIsDark()
  const go = approveColors(isDark)
  const [open, setOpen] = useState(false)
  useEffect(() => { if (!reason) setOpen(false) }, [verb])  // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      {open && (
        <Input.TextArea
          autoFocus size="small" data-rs-confirm-reason="" aria-label={reasonLabel}
          placeholder={`Optional. ${reasonHint}`}
          value={reason} maxLength={2000} autoSize={{ minRows: 1, maxRows: 6 }}
          onChange={e => setReason(e.target.value)}
          onKeyDown={e => {
            // Enter sends the Deny with the reason; Shift+Enter is a newline.
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              if (reason.trim()) onDeny()
            }
          }}
          style={{ fontSize: 12, marginBottom: 10, borderRadius: 10 }}
        />
      )}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <Tooltip title={`Approve (${shortcutHint('approve')})`}>
          {/* One fixed look whatever the brand accent: green go, neutral Deny. */}
          <Button shape="round" icon={<CaretRightOutlined />} onClick={onApprove} data-rs-confirm="approve"
            loading={busy === 'approve'} disabled={!!busy} className="rs-approve"
            style={{ background: go.bg, borderColor: go.bg, color: go.text, ['--rs-approve-hover' as string]: go.hover }}>
            {edited ? <>Approve edited &amp; {verb}</> : <>Approve &amp; {verb}</>}
          </Button>
        </Tooltip>
        {edited && <StatusTag tone="attention" testId="confirm-edited">edited</StatusTag>}
        <Tooltip title={reason.trim() ? `Deny, with your reason (${shortcutHint('deny')})` : `Deny (${shortcutHint('deny')})`}>
          <Button shape="round" onClick={onDeny} data-rs-confirm="deny" loading={busy === 'deny'} disabled={!!busy}>
            {reason.trim() ? 'Deny with reason' : 'Deny'}
          </Button>
        </Tooltip>
        {!open && (
          <Button size="small" type="link" onClick={() => setOpen(true)} style={{ padding: 0, color: textSecondary(isDark) }}>
            Add a reason…
          </Button>
        )}
        {extra}
      </div>
    </>
  )
}
