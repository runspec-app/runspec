/**
 * WebhooksSection — the Schedules tab's view of the webhooks this console
 * follows: one strip per followed host + venv (connection, mutes, recent
 * deliveries, the hook editor) and a quiet offer to follow hosts whose venvs
 * take webhooks but that this console doesn't follow yet.
 *
 * Waiting approvals are decided on the Console tab's cards; here a strip only
 * counts them.
 */
import { useState } from 'react'
import { Button, Dropdown, Tag, Tooltip, message } from 'antd'
import { DownOutlined, RightOutlined, ApiOutlined, BellOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import type { WebhookVenv, WebhooksStatus } from '../bridge'
import { useIsDark, textColor, textMuted, textSecondary, textWarning, MONO_FONT, RADIUS } from '../ThemeContext'
import { StatusTag, RowActions } from '../components/kit'
import { connection, deliveryStatus, muteLabel, relTime, runLine } from '../webhooks'
import type { SchedulerVenv } from './allSchedules'
import { WebhookHooksModal } from './WebhookHooksModal'

export const MUTE_CHOICES = [
  { key: '1h', label: 'for 1 hour' },
  { key: '4h', label: 'for 4 hours' },
  { key: '12h', label: 'for 12 hours' },
  { key: '24h', label: 'for a day' },
]

/** Hosts with a venv that takes webhooks (`[hooks] enabled`) that this console
 *  doesn't follow yet, sorted. */
export function followOffers(venvs: SchedulerVenv[], status: WebhooksStatus): string[] {
  const hosts = venvs.filter(v => v.webhooks?.enabled && v.webhooks.hooks.length > 0).map(v => v.host)
  return [...new Set(hosts)].filter(h => !(status.follows[h]?.length)).sort()
}

/** Venvs whose runspec-scheduler takes webhooks (0.11.0+) that no followed
 *  strip covers: where "Set up webhooks…" can open the hook editor. */
export function setupTargets(venvs: SchedulerVenv[], status: WebhooksStatus): { host: string; venv: string }[] {
  const followed = new Set(status.venvs.map(v => `${v.host}/${v.venv}`))
  return venvs.filter(v => v.webhooks && !followed.has(`${v.host}/${v.venv}`)).map(v => ({ host: v.host, venv: v.venv }))
}

/** "follows every hook" / "follows alerts, deploys". */
export function followsText(follows: string[]): string {
  return follows.includes('*') ? 'follows every hook' : `follows ${follows.join(', ')}`
}

function WebhookStrip({ v, editable, onHooks }: { v: WebhookVenv; editable: boolean; onHooks: (v: WebhookVenv) => void }) {
  const isDark = useIsDark()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const conn = connection(v)
  const target = { host: v.host, venv: v.venv }
  const waiting = v.open.filter(d => d.state === 'pending').length
  const last = v.recent[0]

  const mute = async (span: string, label: string) => {
    setBusy(true)
    try {
      const r = await bridge.webhook_mute({ ...target, hook: '*', for: span, reason: 'muted from the console' })
      if (!r.ok) message.error(`Couldn't mute webhooks on ${v.host} / ${v.venv}: ${r.error || 'no answer from the host'}.`)
      else message.success(`Webhook alerts from ${v.host} / ${v.venv} muted ${label}. Deliveries still arrive and wait.`)
    } finally { setBusy(false) }
  }
  const unmute = async (hook: string) => {
    const r = await bridge.webhook_unmute({ ...target, hook })
    if (!r.ok) message.error(`Couldn't unmute ${muteLabel(hook)}: ${r.error || 'no answer from the host'}.`)
  }
  const stopFollowing = async () => {
    const r = await bridge.webhook_follow({ host: v.host, hooks: [] })
    if (!r.ok) message.error(`Couldn't stop following ${v.host}: ${r.error}`)
  }

  return (
    <div data-testid="webhook-strip" style={{
      display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 10px',
      border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, background: 'var(--rs-surface-raised)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <StatusTag tone={conn.tone} tip={conn.detail || undefined}>webhooks {conn.label}</StatusTag>
        {waiting > 0 && <StatusTag tone="waiting" tip="Decide them on the Console tab">{waiting} waiting</StatusTag>}
        <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{v.host} / {v.venv}</span>
        <span style={{ color: textMuted(isDark), fontSize: 12 }}>{followsText(v.follows)}</span>
        {v.mutes.map(m => (
          <Tag key={`${m.who}-${m.hook}`} closable={editable} onClose={e => { e.preventDefault(); void unmute(m.hook) }}
            icon={<BellOutlined />} style={{ marginInlineEnd: 0, fontSize: 12 }}
            closeIcon={editable ? undefined : false}>
            <Tooltip title={m.reason ? `${m.reason} (${m.who})` : m.who}>
              {muteLabel(m.hook)} muted until {new Date(m.until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </Tooltip>
          </Tag>
        ))}
        {v.dismissed > 0 && <span style={{ color: textMuted(isDark), fontSize: 12 }}>{v.dismissed} hidden on this console</span>}
        {editable && (
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
            <Dropdown trigger={['click']} disabled={busy} menu={{
              items: MUTE_CHOICES.map(c => ({ key: c.key, label: `Mute every hook ${c.label}` })),
              onClick: ({ key }) => { const c = MUTE_CHOICES.find(x => x.key === key); if (c) void mute(c.key, c.label) },
            }}>
              <Button size="small" type="text" loading={busy} style={{ color: 'var(--rs-text-secondary)' }}>Mute <DownOutlined style={{ fontSize: 10 }} /></Button>
            </Dropdown>
            <RowActions
              primary={{ key: 'hooks', label: 'Hooks…', icon: <ApiOutlined />, onClick: () => onHooks(v) }}
              more={[{
                key: 'unfollow', label: 'Stop following this host', danger: true, onClick: () => { void stopFollowing() },
                confirm: {
                  title: 'Stop following webhooks?',
                  what: <span style={{ fontFamily: MONO_FONT }}>{v.host}</span>,
                  consequence: 'This console stops showing its deliveries and approvals. The host keeps taking them.',
                  okText: 'Stop following',
                },
              }]}
            />
          </div>
        )}
      </div>
      {conn.detail && conn.tone !== 'ok' && (
        <div style={{ fontSize: 12, color: conn.tone === 'failed' ? textWarning(isDark) : textSecondary(isDark) }}>{conn.detail}</div>
      )}
      {last ? (
        <>
          <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: 0, border: 'none', background: 'none', cursor: 'pointer', color: textSecondary(isDark), fontSize: 12, textAlign: 'left' }}>
            {open ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
            <span>Last delivery {relTime(last.received)}: {last.hook}, {deliveryStatus(last).label}</span>
            {!open && v.recent.length > 1 && <span style={{ color: textMuted(isDark) }}>· {v.recent.length} recent</span>}
          </button>
          {open && (
            <ul style={{ listStyle: 'none', margin: 0, padding: '0 0 0 16px', display: 'flex', flexDirection: 'column', gap: 2 }}>
              {v.recent.map(d => {
                const st = deliveryStatus(d)
                const line = runLine(d)
                return (
                  <li key={d.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, flexWrap: 'wrap' }}>
                    <StatusTag tone={st.tone}>{st.label}</StatusTag>
                    <span style={{ color: textColor(isDark), fontWeight: 500 }}>{d.hook}</span>
                    {line && <span style={{ fontFamily: MONO_FONT, color: textSecondary(isDark) }}>{line}</span>}
                    {!line && d.reason && <span style={{ color: textSecondary(isDark) }}>{d.reason}</span>}
                    <span style={{ color: textMuted(isDark) }}>
                      {[relTime(d.received), d.decided_by ? `${d.state === 'denied' ? 'denied' : 'approved'} by ${d.decided_by}` : ''].filter(Boolean).join(' · ')}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </>
      ) : conn.tone === 'ok' && (
        <div style={{ fontSize: 12, color: textMuted(isDark) }}>Nothing received yet.</div>
      )}
    </div>
  )
}

export function WebhooksSection({ status, venvs, editable }: { status: WebhooksStatus; venvs: SchedulerVenv[]; editable: boolean }) {
  const isDark = useIsDark()
  const [editing, setEditing] = useState<{ host: string; venv: string } | null>(null)
  const [following, setFollowing] = useState('')
  const offers = editable ? followOffers(venvs, status) : []
  const setupable = editable ? setupTargets(venvs, status) : []
  if (!status.venvs.length && !offers.length && !setupable.length) return null

  const follow = async (host: string) => {
    setFollowing(host)
    try {
      const r = await bridge.webhook_follow({ host, hooks: ['*'] })
      if (!r.ok) message.error(`Couldn't follow webhooks on ${host}: ${r.error}`)
    } finally { setFollowing('') }
  }

  return (
    <section aria-label="Webhooks" data-testid="webhooks-section" style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
      {status.away && (
        <div style={{ fontSize: 12, color: textSecondary(isDark) }}>
          You're away ({status.away}): webhook alerts are muted until you're back. Deliveries still wait for anyone else following.
        </div>
      )}
      {status.venvs.map(v => (
        <WebhookStrip key={v.key} v={v} editable={editable} onHooks={x => setEditing({ host: x.host, venv: x.venv })} />
      ))}
      {(offers.length > 0 || setupable.length > 0) && (
        <div data-testid="webhook-follow-offer" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12, color: textSecondary(isDark) }}>
          {offers.length > 0 && <span>Takes webhooks, not followed here:</span>}
          {offers.map(h => (
            <Button key={h} size="small" loading={following === h} disabled={!!following} onClick={() => { void follow(h) }}
              aria-label={`Follow webhooks on ${h}`}>
              Follow <span style={{ fontFamily: MONO_FONT, marginLeft: 4 }}>{h}</span>
            </Button>
          ))}
          {setupable.length > 0 && (
            <Dropdown trigger={['click']} menu={{
              items: setupable.map(t => ({ key: `${t.host}/${t.venv}`, label: <span style={{ fontFamily: MONO_FONT }}>{t.host} / {t.venv}</span> })),
              onClick: ({ key }) => { const t = setupable.find(x => `${x.host}/${x.venv}` === key); if (t) setEditing(t) },
            }}>
              <Button size="small" type="text" style={{ color: 'var(--rs-text-secondary)' }}>
                Set up webhooks on… <DownOutlined style={{ fontSize: 10 }} />
              </Button>
            </Dropdown>
          )}
        </div>
      )}
      {editing && <WebhookHooksModal target={editing} onClose={() => setEditing(null)} />}
    </section>
  )
}
