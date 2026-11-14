/**
 * WebhookApprovals — the cards at the top of the Console for webhook deliveries
 * a host's runspec-scheduler is holding for a person (a hook rule whose run is
 * `confirm` / `supervised`, or `require_approval`).
 *
 * Each card says which hook, on which host and venv, what it wants to run and
 * the fields it was sent, and takes one decision. The first decision on any
 * console wins: when someone else decides first the card turns into a quiet
 * "approved by …" line until the run finishes. Works with AI off: nothing here
 * goes through the assistant.
 */
import { useState } from 'react'
import { Button, Input, Tooltip, message } from 'antd'
import { CaretRightOutlined, DownOutlined, RightOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import type { WebhookDelivery, WebhookVenv, WebhooksStatus } from '../bridge'
import {
  useIsDark, textColor, textMuted, textSecondary, textWarning, approveColors, MONO_FONT, RADIUS,
} from '../ThemeContext'
import { StatusTag, RowActions } from './kit'
import { flatFields, inFlight, relTime, runLine, waitingApprovals, deliveryStatus } from '../webhooks'

// Same amber panel as a chat Approve/Deny card (OutputPanel), so a waiting
// webhook reads as the same kind of thing.
const AWAIT_BORDER = 'color-mix(in srgb, #e3b341 45%, var(--rs-border))'
const AWAIT_BG = 'color-mix(in srgb, #e3b341 8%, var(--rs-surface-raised))'
/** Cards shown before the rest fold behind "N more". */
export const SHOWN_AT_ONCE = 3

const AUTONOMY_WHY: Record<string, string> = {
  confirm: 'The runnable asks for approval before it runs.',
  supervised: 'The runnable asks for approval before it runs (supervised).',
}

export function WebhookCard({ venv, d, now = Date.now() }: { venv: WebhookVenv; d: WebhookDelivery; now?: number }) {
  const isDark = useIsDark()
  const go = approveColors(isDark)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<'' | 'approve' | 'deny'>('')
  const [showFields, setShowFields] = useState(false)
  const target = { host: venv.host, venv: venv.venv }
  const line = runLine(d)
  const fields = flatFields(d.fields)
  const due = d.expires ? Date.parse(d.expires) - now : NaN
  const soon = Number.isFinite(due) && due < 2 * 60_000

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
      aria-label={`Webhook ${d.hook} on ${venv.host} waiting for approval`}
      data-testid="webhook-card"
      style={{ background: AWAIT_BG, border: `1px solid ${AWAIT_BORDER}`, borderRadius: 14, padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <StatusTag tone="waiting">webhook</StatusTag>
        <span style={{ fontWeight: 600, fontSize: 14, color: textColor(isDark) }}>{d.hook}</span>
        <span style={{ fontFamily: MONO_FONT, fontSize: 12, color: textSecondary(isDark) }}>{venv.host} / {venv.venv}</span>
        <span style={{ fontSize: 12, color: textMuted(isDark) }}>
          {[d.received ? `received ${relTime(d.received, now)}` : '', d.source ? `from ${d.source}` : '', (d.count ?? 1) > 1 ? `${d.count} times` : '']
            .filter(Boolean).join(' · ')}
        </span>
        {Number.isFinite(due) && (
          <span style={{ marginLeft: 'auto', fontSize: 12, color: soon ? textWarning(isDark) : textMuted(isDark) }}>
            {due > 0 ? `expires ${relTime(d.expires, now)}` : 'expiring'}
          </span>
        )}
      </div>

      {line ? (
        <div>
          <div style={{ fontSize: 12, color: textMuted(isDark), marginBottom: 2 }}>
            Wants to run{d.rule ? <> (rule <span style={{ fontFamily: MONO_FONT }}>{d.rule}</span>)</> : null}
          </div>
          <code style={{
            display: 'block', fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark), whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            background: 'var(--rs-surface-sunken)', borderRadius: RADIUS.sm, padding: '6px 8px',
          }}>{line}</code>
          {AUTONOMY_WHY[d.autonomy] && <div style={{ fontSize: 12, color: textMuted(isDark), marginTop: 2 }}>{AUTONOMY_WHY[d.autonomy]}</div>}
        </div>
      ) : (
        <div style={{ fontSize: 13, color: textSecondary(isDark) }}>{d.reason || 'This hook asks for approval before it does anything.'}</div>
      )}

      {fields.length > 0 && (
        <div>
          <Button type="text" size="small" aria-expanded={showFields} onClick={() => setShowFields(s => !s)}
            icon={showFields ? <DownOutlined /> : <RightOutlined />} style={{ padding: '0 4px', color: textSecondary(isDark) }}>
            {showFields ? 'Hide' : 'Show'} {fields.length} field{fields.length === 1 ? '' : 's'} it was sent
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

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Input size="small" value={note} onChange={e => setNote(e.target.value)} maxLength={500}
          placeholder="Note for the record (optional)" aria-label="Note for the record"
          style={{ flex: '1 1 220px', maxWidth: 420 }} />
        <Tooltip title={line ? `Run it on ${venv.host} now` : 'Let the hook go on'}>
          <Button shape="round" size="small" icon={<CaretRightOutlined />} loading={busy === 'approve'} disabled={!!busy}
            className="rs-approve" onClick={() => { void decide('approve') }}
            style={{ background: go.bg, borderColor: go.bg, color: go.text, ['--rs-approve-hover' as string]: go.hover }}>
            Approve
          </Button>
        </Tooltip>
        <Button shape="round" size="small" loading={busy === 'deny'} disabled={!!busy} onClick={() => { void decide('deny') }}>
          Deny
        </Button>
        <RowActions testId="webhook-card-more" more={[{ key: 'hide', label: 'Hide on this console', onClick: () => { void dismiss() } }]} />
      </div>
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
