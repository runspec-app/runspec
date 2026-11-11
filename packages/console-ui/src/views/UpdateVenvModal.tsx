import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Checkbox, Select, Tag, Alert, Spin, Tooltip, message } from 'antd'
import { SyncOutlined, ReloadOutlined, CheckCircleFilled, CloseCircleFilled, WarningFilled } from '@ant-design/icons'
import {
  bridge,
  type DeprovisionTarget,
  type ProvisionOptions,
  type ProvisionResult,
  type ProvisionRetention,
  type ProvisionSchedulerKeeper,
  type ProvisionSchedulerSettings,
  type VenvUpdatePayload,
  type VenvUpdateRow,
} from '../bridge'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'
import {
  LogCleanupField,
  ProgressPanel,
  RETENTION_DEFAULT,
  SCHEDULER_OPTIONS,
  STEP_LABEL,
  SchedulerSettingsFields,
  schedulerSettingsPayload,
  applyProgress,
  membersOf,
  retentionPayload,
  retentionProblem,
  type HostProgress,
  type ProgressEvent,
} from './ProvisionModal'
import { venvChoices, venvRoot } from './DeprovisionModal'

const { Text } = Typography

type SchedulerChoice = '' | ProvisionSchedulerKeeper | 'off'

/** Log cleanup needs `runspec logs retention`, new in this runspec. */
export const RETENTION_MIN_RUNSPEC = '0.80.0'

/** What the Shared schedules picker offers on an existing venv. */
export const UPDATE_SCHEDULER_OPTIONS: { value: SchedulerChoice; label: string }[] = [
  { value: '', label: 'Leave as they are' },
  ...SCHEDULER_OPTIONS.filter(o => o.value !== '').map(o => ({ value: o.value as SchedulerChoice, label: o.label.replace(/^Daemon/, 'Move to the daemon') })),
  { value: 'off', label: 'Back to crontab (stop the daemon)' },
]

/** `0.81.0` >= `0.80.0`; an unreadable version is never new enough. */
export function versionAtLeast(version: string, minimum: string): boolean {
  const parse = (v: string) => {
    const m = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(v || '')
    return m ? [Number(m[1]), Number(m[2]), Number(m[3] ?? 0)] : null
  }
  const a = parse(version)
  const b = parse(minimum)
  if (!a || !b) return false
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i]
  return true
}

/** The options the dialog collects (the targets come from the venv + host pick). */
export interface UpdateForm {
  upgradeRunspec: boolean
  packages: string[]
  repo: string
  inbox: boolean
  retention: ProvisionRetention | null
  scheduler: SchedulerChoice
  /** The scheduler's zone / package policy; blank = leave it. */
  schedulerSettings?: ProvisionSchedulerSettings
  become: 'sudo' | 'su'
}

export function buildUpdatePayload(targets: DeprovisionTarget[], f: UpdateForm): VenvUpdatePayload {
  const packages = f.packages.map(p => p.trim()).filter(Boolean)
  const settings = schedulerSettingsPayload(f.schedulerSettings)
  return {
    targets,
    upgrade_runspec: f.upgradeRunspec,
    packages,
    repo: f.repo || undefined,
    inbox: f.inbox,
    retention: f.retention ? retentionPayload(f.retention) : undefined,
    scheduler: f.scheduler || undefined,
    ...(settings ? { scheduler_settings: settings } : {}),
    become_method: f.become,
  }
}

/** True when the request would change something (verify alone isn't an update). */
export function hasWork(p: VenvUpdatePayload): boolean {
  return !!(p.upgrade_runspec || (p.packages ?? []).length || p.inbox || p.retention || p.scheduler || p.scheduler_settings)
}

/** What the chosen options mean for one venv, before anything runs. */
export function updateNotes(row: VenvUpdateRow, f: UpdateForm): string[] {
  const out: string[] = []
  if (f.retention && !f.upgradeRunspec && !versionAtLeast(row.runspec_version, RETENTION_MIN_RUNSPEC)) {
    out.push(`runspec ${row.runspec_version || '(unknown version)'} here is too old for log cleanup (needs ${RETENTION_MIN_RUNSPEC}); tick Upgrade runspec.`)
  }
  if (f.scheduler === 'off' && !row.scheduler) out.push('No runspec-scheduler in this venv, so there is no daemon to stop.')
  if (schedulerSettingsPayload(f.schedulerSettings) && !row.scheduler && (!f.scheduler || f.scheduler === 'off')) {
    out.push('No runspec-scheduler in this venv, so the schedule time zone and package schedules aren’t set.')
  }
  const c = row.schedules
  if (f.scheduler && row.scheduler && !c) {
    out.push(`Couldn't count this venv's schedules${row.schedules_error ? `: ${row.schedules_error}` : ''}.`)
  }
  if (f.scheduler && f.scheduler !== 'off' && row.backend === 'cron' && !c) {
    out.push("Its schedules move off the owner's crontab and the daemon fires them instead.")
  }
  const upgrades = f.upgradeRunspec || f.packages.some(x => x.trim())
  if (upgrades && row.scheduler && row.scheduler_pid && f.scheduler !== 'off') {
    out.push('The scheduler daemon is restarted at the end, so it runs the upgraded code.')
  }
  if (f.scheduler === 'off' && row.backend === 'daemon' && c && c.every > 0) {
    out.push(`${plural(c.every, 'interval schedule')} can't run from cron and will stop firing (only the daemon runs "every" schedules).`)
  }
  return out
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** One line on what the Shared schedules choice moves for this venv, or ''. */
export function moveText(row: VenvUpdateRow, choice: SchedulerChoice): string {
  const c = row.schedules
  if (!choice || !c) return ''
  const extra = [
    c.disabled ? `${c.disabled} disabled` : '',
    c.leftover ? plural(c.leftover, 'leftover crontab line') + ' removed' : '',
  ].filter(Boolean)
  const tail = extra.length ? `; ${extra.join(', ')}` : ''
  if (choice === 'off') {
    if (row.backend !== 'daemon') return ''
    return `${plural(c.cron, 'schedule')} written to the owner's crontab (${plural(c.total, 'schedule')} in all${tail}).`
  }
  if (row.backend === 'daemon') {
    return c.leftover ? `Already on the daemon; ${plural(c.leftover, 'leftover crontab line')} removed.` : 'Already on the daemon; only its keeper is set up.'
  }
  if (c.in_crontab === null) return `Its crontab entries move to the daemon; how many is unknown (the crontab couldn't be read; ${plural(c.total, 'schedule')} in all${tail}).`
  return `${plural(c.in_crontab, 'crontab entry', 'crontab entries')} ${c.in_crontab === 1 ? 'moves' : 'move'} to the daemon (${plural(c.total, 'schedule')} in all${tail}).`
}

/** Across the ready venvs: how many crontab entries the choice moves, or ''. */
export function moveTotal(rows: VenvUpdateRow[], choice: SchedulerChoice): string {
  if (!choice) return ''
  let n = 0, venvs = 0, unknown = 0
  for (const r of rows) {
    const c = r.schedules
    if (!c) continue
    const k = choice === 'off' ? (r.backend === 'daemon' ? c.cron : 0) : (r.backend === 'daemon' ? 0 : c.in_crontab)
    if (k === null) { unknown++; continue }
    if (k > 0) { n += k; venvs++ }
  }
  const what = choice === 'off' ? `Writes ${plural(n, 'schedule')} to crontabs` : `Moves ${plural(n, 'crontab entry', 'crontab entries')} to the daemon`
  return `${what} on ${plural(venvs, 'host')}${unknown ? ` (${plural(unknown, 'host')} couldn't be counted)` : ''}.`
}

export function retentionText(r: VenvUpdateRow['retention']): string {
  const parts = [
    r.compact_after && `compress ${r.compact_after}`,
    r.delete_after && `delete ${r.delete_after}`,
    r.max_total_size && `cap ${r.max_total_size}`,
  ].filter(Boolean)
  return parts.length ? parts.join(', ') : 'none'
}

/**
 * What log cleanup policy the ready venvs have now, grouped: one group when
 * they all agree. `same` is that shared policy (to start the boxes from), or
 * null when they differ or none has one.
 */
export function retentionNow(rows: VenvUpdateRow[]): { groups: { text: string; hosts: string[] }[]; same: ProvisionRetention | null } {
  const groups: { text: string; hosts: string[] }[] = []
  for (const r of rows) {
    const text = retentionText(r.retention)
    const g = groups.find(x => x.text === text)
    if (g) g.hosts.push(r.host)
    else groups.push({ text, hosts: [r.host] })
  }
  const first = rows[0]?.retention
  const same = groups.length === 1 && first && (first.compact_after || first.delete_after)
    ? { compact_after: first.compact_after ?? '', delete_after: first.delete_after ?? '' }
    : null
  return { groups, same }
}

/** One line on the venv's current log cleanup across the ready hosts, or ''. */
export function retentionNowText(rows: VenvUpdateRow[]): string {
  const { groups } = retentionNow(rows)
  if (groups.length === 0) return ''
  if (groups.length === 1) return `Now: ${groups[0].text} on ${rows.length === 1 ? 'this host' : `all ${rows.length} hosts`}.`
  return `Now it differs: ${groups.map(g => `${g.text} on ${plural(g.hosts.length, 'host')}`).join('; ')}.`
}

/**
 * The hosts that list this venv but won't be updated (not ticked, or not
 * connected, which includes every host outside the active profile), so nobody
 * thinks they changed the venv everywhere. '' when every host is ticked.
 */
export function untouchedHostsText(hosts: string[], selected: string[], connected: Set<string>): string {
  const left = hosts.filter(h => !selected.includes(h))
  if (left.length === 0) return ''
  const shown = left.slice(0, 6).map(h => (connected.has(h) ? h : `${h} (not connected)`))
  const more = left.length > shown.length ? `, and ${left.length - shown.length} more` : ''
  const cold = left.some(h => !connected.has(h))
  return `This venv is also on ${plural(left.length, 'other host')} that won't change: ${shown.join(', ')}${more}.${cold ? ' Hosts outside the active profile aren\'t connected; switch profile to update them.' : ''}`
}

function schedulesText(row: VenvUpdateRow): string {
  if (!row.scheduler) return row.backend ? `${row.backend} (scheduler missing)` : 'no scheduler'
  if (row.backend === 'daemon') return row.scheduler_pid ? `daemon (pid ${row.scheduler_pid})` : 'daemon (not running)'
  return 'crontab'
}

// Bring venvs the hosts already list up to the options a provision applies
// today (runspec upgrade, packages, logs inbox, log cleanup, shared schedules)
// without recreating them: each step runs as the venv's owner and is safe to
// repeat; installs and logs/ stay. Venv + host pick works like Deprovision
// (group / profile quick-picks), progress like Provision.
export function UpdateVenvModal({
  open,
  initialTargets,
  onClose,
  onDone,
  zIndex,
}: {
  open: boolean
  initialTargets?: DeprovisionTarget[]
  onClose: () => void
  onDone?: () => void
  zIndex?: number
}) {
  const isDark = useIsDark()
  const [opts, setOpts] = useState<ProvisionOptions | null>(null)
  const [root, setRoot] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [preview, setPreview] = useState<VenvUpdateRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [upgradeRunspec, setUpgradeRunspec] = useState(true)
  const [packages, setPackages] = useState<string[]>([])
  const [repo, setRepo] = useState('')
  const [inbox, setInbox] = useState(true)
  // Log cleanup starts off (leave each venv's policy as it is), so venvs that
  // differ on purpose stay that way unless the operator ticks it.
  const [cleanup, setCleanup] = useState(false)
  const [compactAfter, setCompactAfter] = useState(RETENTION_DEFAULT.compact_after ?? '')
  const [deleteAfter, setDeleteAfter] = useState(RETENTION_DEFAULT.delete_after ?? '')
  const [scheduler, setScheduler] = useState<SchedulerChoice>('')
  const [schedSettings, setSchedSettings] = useState<ProvisionSchedulerSettings>({})
  const [become, setBecome] = useState<'sudo' | 'su'>('sudo')
  const [op, setOp] = useState<string | null>(null)
  const [progress, setProgress] = useState<Record<string, HostProgress>>({})
  const [results, setResults] = useState<ProvisionResult[] | null>(null)
  const opRef = useRef<string | null>(null)

  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setPreview(null); setOp(null); opRef.current = null; setProgress({}); setResults(null)
      // Every open starts from the defaults, never the last update's choices.
      setUpgradeRunspec(true); setPackages([]); setRepo(''); setInbox(true)
      setCleanup(false); setCompactAfter(RETENTION_DEFAULT.compact_after ?? ''); setDeleteAfter(RETENTION_DEFAULT.delete_after ?? '')
      setScheduler(''); setSchedSettings({}); setBecome('sudo')
      const first = initialTargets?.[0]
      setRoot(first ? venvRoot(first.runspec_path) : '')
      setSelected((initialTargets ?? []).map(t => t.host))
      bridge.provision_options().then(setOpts).catch(e => message.error(String(e)))
    }
    wasOpen.current = open
  }, [open, initialTargets])

  const choices = useMemo(() => venvChoices(opts?.hosts ?? []), [opts])
  const choice = choices.find(c => c.root === root)
  const connected = useMemo(() => new Set((opts?.hosts ?? []).filter(h => h.connected).map(h => h.name)), [opts])
  const targets: DeprovisionTarget[] = useMemo(
    () => (choice?.hosts ?? []).filter(h => selected.includes(h.host)),
    [choice, selected],
  )

  useEffect(() => {
    if (!open || targets.length === 0 || op) { if (!op) setPreview(null); return }
    let alive = true
    setLoading(true)
    bridge.venv_update_preview({ targets })
      .then(res => { if (alive) setPreview(res.rows) })
      .catch(e => { if (alive) message.error(String(e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, targets, op])

  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as ProgressEvent
      if (!d || d.op !== opRef.current) return
      setProgress(prev => applyProgress(prev, d))
    }
    const onDoneEvt = (e: Event) => {
      const d = (e as CustomEvent).detail as { op: string; results: ProvisionResult[] }
      if (!d || d.op !== opRef.current) return
      setResults(d.results)
      const ok = d.results.filter(r => r.ok).length
      if (ok === d.results.length) message.success(`Updated on ${ok} host${ok !== 1 ? 's' : ''}`)
      else message.warning(`Updated on ${ok} of ${d.results.length} hosts`)
      onDone?.()
    }
    window.addEventListener('runspec:provision_progress', onProgress)
    window.addEventListener('runspec:provision_done', onDoneEvt)
    return () => {
      window.removeEventListener('runspec:provision_progress', onProgress)
      window.removeEventListener('runspec:provision_done', onDoneEvt)
    }
  }, [onDone])

  const retention = cleanup ? { compact_after: compactAfter, delete_after: deleteAfter } : null
  const retentionError = retention ? retentionProblem(retention) : ''
  const form: UpdateForm = { upgradeRunspec, packages, repo, inbox, retention, scheduler, schedulerSettings: schedSettings, become }
  const ready = (preview ?? []).filter(r => r.ok)
  const payload = buildUpdatePayload(ready.map(r => ({ host: r.host, runspec_path: r.runspec_path })), form)
  const nowRetention = retentionNow(ready)
  const nowRetentionText = retentionNowText(ready)
  const untouched = choice ? untouchedHostsText(choice.hosts.map(h => h.host), selected, connected) : ''
  // Ticking log cleanup starts the boxes from the policy the venvs share now,
  // else the provision defaults.
  const onCleanup = (v: boolean) => {
    if (v) {
      const from = nowRetention.same ?? RETENTION_DEFAULT
      setCompactAfter(from.compact_after ?? ''); setDeleteAfter(from.delete_after ?? '')
    }
    setCleanup(v)
  }
  const canStart = ready.length > 0 && hasWork(payload) && !retentionError && !op
  const running = op !== null && results === null

  const start = async () => {
    if (!canStart) return
    try {
      setProgress({}); setResults(null)
      const res = await bridge.venv_update_start(payload)
      opRef.current = res.op
      setOp(res.op)
    } catch (e) {
      message.error(String(e))
    }
  }

  const retryFailed = () => {
    setSelected((results ?? []).filter(r => !r.ok).map(r => r.host))
    setOp(null); opRef.current = null; setResults(null); setProgress({})
  }

  const hostOptions = (choice?.hosts ?? []).map(h => ({
    value: h.host,
    disabled: !connected.has(h.host),
    label: <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{h.host}{!connected.has(h.host) && <Text type="secondary" style={{ fontSize: 11 }}> (not connected)</Text>}</span>,
  }))
  const inChoice = (hs: string[]) => hs.filter(h => choice?.hosts.some(c => c.host === h))
  const label = (s: string) => <div><Text style={{ fontSize: 12, color: textSecondary(isDark) }}>{s}</Text></div>
  const hint = (s: string) => <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>{s}</Text>
  const off = scheduler === 'off'
  const stepLabel = {
    ...STEP_LABEL,
    install: 'installing / upgrading packages',
    ...(off ? { scheduler: 'moving schedules back to crontab', keeper: 'stopping the scheduler daemon' } : {}),
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={680}
      zIndex={zIndex}
      maskClosable={false}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><SyncOutlined /> Update a runspec venv</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          {running && <Button onClick={() => op && bridge.provision_cancel(op)}>Cancel</Button>}
          {results && results.some(r => !r.ok) && <Button icon={<ReloadOutlined />} onClick={retryFailed}>Retry failed</Button>}
          <Button onClick={onClose}>Close</Button>
          {!op && (
            <Button type="primary" disabled={!canStart} onClick={start}>
              Update {ready.length || ''} host{ready.length !== 1 ? 's' : ''}
            </Button>
          )}
        </div>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, opacity: op ? 0.6 : 1, pointerEvents: op ? 'none' : 'auto' }}>
        <Text style={{ fontSize: 13 }}>
          Applies what you tick to a venv the hosts already list, as the venv's owner. Nothing is recreated: installed packages, settings and <Text code>logs/</Text> stay, and every step is safe to run again.
        </Text>

        <div>
          {label('Venv')}
          <Select
            style={{ width: '100%' }}
            value={root || undefined}
            onChange={v => { setRoot(v); setSelected([]) }}
            placeholder="Pick a venv the hosts already list"
            options={choices.map(c => ({
              value: c.root,
              label: <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{c.root} <Text type="secondary" style={{ fontSize: 11 }}>on {c.hosts.length} host{c.hosts.length !== 1 ? 's' : ''}</Text></span>,
            }))}
          />
        </div>

        {choice && (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
              <Text style={{ fontSize: 12, color: textSecondary(isDark) }}>Hosts</Text>
              {(opts?.groups ?? []).map(g => (
                <Tag key={`g:${g}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => setSelected(p => [...new Set([...p, ...inChoice(membersOf(opts?.hosts ?? [], 'group', g))])])}>+ Group: {g}</Tag>
              ))}
              {(opts?.profiles ?? []).map(p => (
                <Tag key={`p:${p}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => setSelected(s => [...new Set([...s, ...inChoice(membersOf(opts?.hosts ?? [], 'profile', p))])])}>+ Profile: {p}</Tag>
              ))}
              <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSelected(inChoice([...connected]))}>All</Button>
              {selected.length > 0 && <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSelected([])}>Clear</Button>}
            </div>
            <div style={{ border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: '8px 10px', maxHeight: 120, overflowY: 'auto' }}>
              <Checkbox.Group value={selected} onChange={v => setSelected(v as string[])} options={hostOptions} style={{ display: 'flex', flexDirection: 'column', gap: 2 }} />
            </div>
            {untouched && hint(untouched)}
          </div>
        )}

        {loading && <Spin size="small" />}
        {preview && !op && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {label('Now (read from each host; nothing has been changed yet)')}
            {preview.map(r => {
              const notes = r.ok ? updateNotes(r, form) : []
              return (
                <div key={r.host} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
                  {r.ok ? <CheckCircleFilled style={{ color: textMuted(isDark), marginTop: 3 }} /> : <CloseCircleFilled style={{ color: '#ff4d4f', marginTop: 3 }} />}
                  <span style={{ fontFamily: MONO_FONT, minWidth: 100 }}>{r.host}</span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    {r.ok && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        runspec {r.runspec_version || '?'} · owner {r.owner || '?'} · inbox {r.inbox ? 'set up' : 'missing'} · log cleanup {retentionText(r.retention)} · schedules {schedulesText(r)}
                      </Text>
                    )}
                    {r.ok && moveText(r, scheduler) && <div><Text style={{ fontSize: 12 }}>{moveText(r, scheduler)}</Text></div>}
                    {r.blockers.map(b => <div key={b}><Text type="danger" style={{ fontSize: 12 }}>{b}</Text></div>)}
                    {notes.map(n => <div key={n}><Text type="warning" style={{ fontSize: 12 }}><WarningFilled /> {n}</Text></div>)}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        {choice && (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
              <div>
                {label('Packages')}
                <Checkbox checked={upgradeRunspec} onChange={e => setUpgradeRunspec(e.target.checked)}>Upgrade runspec</Checkbox>
                <Select mode="tags" value={packages} onChange={setPackages} placeholder="also install or upgrade: runspec-linux…" style={{ width: '100%', marginTop: 4 }} tokenSeparators={[',', ' ']} open={false} suffixIcon={null} aria-label="Packages" />
                {hint('pip install -U: only what is out of date changes.')}
              </div>
              <div>
                {label('Package index')}
                <Select
                  value={repo}
                  onChange={setRepo}
                  style={{ width: '100%' }}
                  options={[{ value: '', label: "Host's own pip config" }, ...(opts?.repos ?? []).map(r => ({ value: r.id, label: r.name }))]}
                />
              </div>
            </div>

            <div>
              {label('Logs inbox')}
              <Checkbox checked={inbox} onChange={e => setInbox(e.target.checked)}>Set up the logs inbox</Checkbox>
              {hint("runspec logs share --inbox: other accounts' runs land in this venv's history.")}
            </div>

            <div>
              {label('Log cleanup')}
              <LogCleanupField
                checked={cleanup}
                onChecked={onCleanup}
                checkboxLabel="Set the log cleanup policy"
                compactAfter={compactAfter}
                onCompactAfter={setCompactAfter}
                deleteAfter={deleteAfter}
                onDeleteAfter={setDeleteAfter}
                error={retentionError}
                hint={cleanup
                  ? 'Replaces these two settings in the venv\'s policy (runspec logs retention, runspec 0.80.0+). A blank box or off turns that step off, e.g. blank compress after = never compress. The audit log is never touched.'
                  : 'Leaves each venv\'s log cleanup policy as it is.'}
              />
              {nowRetentionText && <div><Text style={{ fontSize: 12 }}>{nowRetentionText}</Text></div>}
              {cleanup && nowRetention.groups.length > 1 && (
                <div><Text type="warning" style={{ fontSize: 12 }}><WarningFilled /> Sets the same policy on all {ready.length} hosts.</Text></div>
              )}
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8 }}>
              <div>
                {label('Shared schedules')}
                <Select value={scheduler} onChange={setScheduler} options={UPDATE_SCHEDULER_OPTIONS} style={{ width: '100%' }} aria-label="Shared schedules" />
                {hint(scheduler === ''
                  ? 'Schedules keep firing the way they do now.'
                  : off
                    ? "Writes the schedules back to the owner's crontab, then removes the daemon's keepers and stops it."
                    : `Installs runspec-scheduler, moves this venv's schedules from the owner's crontab to its daemon and sets up what restarts it.${scheduler === 'auto' ? ' Per host: systemd --user when lingering is on, else supervisord in the venv.' : ''}`)}
                {scheduler && preview && moveTotal(ready, scheduler) && <div><Text style={{ fontSize: 12 }}>{moveTotal(ready, scheduler)}</Text></div>}
                {!off && (
                  <div style={{ marginTop: 8 }}>
                    <SchedulerSettingsFields value={schedSettings} onChange={setSchedSettings} leave
                      hint="Needs runspec-scheduler 0.10.0 or later (add runspec-scheduler to the packages to upgrade it)." />
                  </div>
                )}
              </div>
              <div>
                {label('Become via')}
                <Tooltip title="How the SSH login acts as the venv's owner when they differ">
                  <Select value={become} onChange={setBecome} options={[{ value: 'sudo', label: 'sudo' }, { value: 'su', label: 'su' }]} style={{ width: '100%' }} aria-label="Become via" />
                </Tooltip>
              </div>
            </div>

            {preview && ready.length > 0 && !hasWork(payload) && (
              <Alert type="info" showIcon message="Tick at least one thing to update." />
            )}
          </>
        )}
      </div>

      {op && <ProgressPanel progress={progress} stepLabel={stepLabel} />}
    </Modal>
  )
}
