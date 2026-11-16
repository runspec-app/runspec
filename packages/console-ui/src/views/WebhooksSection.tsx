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
import { useState, type ReactNode } from 'react'
import { Button, Dropdown, Input, Select, Switch, Tag, Tooltip, message } from 'antd'
import { DownOutlined, RightOutlined, ApiOutlined, BellOutlined, PlusOutlined, PoweroffOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import type { DeprovisionTarget, WebhookVenv, WebhooksStatus } from '../bridge'
import { UpdateVenvModal } from './UpdateVenvModal'
import { useIsDark, textColor, textMuted, textSecondary, textWarning, MONO_FONT, RADIUS } from '../ThemeContext'
import { StatusTag, RowActions } from '../components/kit'
import { connection, deliveryStatus, muteLabel, relTime, runLine } from '../webhooks'
import type { SchedulerVenv } from './allSchedules'
import { WebhookHooksModal, suggestPort } from './WebhookHooksModal'

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
 *  its own, only a line (Turn on, Hooks…, Settings…) in its host's "not
 *  listening" row. */
export function isOffQuietly(v: WebhookVenv): boolean {
  return v.connected && !v.unavailable && v.settings?.enabled === false && !v.open.length && !v.recent.length
}

/** The quiet line under a strip when following it can't bring anything yet. */
export function stripHint(v: WebhookVenv): { text: string; action: string; tab: ModalTab } | null {
  if (!v.connected || v.settings?.enabled === false) return null
  if (!v.hooks.length) return { text: 'No hooks in this venv yet.', action: 'Add a hook…', tab: 'hooks' }
  return null
}

/** The port Turn on saves: the one the venv already has, else the first one
 *  no other venv on the host uses. */
export function turnOnPort(current: number | undefined, taken: number[]): number {
  return current && current > 0 ? current : suggestPort(taken)
}

/** Switch a venv's listener on in one save (Turn on used to only fill in the
 *  form, which looked on while the host stayed off). True when it saved. */
export async function turnOnListener(host: string, venv: string, current: number | undefined, taken: number[]): Promise<boolean> {
  const port = turnOnPort(current, taken)
  const r = await bridge.webhook_settings({ host, venv, hooks: { enabled: true, port } })
  if (!r.ok) {
    message.error(`Couldn't turn on webhooks on ${host} / ${venv}: ${r.error || 'no answer from the host'}.`)
    return false
  }
  const problem = r.settings?.problem
  if (problem) message.warning(`Saved on ${host} / ${venv}, but: ${problem}.`)
  else message.success(`Webhooks on for ${host} / ${venv}, port ${port}. Its scheduler daemon starts listening by itself; add a hook next.`)
  return true
}

async function stopFollowing(host: string): Promise<void> {
  const r = await bridge.webhook_follow({ host, hooks: [] })
  if (!r.ok) message.error(`Couldn't stop following ${host}: ${r.error}`)
}

function unfollowAction(host: string) {
  return {
    key: 'unfollow', label: 'Stop following this host', danger: true, onClick: () => { void stopFollowing(host) },
    confirm: {
      title: 'Stop following webhooks?',
      what: <span style={{ fontFamily: MONO_FONT }}>{host}</span>,
      consequence: 'This console stops showing its deliveries and approvals. The host keeps taking them.',
      okText: 'Stop following',
    },
  }
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
              more={[unfollowAction(v.host)]}
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
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 0', minHeight: 24, border: 'none', background: 'none', cursor: 'pointer', color: textSecondary(isDark), fontSize: 12, textAlign: 'left' }}>
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

/** The listener ports the other venvs on `host` are set to, from what the
 *  follower and the scheduler reads know, so a new one is picked clear of them. */
export function portsOnHost(status: WebhooksStatus, venvs: SchedulerVenv[], host: string, venv: string): number[] {
  const ports = [
    ...status.venvs.filter(v => v.host === host && v.venv !== venv).map(v => v.settings?.port ?? 0),
    ...venvs.filter(v => v.host === host && v.venv !== venv).map(v => v.webhooks?.port ?? 0),
  ]
  return [...new Set(ports.filter(p => p > 0))].sort((a, b) => a - b)
}

/** Why a followed venv takes no webhooks, in a couple of words. */
export function unavailableText(v: WebhookVenv): string {
  return /root-owned/i.test(v.unavailable ?? '') ? 'root-owned' : 'needs runspec-scheduler 0.11.0'
}

/** A followed host whose venvs aren't listening: the ones with webhooks off
 *  (can be turned on) and the ones that take no webhooks at all. */
export interface QuietHost { host: string; off: WebhookVenv[]; unavailable: WebhookVenv[] }

/** Followed venvs with nothing to show, one entry per host, sorted: hosts
 *  with a venv that can be turned on first. */
export function quietHosts(off: WebhookVenv[], unavailable: WebhookVenv[]): QuietHost[] {
  const by = new Map<string, QuietHost>()
  const get = (host: string) => by.get(host) ?? by.set(host, { host, off: [], unavailable: [] }).get(host)!
  for (const v of off) get(v.host).off.push(v)
  for (const v of unavailable) get(v.host).unavailable.push(v)
  return [...by.values()].sort((a, b) => Number(!!b.off.length) - Number(!!a.off.length) || a.host.localeCompare(b.host))
}

/** "2 hosts, 5 venvs". */
export function quietCount(hosts: QuietHost[]): string {
  const venvs = hosts.reduce((n, h) => n + h.off.length + h.unavailable.length, 0)
  return `${hosts.length} host${hosts.length === 1 ? '' : 's'}, ${venvs} venv${venvs === 1 ? '' : 's'}`
}

type QuietActions = {
  editable: boolean
  /** The key of the venv being turned on, '' when none. */
  turning: string
  onTurnOn: (v: WebhookVenv) => void
  onOpen: (v: WebhookVenv, tab: ModalTab) => void
}

/** Turn on / Hooks… / Settings… for one venv, each named for the venv so a
 *  screen reader can tell a host's rows apart. */
function VenvButtons({ host, venv, busy, disabled, onTurnOn, onOpen }: {
  host: string; venv: string; busy: boolean; disabled: boolean
  onTurnOn: () => void; onOpen: (tab: ModalTab) => void
}) {
  const where = `${host} / ${venv}`
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
      <Button size="small" icon={<PoweroffOutlined />} loading={busy} disabled={disabled && !busy}
        aria-label={`Turn on webhooks on ${where}`} onClick={onTurnOn}>
        Turn on
      </Button>
      <Button size="small" type="text" icon={<ApiOutlined />} aria-label={`Hooks on ${where}`} onClick={() => onOpen('hooks')}
        style={{ color: 'var(--rs-text-secondary)' }}>
        Hooks…
      </Button>
      <Button size="small" type="text" icon={<SettingOutlined />} aria-label={`Listener settings on ${where}`} onClick={() => onOpen('settings')}
        style={{ color: 'var(--rs-text-secondary)' }}>
        Settings…
      </Button>
    </span>
  )
}

/** One followed host whose venvs aren't listening: a line per venv with its
 *  listener off (Turn on, Hooks…, Settings…), a muted note for each venv that
 *  can't take webhooks, and Stop following in the host's More menu. */
function QuietHostRow({ q, editable, turning, onTurnOn, onOpen }: { q: QuietHost } & QuietActions) {
  const isDark = useIsDark()
  return (
    <div data-testid="webhook-off-host" style={{
      display: 'flex', flexDirection: 'column', gap: 4, padding: '4px 10px',
      border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, background: 'var(--rs-surface-raised)', fontSize: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <StatusTag tone="neutral">{q.off.length ? 'webhooks off' : 'no webhooks'}</StatusTag>
        <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{q.host}</span>
        {q.unavailable.map(v => (
          <Tooltip key={v.key} title={v.unavailable}>
            <span data-testid="webhook-unavailable" style={{ color: textMuted(isDark) }}>
              <span style={{ fontFamily: MONO_FONT }}>{v.venv}</span> ({unavailableText(v)})
            </span>
          </Tooltip>
        ))}
        {editable && <RowActions style={{ marginLeft: 'auto' }} more={[unfollowAction(q.host)]} />}
      </div>
      {q.off.map(v => (
        <div key={v.key} data-testid="webhook-off-venv" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', paddingLeft: 8 }}>
          <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{v.venv}</span>
          <span style={{ color: textSecondary(isDark) }}>listener off{v.settings?.port ? `, port ${v.settings.port}` : ''}</span>
          {editable && (
            <span style={{ marginLeft: 'auto' }}>
              <VenvButtons host={v.host} venv={v.venv} busy={turning === v.key} disabled={!!turning}
                onTurnOn={() => onTurnOn(v)} onOpen={tab => onOpen(v, tab)} />
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

/** Followed venvs that aren't listening, one row per host under a heading,
 *  folded behind a toggle when there are many hosts. */
function QuietHosts({ hosts, ...actions }: { hosts: QuietHost[] } & QuietActions) {
  const isDark = useIsDark()
  const [expanded, setExpanded] = useState<boolean | null>(null)
  const show = expanded ?? hosts.length <= HOSTS_SHOWN
  const canTurnOn = hosts.some(h => h.off.length)
  return (
    <div data-testid="webhook-off" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <h3 style={{ margin: '6px 0 0', fontSize: 13, fontWeight: 600, color: textColor(isDark), display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        Followed, not listening
        <span style={{ fontSize: 12, fontWeight: 400, color: textMuted(isDark) }}>{quietCount(hosts)}</span>
        {hosts.length > HOSTS_SHOWN && (
          <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12, fontWeight: 400 }} aria-expanded={show}
            onClick={() => setExpanded(!show)}>
            {show ? 'Hide' : 'Show'}
          </Button>
        )}
      </h3>
      {canTurnOn && actions.editable && (
        <div style={{ fontSize: 12, color: textSecondary(isDark) }}>
          Turn on switches a venv's listener on with a free port. Its scheduler daemon must be running; then add a hook.
        </div>
      )}
      {show && hosts.map(q => <QuietHostRow key={q.host} q={q} {...actions} />)}
    </div>
  )
}

/** Which venvs of unfollowed hosts to list. */
export type OfferState = 'all' | VenvHookState
export interface OfferFilters { q: string; state: OfferState; followable: boolean }
export const NO_OFFER_FILTERS: OfferFilters = { q: '', state: 'all', followable: false }

/** Narrow the unfollowed hosts: every word of `q` must appear in the host, its
 *  group or the venv (a host or group hit keeps all its venvs), `state` keeps
 *  venvs in that webhook state, `followable` keeps hosts Follow would work on.
 *  A host with no venv left drops out. */
export function filterOffers(offers: HostOffer[], f: OfferFilters): HostOffer[] {
  const words = f.q.toLowerCase().split(/\s+/).filter(Boolean)
  const out: HostOffer[] = []
  for (const o of offers) {
    if (f.followable && !o.canFollow) continue
    const venvs = o.venvs.filter(v => {
      if (f.state !== 'all' && venvHookState(v) !== f.state) return false
      const hay = `${v.host} ${v.hostGroup} ${v.venv}`.toLowerCase()
      return words.every(w => hay.includes(w))
    })
    if (venvs.length) out.push({ ...o, venvs })
  }
  return out
}

const OFFER_STATE_LABEL: Record<'off' | 'empty' | 'on' | 'old', string> = {
  off: 'Webhooks off',
  empty: 'On, no hooks yet',
  on: 'On, with hooks',
  old: 'Needs runspec-scheduler 0.11.0',
}

/** Host, venv, webhook state, then each venv action (Turn on or Add a hook,
 *  Hooks, Settings) and Follow in a column of its own, so every button sits
 *  in the same place on every line however many hosts and venvs there are. */
const OFFER_COLUMNS = 'minmax(120px, max-content) minmax(90px, max-content) minmax(160px, max-content) repeat(3, max-content) minmax(max-content, 1fr)'

type OfferRowProps = {
  editable: boolean; busy: string; turning: string
  onFollow: (host: string) => void; onEdit: (t: { host: string; venv: string; tab: ModalTab }) => void
  onTurnOn: (v: SchedulerVenv) => void
  /** Opens Update venv for a venv whose runspec-scheduler is too old. */
  onUpdate?: (v: SchedulerVenv) => void
}

/** One host this console doesn't follow: a row per venv (its webhook state
 *  and a way to set it up), and Follow, which works before any hook exists.
 *  The host's rows are one rowgroup; each row names its host (visibly on the
 *  first, for screen readers on the rest) so every row reads on its own. */
function HostOfferRow({ offer, editable, busy, turning, onFollow, onEdit, onTurnOn, onUpdate }: { offer: HostOffer } & OfferRowProps) {
  const isDark = useIsDark()
  const { host, venvs, canFollow } = offer
  const span = `span ${venvs.length}`
  const group = venvs[0]?.hostGroup
  return (
    <div data-testid="webhook-host-offer" role="rowgroup" style={{
      gridColumn: '1 / -1', display: 'grid', gridTemplateColumns: 'subgrid', alignItems: 'center',
      columnGap: 12, rowGap: 2, padding: '5px 10px', borderTop: '1px solid var(--rs-border)', fontSize: 12,
    }}>
      {venvs.map((v, i) => {
        const st = venvHookState(v)
        const row = i + 1
        const where = `${host} / ${v.venv}`
        const quiet = { color: 'var(--rs-text-secondary)' }
        const cell = (col: number, key: string, node: ReactNode) => (
          <span key={key} role="cell" style={{ gridColumn: col, gridRow: row, minHeight: 24, display: 'flex', alignItems: 'center' }}>{node}</span>
        )
        const canSet = editable && st !== 'old'
        return (
          // `contents`: the row's cells sit straight in the host's grid, so the
          // columns line up across hosts.
          <div key={v.key} role="row" style={{ display: 'contents' }}>
            {i === 0 ? (
              <div role="rowheader" style={{ gridColumn: 1, gridRow: `1 / ${span}`, alignSelf: 'start', minWidth: 0, paddingTop: 2 }}>
                <div style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark), overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{host}</div>
                {group && <div style={{ color: textMuted(isDark) }}>{group}</div>}
              </div>
            ) : (
              <span role="rowheader" className="rs-visually-hidden">{host}</span>
            )}
            <span role="cell" style={{ gridColumn: 2, gridRow: row, fontFamily: MONO_FONT, color: textSecondary(isDark) }}>{v.venv}</span>
            <span role="cell" style={{ gridColumn: 3, gridRow: row, color: st === 'old' ? textMuted(isDark) : textSecondary(isDark) }}>
              {hookStateText(v)}
              {st === 'old' && editable && onUpdate && (
                <>
                  {' · '}
                  <Button type="link" size="small" style={{ padding: 0, height: 24, fontSize: 12 }}
                    aria-label={`Update venv ${where}`} onClick={() => onUpdate(v)}>Update venv…</Button>
                </>
              )}
            </span>
            {cell(4, 'first', editable && (st === 'off'
              ? <Button size="small" icon={<PoweroffOutlined />} loading={turning === v.key} disabled={!!turning && turning !== v.key}
                  aria-label={`Turn on webhooks on ${where}`} onClick={() => onTurnOn(v)}>Turn on</Button>
              : st === 'empty'
                ? <Button size="small" type="text" icon={<PlusOutlined />} style={quiet}
                    aria-label={`Add a hook on ${where}`} onClick={() => onEdit({ host, venv: v.venv, tab: 'hooks' })}>Add a hook…</Button>
                : null))}
            {cell(5, 'hooks', canSet && st !== 'empty' && (
              <Button size="small" type="text" icon={<ApiOutlined />} style={quiet}
                aria-label={`Hooks on ${where}`} onClick={() => onEdit({ host, venv: v.venv, tab: 'hooks' })}>Hooks…</Button>
            ))}
            {cell(6, 'settings', canSet && (
              <Button size="small" type="text" icon={<SettingOutlined />} style={quiet}
                aria-label={`Listener settings on ${where}`} onClick={() => onEdit({ host, venv: v.venv, tab: 'settings' })}>Settings…</Button>
            ))}
            {i === 0 ? (
              <div role="cell" style={{ gridColumn: 7, gridRow: `1 / ${span}`, alignSelf: 'start', justifySelf: 'end' }}>
                {editable && (
                  <Tooltip title={canFollow
                    ? 'Show its webhook deliveries and approvals on this console. Works before any hook is made.'
                    : 'Needs runspec-scheduler 0.11.0 or later in one of its venvs: use Update venv… on that venv’s line.'}>
                    <Button size="small" disabled={!canFollow || !!busy} loading={busy === host}
                      onClick={() => onFollow(host)} aria-label={`Follow webhooks on ${host}`}>
                      Follow
                    </Button>
                  </Tooltip>
                )}
              </div>
            ) : (
              <span role="cell" className="rs-visually-hidden">{canFollow ? 'Follow is on the host’s first line' : ''}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** Unfollowed hosts are filterable once there are more than this many. */
export const OFFER_FILTER_AT = 5

/** The unfollowed hosts as one aligned grid, with filters when there are many. */
function HostOffers({ offers, ...row }: { offers: HostOffer[] } & OfferRowProps) {
  const isDark = useIsDark()
  const [f, setF] = useState<OfferFilters>(NO_OFFER_FILTERS)
  const shown = filterOffers(offers, f)
  const filtered = f.q !== '' || f.state !== 'all' || f.followable
  const venvs = offers.flatMap(o => o.venvs)
  const stateOptions = (Object.keys(OFFER_STATE_LABEL) as (keyof typeof OFFER_STATE_LABEL)[])
    .map(k => ({ k, n: venvs.filter(v => venvHookState(v) === k).length }))
    .filter(o => o.n > 0)
  const someCantFollow = offers.some(o => !o.canFollow)
  const head = { fontSize: 12, color: textMuted(isDark), fontWeight: 500 }
  return (
    <div data-testid="webhook-offers" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {offers.length > OFFER_FILTER_AT && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Input size="small" allowClear prefix={<SearchOutlined style={{ color: textMuted(isDark) }} />}
            placeholder="Host, venv or group" aria-label="Filter hosts by host, venv or group"
            value={f.q} onChange={e => setF({ ...f, q: e.target.value })} style={{ width: 220 }} />
          <Select size="small" aria-label="Filter by webhook state" value={f.state} style={{ minWidth: 200 }} popupMatchSelectWidth={false}
            onChange={(state: OfferState) => setF({ ...f, state })}
            options={[
              { value: 'all', label: 'Any webhook state' },
              ...stateOptions.map(o => ({ value: o.k, label: `${OFFER_STATE_LABEL[o.k]} (${o.n})` })),
            ]} />
          {someCantFollow && (
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: textSecondary(isDark) }}>
              <Switch size="small" checked={f.followable} aria-label="Only hosts that can be followed"
                onChange={followable => setF({ ...f, followable })} />
              Only hosts that can be followed
            </label>
          )}
          <span style={{ fontSize: 12, color: textMuted(isDark) }} aria-live="polite">
            {filtered ? `Showing ${shown.length} of ${offers.length} hosts` : `${offers.length} hosts, ${venvs.length} venvs`}
          </span>
          {filtered && (
            <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }}
              onClick={() => setF(NO_OFFER_FILTERS)}>Clear filters</Button>
          )}
        </div>
      )}
      {shown.length > 0 ? (
        <div role="table" aria-label="Hosts not followed here" style={{
          display: 'grid', gridTemplateColumns: OFFER_COLUMNS, overflowX: 'auto',
          border: '1px dashed var(--rs-border)', borderRadius: RADIUS.sm,
        }}>
          <div role="row" style={{ gridColumn: '1 / -1', display: 'grid', gridTemplateColumns: 'subgrid', columnGap: 12, padding: '4px 10px' }}>
            <span role="columnheader" style={head}>Host</span>
            <span role="columnheader" style={head}>Venv</span>
            <span role="columnheader" style={head}>Webhooks</span>
            <span role="columnheader" style={head}><span className="rs-visually-hidden">Set up</span></span>
            <span role="columnheader" style={head}><span className="rs-visually-hidden">Hooks</span></span>
            <span role="columnheader" style={head}><span className="rs-visually-hidden">Listener settings</span></span>
            <span role="columnheader" style={head}><span className="rs-visually-hidden">Follow</span></span>
          </div>
          {shown.map(o => <HostOfferRow key={o.host} offer={o} {...row} />)}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: textSecondary(isDark) }}>No host matches these filters.</div>
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
  const [turning, setTurning] = useState('')
  const [updating, setUpdating] = useState<DeprovisionTarget[] | null>(null)
  const [expanded, setExpanded] = useState<boolean | null>(null)
  const offers = hostOffers(venvs, status)
  const live = status.venvs.filter(v => !v.unavailable && !isOffQuietly(v))
  const off = status.venvs.filter(isOffQuietly)
  const quiet = status.venvs.filter(v => !!v.unavailable)
  const quietList = quietHosts(off, quiet)
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

  const turnOn = async (key: string, host: string, venv: string, port: number | undefined) => {
    setTurning(key)
    try {
      if (await turnOnListener(host, venv, port, portsOnHost(status, venvs, host, venv))) onChanged?.()
    } finally { setTurning('') }
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
      {quietList.length > 0 && (
        <QuietHosts hosts={quietList} editable={editable} turning={turning}
          onTurnOn={v => { void turnOn(v.key, v.host, v.venv, v.settings?.port) }}
          onOpen={(v, tab) => setEditing({ host: v.host, venv: v.venv, tab })} />
      )}
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
      {showOffers && (
        <HostOffers offers={offers} editable={editable} busy={following} turning={turning}
          onFollow={h => { void follow(h) }} onEdit={setEditing}
          onTurnOn={v => { void turnOn(v.key, v.host, v.venv, v.webhooks?.port) }}
          onUpdate={v => setUpdating([{ host: v.host, runspec_path: v.venv }])} />
      )}
      <UpdateVenvModal open={updating !== null} initialTargets={updating ?? undefined}
        onClose={() => setUpdating(null)} onDone={() => onChanged?.()} />
      {editing && <WebhookHooksModal target={{ host: editing.host, venv: editing.venv }} startTab={editing.tab}
        takenPorts={portsOnHost(status, venvs, editing.host, editing.venv)} onClose={() => { setEditing(null); onChanged?.() }} />}
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
