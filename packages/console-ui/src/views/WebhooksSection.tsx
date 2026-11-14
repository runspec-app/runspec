/**
 * WebhooksSection — the Schedules tab's view of the webhooks this console
 * follows: one strip per followed host + venv (connection, mutes, recent
 * deliveries, the hook editor), one quiet line for followed venvs that take no
 * webhooks (no runspec-scheduler 0.11.0, root-owned), and one row per host it
 * doesn't follow saying what each venv would need — Follow works before any
 * hook is made.
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

/** What one venv's runspec-scheduler says about webhooks: `on` (listener on,
 *  hooks made), `empty` (on, no hook yet), `off` (`[hooks] enabled = false`),
 *  `old` (runspec-scheduler older than 0.11.0), `missing` (no runspec-scheduler). */
export type VenvHookState = 'on' | 'empty' | 'off' | 'old' | 'missing'

export function venvHookState(v: SchedulerVenv | undefined): VenvHookState {
  if (!v) return 'missing'
  if (!v.webhooks) return 'old'
  if (!v.webhooks.enabled) return 'off'
  return v.webhooks.hooks.length ? 'on' : 'empty'
}

/** A venv's webhook state in a few words. */
export function hookStateText(v: SchedulerVenv): string {
  const st = venvHookState(v)
  if (st === 'on') {
    const n = v.webhooks?.hooks.length ?? 0
    return `webhooks on, ${n} hook${n === 1 ? '' : 's'}`
  }
  if (st === 'empty') return 'webhooks on, no hooks yet'
  if (st === 'off') return 'webhooks off'
  return 'needs runspec-scheduler 0.11.0'
}

/** A host this console doesn't follow: its venvs with runspec-scheduler, and
 *  whether following would get anything (some venv has 0.11.0 or later, even
 *  with webhooks still off or no hook made yet). */
export interface HostOffer { host: string; venvs: SchedulerVenv[]; canFollow: boolean }

/** Every host with a runspec-scheduler venv that this console doesn't follow,
 *  sorted: the ones it can follow first. */
export function hostOffers(venvs: SchedulerVenv[], status: WebhooksStatus): HostOffer[] {
  const by = new Map<string, SchedulerVenv[]>()
  for (const v of venvs) {
    if (status.follows[v.host]?.length) continue
    by.set(v.host, [...(by.get(v.host) ?? []), v])
  }
  return [...by.entries()]
    .map(([host, vs]) => ({ host, venvs: vs, canFollow: vs.some(v => !!v.webhooks) }))
    .sort((a, b) => Number(b.canFollow) - Number(a.canFollow) || a.host.localeCompare(b.host))
}

/** Hosts listed before the rest fold behind a "N hosts not followed" toggle. */
export const HOSTS_SHOWN = 8

/** "follows every hook" / "follows alerts, deploys". */
export function followsText(follows: string[]): string {
  return follows.includes('*') ? 'follows every hook' : `follows ${follows.join(', ')}`
}

type ModalTab = 'hooks' | 'settings'

/** A followed venv with webhooks switched off and nothing to show: a normal
 *  state (not every venv on a host takes webhooks), so it gets no strip of
 *  its own, only a name on the quiet "Webhooks off here" line. */
export function isOffQuietly(v: WebhookVenv): boolean {
  return v.connected && !v.unavailable && v.settings?.enabled === false && !v.open.length && !v.recent.length
}

/** The quiet line under a strip when following it can't bring anything yet. */
export function stripHint(v: WebhookVenv): { text: string; action: string; tab: ModalTab } | null {
  if (!v.connected || v.settings?.enabled === false) return null
  if (!v.hooks.length) return { text: 'No hooks in this venv yet.', action: 'Add a hook…', tab: 'hooks' }
  return null
}

function WebhookStrip({ v, editable, onHooks }: { v: WebhookVenv; editable: boolean; onHooks: (v: WebhookVenv, tab?: ModalTab) => void }) {
  const isDark = useIsDark()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const conn = connection(v)
  const target = { host: v.host, venv: v.venv }
  const waiting = v.open.filter(d => d.state === 'pending').length
  const last = v.recent[0]
  const hint = stripHint(v)

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
      {hint && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: textSecondary(isDark) }}>
          <span>{hint.text}</span>
          {editable && (
            <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => onHooks(v, hint.tab)}>
              {hint.action}
            </Button>
          )}
        </div>
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
      ) : conn.tone === 'ok' && !hint && (
        <div style={{ fontSize: 12, color: textMuted(isDark) }}>Nothing received yet.</div>
      )}
    </div>
  )
}

/** Followed venvs that take no webhooks at all, folded into one quiet line. */
function UnavailableLine({ venvs }: { venvs: WebhookVenv[] }) {
  const isDark = useIsDark()
  return (
    <div data-testid="webhook-unavailable" style={{ fontSize: 12, color: textMuted(isDark), display: 'flex', flexWrap: 'wrap', gap: 4 }}>
      <span>Not taking webhooks:</span>
      {venvs.map((v, i) => (
        <Tooltip key={v.key} title={v.unavailable}>
          <span style={{ fontFamily: MONO_FONT }}>
            {v.host} / {v.venv}
            <span style={{ fontFamily: 'inherit' }}> ({/root-owned/i.test(v.unavailable ?? '') ? 'root-owned' : 'needs runspec-scheduler 0.11.0'})</span>
            {i < venvs.length - 1 ? ',' : ''}
          </span>
        </Tooltip>
      ))}
    </div>
  )
}

/** Followed venvs with webhooks switched off, as one quiet line. */
function OffLine({ venvs, editable, onTurnOn }: { venvs: WebhookVenv[]; editable: boolean; onTurnOn: (v: WebhookVenv) => void }) {
  const isDark = useIsDark()
  return (
    <div data-testid="webhook-off" style={{ fontSize: 12, color: textMuted(isDark), display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: 4 }}>
      <span>Webhooks off here:</span>
      {venvs.map((v, i) => (
        <span key={v.key} style={{ fontFamily: MONO_FONT }}>
          {v.host} / {v.venv}
          {editable && (
            <Button size="small" type="link" style={{ padding: '0 0 0 4px', height: 'auto', fontSize: 12, fontFamily: 'inherit' }}
              aria-label={`Turn on webhooks on ${v.host} / ${v.venv}`} onClick={() => onTurnOn(v)}>turn on</Button>
          )}
          {i < venvs.length - 1 ? ',' : ''}
        </span>
      ))}
    </div>
  )
}

/** One host this console doesn't follow: each venv's webhook state, a way to
 *  set them up, and Follow (works before any hook exists). */
function HostOfferRow({ offer, editable, busy, onFollow, onEdit }: {
  offer: HostOffer; editable: boolean; busy: string
  onFollow: (host: string) => void; onEdit: (t: { host: string; venv: string; tab: ModalTab }) => void
}) {
  const isDark = useIsDark()
  const { host, venvs, canFollow } = offer
  return (
    <div data-testid="webhook-host-offer" style={{
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '4px 10px',
      border: '1px dashed var(--rs-border)', borderRadius: RADIUS.sm, fontSize: 12,
    }}>
      <StatusTag tone="neutral">not followed</StatusTag>
      <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{host}</span>
      {venvs.map(v => {
        const st = venvHookState(v)
        const action = st === 'off' ? { label: 'Set up…', tab: 'settings' as const }
          : st === 'empty' ? { label: 'Add a hook…', tab: 'hooks' as const }
          : st === 'on' ? { label: 'Hooks…', tab: 'hooks' as const } : null
        return (
          <span key={v.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: st === 'old' ? textMuted(isDark) : textSecondary(isDark) }}>
            <span style={{ fontFamily: MONO_FONT }}>{v.venv}</span>
            <span>{hookStateText(v)}</span>
            {editable && action && (
              <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }}
                aria-label={`${action.label.replace('…', '')} on ${host} / ${v.venv}`}
                onClick={() => onEdit({ host, venv: v.venv, tab: action.tab })}>
                {action.label}
              </Button>
            )}
          </span>
        )
      })}
      {editable && (
        <Tooltip title={canFollow
          ? 'Show its webhook deliveries and approvals on this console. Works before any hook is made.'
          : 'Needs runspec-scheduler 0.11.0 or later in one of its venvs (Update venv… upgrades it).'}>
          <Button size="small" style={{ marginLeft: 'auto' }} disabled={!canFollow || !!busy} loading={busy === host}
            onClick={() => onFollow(host)} aria-label={`Follow webhooks on ${host}`}>
            Follow
          </Button>
        </Tooltip>
      )}
    </div>
  )
}

export function WebhooksSection({ status, venvs, editable, onChanged }: {
  status: WebhooksStatus; venvs: SchedulerVenv[]; editable: boolean
  /** A hook or listener setting may have changed on a host: re-read the venvs. */
  onChanged?: () => void
}) {
  const isDark = useIsDark()
  const [editing, setEditing] = useState<{ host: string; venv: string; tab?: ModalTab } | null>(null)
  const [following, setFollowing] = useState('')
  const [expanded, setExpanded] = useState<boolean | null>(null)
  const offers = hostOffers(venvs, status)
  const live = status.venvs.filter(v => !v.unavailable && !isOffQuietly(v))
  const off = status.venvs.filter(isOffQuietly)
  const quiet = status.venvs.filter(v => !!v.unavailable)
  if (!status.venvs.length && !offers.length) return null
  const showOffers = expanded ?? offers.length <= HOSTS_SHOWN
  const followable = offers.filter(o => o.canFollow).length

  const follow = async (host: string) => {
    setFollowing(host)
    try {
      const r = await bridge.webhook_follow({ host, hooks: ['*'] })
      if (!r.ok) message.error(`Couldn't follow webhooks on ${host}: ${r.error}`)
      else message.success(`Following webhooks on ${host}.`)
    } finally { setFollowing('') }
  }

  return (
    <section aria-label="Webhooks" data-testid="webhooks-section" style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
      {status.away && (
        <div style={{ fontSize: 12, color: textSecondary(isDark) }}>
          You're away ({status.away}): webhook alerts are muted until you're back. Deliveries still wait for anyone else following.
        </div>
      )}
      {live.map(v => (
        <WebhookStrip key={v.key} v={v} editable={editable} onHooks={(x, tab) => setEditing({ host: x.host, venv: x.venv, tab })} />
      ))}
      {off.length > 0 && <OffLine venvs={off} editable={editable} onTurnOn={v => setEditing({ host: v.host, venv: v.venv, tab: 'settings' })} />}
      {quiet.length > 0 && <UnavailableLine venvs={quiet} />}
      {offers.length > 0 && (
        <h3 style={{ margin: '6px 0 0', fontSize: 13, fontWeight: 600, color: textColor(isDark), display: 'flex', alignItems: 'baseline', gap: 8 }}>
          Not followed here
          {offers.length > HOSTS_SHOWN && (
            <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12, fontWeight: 400 }} aria-expanded={showOffers}
              onClick={() => setExpanded(!showOffers)}>
              {showOffers ? 'Hide' : `Show ${offers.length} hosts${followable ? ` (${followable} can be followed)` : ''}`}
            </Button>
          )}
        </h3>
      )}
      {showOffers && offers.map(o => (
        <HostOfferRow key={o.host} offer={o} editable={editable} busy={following}
          onFollow={h => { void follow(h) }} onEdit={setEditing} />
      ))}
      {editing && <WebhookHooksModal target={{ host: editing.host, venv: editing.venv }} startTab={editing.tab} onClose={() => { setEditing(null); onChanged?.() }} />}
    </section>
  )
}

/** Counts for the Webhooks tab and the one-line summary on the Schedules tab. */
export function webhookCounts(status: WebhooksStatus): { hosts: number; waiting: number; problems: number } {
  return {
    hosts: Object.keys(status.follows).length,
    waiting: status.venvs.reduce((n, v) => n + v.open.filter(d => d.state === 'pending').length, 0),
    problems: status.venvs.filter(v => !v.unavailable && !v.connected && !!v.error).length,
  }
}

/** One line on the Schedules tab when this console follows any webhooks. */
export function WebhooksSummary({ status, onOpen }: { status: WebhooksStatus; onOpen?: () => void }) {
  const isDark = useIsDark()
  const { hosts, waiting, problems } = webhookCounts(status)
  if (!hosts) return null
  return (
    <div data-testid="webhooks-summary" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12, color: textSecondary(isDark), marginBottom: 10 }}>
      <ApiOutlined />
      <span>Webhooks: following {hosts} host{hosts === 1 ? '' : 's'}</span>
      {waiting > 0 && <StatusTag tone="waiting">{waiting} waiting</StatusTag>}
      {problems > 0 && <StatusTag tone="failed">{problems} not connected</StatusTag>}
      {onOpen && <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={onOpen}>Open Webhooks</Button>}
    </div>
  )
}
