/**
 * WebhookApprovals — the cards at the top of the Console for webhook deliveries
 * a host's runspec-scheduler is holding for a person: a hook or rule with
 * `require_approval = true` (from runspec-scheduler 0.12.0 a hook otherwise
 * runs without asking; 0.11.0 also held `confirm` / `supervised` runnables).
 *
 * Each card says which hook, on which host and venv, what it wants to run and
 * the fields it was sent, and takes one decision. The first decision on any
 * console wins: when someone else decides first the card turns into a quiet
 * "approved by …" line until the run finishes. Works with AI off: nothing here
 * goes through the assistant.
 */
import { approvalPolicyText } from '../consoleInfo'
import { useRef, useState } from 'react'
import { Button, message } from 'antd'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import type { WebhookDelivery, WebhookVenv, WebhooksStatus } from '../bridge'
import {
  useIsDark, textColor, textMuted, textSecondary, textWarning, MONO_FONT, RADIUS,
} from '../ThemeContext'
import { StatusTag, RowActions } from './kit'
import { APPROVAL_BG, APPROVAL_BORDER, ApprovalActions, QueueMark, useGatePosition } from './approval'
import { flatFields, inFlight, relTime, runLine, waitingApprovals, deliveryStatus } from '../webhooks'

// The same approval card as the assistant's (approval.tsx): same panel, same
// buttons and reason, and Alt+A / Alt+D answer it when it's first in line.
/** A delivery with this many fields or fewer shows them open. */
export const FIELDS_SHOWN_OPEN = 6
/** Cards shown before the rest fold behind "N more". */
export const SHOWN_AT_ONCE = 3

// The chat approval card's words, from one source (consoleInfo).
const AUTONOMY_WHY: Record<string, string> = {
  confirm: approvalPolicyText('confirm'),
  supervised: approvalPolicyText('supervised'),
}

/** Why this delivery waits for a person: the hook asked for it
 *  (`require_approval`), or, on runspec-scheduler 0.11.0, the runnable's own
 *  `confirm` / `supervised` level. */
export function approvalWhy(d: Pick<WebhookDelivery, 'autonomy' | 'reason'>): string {
  if ((d.reason || '').includes('the hook asks for an approval')) return 'This hook asks for a person to approve each run.'
  return AUTONOMY_WHY[d.autonomy] || ''
}

export function WebhookCard({ venv, d, now = Date.now() }: { venv: WebhookVenv; d: WebhookDelivery; now?: number }) {
  const isDark = useIsDark()
  const [note, setNote] = useState('')
  const gateRef = useRef<HTMLElement>(null)
  const queue = useGatePosition(gateRef)
  const [busy, setBusy] = useState<'' | 'approve' | 'deny'>('')
  const fields0 = flatFields(d.fields)
  // The fields that triggered it come from outside: show them unless there are
  // many, so they're read before Approve, not after.
  const [showFields, setShowFields] = useState(fields0.length <= FIELDS_SHOWN_OPEN)
  const target = { host: venv.host, venv: venv.venv }
  const line = runLine(d)
  const fields = fields0
  const due = d.expires ? Date.parse(d.expires) - now : NaN
  const soon = Number.isFinite(due) && due < 2 * 60_000
  const expired = Number.isFinite(due) && due <= 0

  const decide = async (verdict: 'approve' | 'deny') => {
    setBusy(verdict)
    try {
      const r = await bridge.webhook_decide({ ...target, id: d.id, verdict, note: note.trim() || undefined })
      if (r.result?.won === false) message.info(`Someone got there first: ${r.error}`)
      else if (!r.ok) message.error(`Couldn't ${verdict} delivery ${d.id} from ${d.hook}: ${r.error || 'no answer from the host'}. Its card stays until it's decided or expires.`)
    } finally {
      setBusy('')
    }
  }
  const dismiss = async () => {
    const r = await bridge.webhook_dismiss({ ...target, id: d.id })
    if (!r.ok) { message.error(r.error || "Couldn't hide that card."); return }
    const key = `wh-hidden-${venv.key}-${d.id}`
    message.open({
      key, type: 'info', duration: 6,
      content: (
        <span>
          Hidden on this console; it still waits for others.{' '}
          <Button size="small" type="link" style={{ padding: 0, height: 'auto' }}
            onClick={() => { message.destroy(key); void bridge.webhook_dismiss({ ...target, id: d.id, undo: true }) }}>
            Undo
          </Button>
        </span>
      ),
    })
  }

  return (
    <section
      ref={gateRef}
      data-rs-confirm-gate=""
      aria-label={`Webhook ${d.hook} on ${venv.host} waiting for approval`}
      data-testid="webhook-card"
      style={{ background: APPROVAL_BG, border: `1px solid ${APPROVAL_BORDER}`, borderRadius: 16, padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <StatusTag tone="waiting">webhook</StatusTag>
        <span style={{ fontWeight: 600, fontSize: 14, color: textColor(isDark) }}>{d.hook}</span>
        <span style={{ fontFamily: MONO_FONT, fontSize: 12, color: textSecondary(isDark) }}>{venv.host} / {venv.venv}</span>
        <span style={{ fontSize: 12, color: textMuted(isDark) }}>
          {[d.received ? `received ${relTime(d.received, now)}` : '', d.source ? `from ${d.source}` : '', (d.count ?? 1) > 1 ? `${d.count} times` : '']
            .filter(Boolean).join(' · ')}
        </span>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 10, alignItems: 'center' }}>
          {Number.isFinite(due) && (
            <span style={{ fontSize: 12, color: soon ? textWarning(isDark) : textMuted(isDark) }}>
              {due > 0 ? `expires ${relTime(d.expires, now)}, then it won't run` : "expired: it won't run"}
            </span>
          )}
          <QueueMark {...queue} />
        </span>
      </div>

      {line ? (
        <div>
          <div style={{ fontSize: 12, color: textMuted(isDark), marginBottom: 2 }}>
            It wants to run this on <span style={{ fontFamily: MONO_FONT }}>{venv.host}</span>{d.rule ? <> (rule <span style={{ fontFamily: MONO_FONT }}>{d.rule}</span>)</> : null}
          </div>
          <code style={{
            display: 'block', fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark), whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            background: 'var(--rs-surface-sunken)', borderRadius: RADIUS.sm, padding: '6px 8px',
          }}>{line}</code>
          {approvalWhy(d) && <div style={{ fontSize: 12, color: textMuted(isDark), marginTop: 2 }}>{approvalWhy(d)}</div>}
        </div>
      ) : (
        <div style={{ fontSize: 13, color: textSecondary(isDark) }}>{d.reason || 'This hook asks for approval before it does anything.'}</div>
      )}

      {fields.length > 0 && (
        <div>
          <Button type="text" size="small" aria-expanded={showFields} onClick={() => setShowFields(s => !s)}
            icon={showFields ? <DownOutlined /> : <RightOutlined />} style={{ padding: '0 4px', color: textSecondary(isDark) }}>
            {showFields ? 'Hide' : 'Show'} the {fields.length} field{fields.length === 1 ? '' : 's'} it received
          </Button>
          {showFields && (
            <dl style={{ margin: '4px 0 0', display: 'grid', gridTemplateColumns: 'minmax(80px, max-content) 1fr', columnGap: 12, rowGap: 2, fontFamily: MONO_FONT, fontSize: 12 }}>
              {fields.map(([k, v]) => (
                <div key={k} style={{ display: 'contents' }}>
                  <dt style={{ color: textMuted(isDark) }}>{k}</dt>
                  <dd style={{ margin: 0, color: textColor(isDark), wordBreak: 'break-word' }}>{v}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}

      {expired ? (
        // Past its expiry the host has already dropped it: approving would do
        // nothing, so the only thing left is to clear it away.
        <div data-testid="webhook-expired" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, color: textSecondary(isDark) }}>It expired before anyone answered, so it won't run.</span>
          <Button shape="round" onClick={() => { void dismiss() }}>Hide</Button>
        </div>
      ) : (
      <div>
        <ApprovalActions
          verb={line ? 'run' : 'continue'}
          onApprove={() => { void decide('approve') }}
          onDeny={() => { void decide('deny') }}
          reason={note} setReason={s => setNote(s.slice(0, 500))}
          reasonHint="It's recorded with your decision, whichever it is."
          reasonLabel="Note for the record (optional)"
          busy={busy}
          extra={<RowActions testId="webhook-card-more" more={[{ key: 'hide', label: 'Hide on this console', onClick: () => { void dismiss() } }]} />}
        />
      </div>
      )}
    </section>
  )
}

/** "approved by alice · running" for a delivery decided elsewhere. */
function DecidedLine({ venv, d }: { venv: WebhookVenv; d: WebhookDelivery }) {
  const isDark = useIsDark()
  const st = deliveryStatus(d)
  return (
    <div data-testid="webhook-decided" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: textSecondary(isDark), padding: '2px 4px' }}>
      <StatusTag tone={st.tone}>{st.label}</StatusTag>
      <span style={{ fontWeight: 600, color: textColor(isDark) }}>{d.hook}</span>
      <span style={{ fontFamily: MONO_FONT }}>{venv.host} / {venv.venv}</span>
      <span>approved by {d.decided_by}{d.note ? ` (“${d.note}”)` : ''}</span>
    </div>
  )
}

/** The Console's one line about webhooks waiting on the Webhooks tab. */
export function WebhookPointer({ count, onReview }: { count: number; onReview: () => void }) {
  const isDark = useIsDark()
  return (
    <div data-testid="webhook-pointer" role="status" style={{
      display: 'flex', alignItems: 'center', gap: 10, margin: '10px 16px 0', padding: '6px 12px', flexShrink: 0,
      background: APPROVAL_BG, border: `1px solid ${APPROVAL_BORDER}`, borderRadius: RADIUS.md, fontSize: 13,
    }}>
      <StatusTag tone="waiting">webhook</StatusTag>
      <span style={{ color: textColor(isDark), fontWeight: 600 }}>
        {count} webhook{count === 1 ? '' : 's'} waiting for approval
      </span>
      <Button size="small" style={{ marginLeft: 'auto' }} onClick={onReview}>Review →</Button>
    </div>
  )
}

export function WebhookApprovals({ status }: { status: WebhooksStatus }) {
  const [all, setAll] = useState(false)
  const waiting = waitingApprovals(status)
  const decided = inFlight(status)
  if (!waiting.length && !decided.length) return null
  const shown = all ? waiting : waiting.slice(0, SHOWN_AT_ONCE)
  const rest = waiting.length - shown.length
  return (
    <div data-testid="webhook-approvals" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '10px 16px 0', maxHeight: '45vh', overflowY: 'auto', flexShrink: 0 }}>
      {shown.map(({ venv, delivery }) => <WebhookCard key={`${venv.key}-${delivery.id}`} venv={venv} d={delivery} />)}
      {rest > 0 && (
        <Button type="link" size="small" style={{ alignSelf: 'flex-start', padding: 0 }} onClick={() => setAll(true)}>
          {rest} more webhook{rest === 1 ? '' : 's'} waiting
        </Button>
      )}
      {decided.map(({ venv, delivery }) => <DecidedLine key={`d-${venv.key}-${delivery.id}`} venv={venv} d={delivery} />)}
    </div>
  )
}
