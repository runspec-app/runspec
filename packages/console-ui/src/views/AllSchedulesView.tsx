/**
 * AllSchedulesView — one list of every schedule that fires: this console's own,
 * plus each venv's runspec-scheduler schedules (cron or daemon backend) on every
 * connected host in the active profile and the local venvs. The option card
 * above it (All / This console / Host cron / Host daemon) presets which source it
 * shows; the filters below narrow it further and are remembered. Host schedules
 * can be added, edited, paused, removed and their crontab synced (0.385.0): each
 * runs the venv's own runspec-scheduler as the venv owner. This console's own
 * schedules are edited on the This console card.
 *
 * Rows, states, counts and filtering live in allSchedules.ts (pure, tested).
 */
import { useEffect, useMemo, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, Alert, Dropdown, Empty, Input, Modal, Popover, Select, Switch, Space, Spin, Popconfirm, message, theme } from 'antd'
import {
  ReloadOutlined, HistoryOutlined, InfoCircleOutlined, PlusOutlined, EditOutlined, DeleteOutlined,
  PauseCircleOutlined, PlayCircleOutlined, SyncOutlined, SettingOutlined, RightOutlined, DownOutlined, ThunderboltOutlined,
  FilterOutlined, CloseOutlined,
} from '@ant-design/icons'
import { When, fmtWhen, fullWhen } from '../when'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HostScheduleResult, type HostScheduleSave, type Runnable, type SharedSchedule } from '../bridge'
import { HostScheduleModal, timeoutText, type HostScheduleEditing } from './HostScheduleModal'
import { MONO_FONT, RADIUS, useIsDark, textColor, textMuted, textSecondary, withAlpha } from '../ThemeContext'
import {
  DAEMON_SETTINGS, DEFAULT_FILTERS, FOLDED_ATTENTION_MAX, PACKAGE_POLICY, STATE_TONE, claimStale, daemonNeedsAttention, daemonSummary, filterRows, noteBatches, noteText, noteVerbs, noteable, packageOverridden, settingText, settingsForm, shortDuration, supportsPackages, venvSettingsChanges,
  type Collected, type VenvSettingsForm, type NoteVerb, type DaemonVenv, type RowFilters, type RowSource, type RowState, type ScheduleRow, type ScheduleScope, type SchedulerVenv, type SortKey,
} from './allSchedules'
import { RUN_TONE, ScheduleDetail, relTime } from './sharedSchedules'
import { EmptyState, IconAction, StatusTag, type StatusTone } from '../components/kit'
import { describeCron } from '../cronText'
import { AskFirst } from '../components/risk'

const { Text } = Typography

const FILTERS_KEY = 'runspec.schedules.allFilters'

export function readFilters(): RowFilters {
  try {
    const raw = localStorage.getItem(FILTERS_KEY)
    if (!raw) return DEFAULT_FILTERS
    const v = JSON.parse(raw) as Partial<RowFilters>
    return {
      search: typeof v.search === 'string' ? v.search : '',
      source: Array.isArray(v.source) ? v.source : [],
      states: Array.isArray(v.states) ? v.states : [],
      owners: Array.isArray(v.owners) ? v.owners : [],
      runAs: Array.isArray(v.runAs) ? v.runAs : [],
      onlyProblems: !!v.onlyProblems,
      sort: (['next', 'last', 'host', 'runnable'] as SortKey[]).includes(v.sort as SortKey) ? v.sort as SortKey : 'next',
    }
  } catch { return DEFAULT_FILTERS }
}

/** Whether the viewer has ever set a filter (so we don't override it). */
function hasStoredFilters(): boolean {
  try { return localStorage.getItem(FILTERS_KEY) != null } catch { return true }
}

const FILTER_SOURCE_LABEL: Record<RowSource, string> = {
  console: 'This console', cron: 'Host cron', daemon: 'Host daemon', unknown: 'Unreadable venv',
}

export interface FilterChip { key: string; label: string; clear: Partial<RowFilters> }

/** The active filters as removable chips, each with the patch that clears it.
 *  Pure, for tests. */
export function filterChips(f: RowFilters, scope: ScheduleScope): FilterChip[] {
  const out: FilterChip[] = []
  if (f.onlyProblems) out.push({ key: 'problems', label: 'only problems', clear: { onlyProblems: false } })
  if (scope === 'all') for (const s of f.source) out.push({ key: `src:${s}`, label: FILTER_SOURCE_LABEL[s], clear: { source: f.source.filter(x => x !== s) } })
  for (const s of f.states) out.push({ key: `st:${s}`, label: s, clear: { states: f.states.filter(x => x !== s) } })
  if (scope !== 'console') {
    for (const o of f.owners) out.push({ key: `ow:${o}`, label: `owner ${o}`, clear: { owners: f.owners.filter(x => x !== o) } })
    for (const o of f.runAs) out.push({ key: `ra:${o}`, label: `runs as ${o}`, clear: { runAs: f.runAs.filter(x => x !== o) } })
  }
  return out
}

const DAEMONS_OPEN_KEY = 'runspec.schedules.daemonsOpen'

/** The remembered open/folded choice for the daemon list, or null if never set. */
function readDaemonsOpen(): boolean | null {
  try {
    const raw = localStorage.getItem(DAEMONS_OPEN_KEY)
    return raw === null ? null : raw === '1'
  } catch { return null }
}

function writeDaemonsOpen(open: boolean) {
  try { localStorage.setItem(DAEMONS_OPEN_KEY, open ? '1' : '0') } catch { /* storage unavailable — fine */ }
}

function writeFilters(f: RowFilters) {
  try { localStorage.setItem(FILTERS_KEY, JSON.stringify(f)) } catch { /* storage unavailable — fine */ }
}

const SOURCE_LABEL: Record<RowSource, string> = {
  console: 'console', cron: 'host cron', daemon: 'host daemon', unknown: 'host',
}

const ALL_STATES: RowState[] = [
  'ok', 'running', 'last run failed', 'not installed', 'not installable', 'stale line',
  'scheduler stopped', 'scheduler stale', "can't read", 'disabled', 'unknown', 'legacy line',
  'being handled', 'dealt with',
]

const NOTE_WORDS: Record<NoteVerb, { button: string; title: string; done: string; placeholder: string }> = {
  claim: { button: "I'm on it", title: "Say you're handling", done: 'Claimed', placeholder: 'Optional: what you are looking at' },
  resolve: { button: 'Dealt with…', title: 'Mark dealt with', done: 'Marked dealt with', placeholder: 'What was done, so the next person knows (required)' },
  release: { button: 'Release', title: 'Let go of', done: 'Released', placeholder: 'Optional: why' },
}

/** Claim / mark dealt with / release the selected failing host schedules: one
 *  `runspec history <verb>` per venv, as its owner. Pure UI around the bridge. */
function NoteModal({ verb, rows, onClose, onDone }: {
  verb: NoteVerb | null
  rows: ScheduleRow[]
  onClose: () => void
  onDone: () => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { setNote(''); setError(''); setBusy(false) }, [verb])
  if (!verb) return null
  const w = NOTE_WORDS[verb]
  const batches = noteBatches(rows)
  const count = batches.reduce((n, b) => n + b.runs.length, 0)
  const needNote = verb === 'resolve' && !note.trim()
  const submit = async () => {
    setBusy(true); setError('')
    const failed: string[] = []
    let ok = 0
    for (const b of batches) {
      try {
        const res = await bridge.host_schedule_note({ host: b.host, venv: b.venv, verb, runs: b.runs, note: note.trim() })
        if (res.ok) ok += b.runs.length
        else failed.push(`${b.host} / ${b.venv}: ${res.error || 'failed'}`)
      } catch (e) { failed.push(`${b.host} / ${b.venv}: ${String(e)}`) }
    }
    setBusy(false)
    if (ok) message.success(`${w.done} ${ok} schedule${ok === 1 ? '' : 's'}.`)
    if (failed.length) { setError(failed.join('\n')); if (ok) onDone(); return }
    onDone()
  }
  return (
    <Modal open title={`${w.title} ${count} failing schedule${count === 1 ? '' : 's'}`} onCancel={onClose} destroyOnHidden
      okText={w.button.replace('…', '')} okButtonProps={{ disabled: needNote || !count, loading: busy }} onOk={submit}>
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        {rows.filter(noteable).map(r => (
          <div key={r.key} style={{ fontFamily: MONO_FONT }}>{r.host} / {r.venv} · {r.id}
            {r.attention && <Text type="secondary" style={{ fontFamily: 'inherit' }}> · {r.attention.failures} failed</Text>}
          </div>
        ))}
      </div>
      <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} maxLength={500} value={note} placeholder={w.placeholder}
        aria-label="Note" onChange={e => setNote(e.target.value)} />
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
        Everyone who reads these venvs sees it, here and in <Text code style={{ fontSize: 12 }}>runspec history jobs</Text> on the host.
        It covers each schedule's failures until it next succeeds, and is recorded with your login.
      </Text>
      {error && <Alert type="error" showIcon style={{ marginTop: 10, whiteSpace: 'pre-wrap' }} message={error} />}
    </Modal>
  )
}

/** A schedule's timing: the plain reading when there is one, with the raw
 *  expression beside it in mono for anyone who wants the exact value. */
export function WhenCell({ when }: { when: string }) {
  // A host schedule's "when" may carry its time zone: "15 * * * * (Europe/London)".
  const m = when.match(/^(.*?)\s*\(([^()]+)\)\s*$/)
  const expr = m ? m[1] : when
  const tz = m ? m[2] : ''
  const plain = describeCron(expr)
  if (!plain) return <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{when}</span>
  return (
    <span style={{ display: 'inline-flex', flexDirection: 'column' }}>
      <span style={{ fontSize: 13 }}>{plain}{tz && <span style={{ color: 'var(--rs-text-muted)' }}> · {tz}</span>}</span>
      <span style={{ fontFamily: MONO_FONT, fontSize: 12, color: 'var(--rs-text-muted)' }}>{when}</span>
    </span>
  )
}

const KEEPER_LABEL: Record<string, string> = {
  'systemd-user': 'systemd --user', systemd: 'systemd (system)', supervisor: 'supervisord', cron: 'cron watchdog',
}

function DaemonStrip({ d, editable, busy, onRestart, onSettings }: {
  d: DaemonVenv
  editable: boolean
  busy: boolean
  onRestart: (d: DaemonVenv) => void
  onSettings: (d: DaemonVenv) => void
}) {
  const isDark = useIsDark()
  const st = d.daemon?.state ?? 'stopped'
  const tone: StatusTone = st === 'running' ? 'ok' : st === 'stale' ? 'attention' : 'failed'
  const dm = d.daemon
  const bits: string[] = []
  if (dm && st !== 'stopped') {
    if (dm.pid) bits.push(`pid ${dm.pid}`)
    if (dm.uptime_s != null) bits.push(`up ${shortDuration(dm.uptime_s)}`)
    if (dm.heartbeat_age_s != null) bits.push(`heartbeat ${shortDuration(dm.heartbeat_age_s)} ago`)
    if (dm.firing) bits.push(`${dm.firing} firing now`)
  }
  if (dm?.version) bits.push(`v${dm.version}`)
  const keeper = d.keepers.length ? d.keepers.map(k => KEEPER_LABEL[k] ?? k).join(', ') : ''
  const running = st === 'running'
  const verb = running ? 'Restart' : 'Start'
  const confirm = running
    ? `Restart the scheduler on ${d.host} / ${d.venv}? Fires already running carry on; the new daemon picks up from the next tick.`
    : `Start the scheduler on ${d.host} / ${d.venv}${keeper ? ` through ${keeper}` : ''}?`
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '6px 10px',
      border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, background: 'var(--rs-surface-raised)',
    }}>
      <StatusTag tone={tone}>scheduler {st}</StatusTag>
      {dm?.restart_needed && running && (
        <StatusTag tone="attention" tip="The daemon still runs the runspec-scheduler it started with; the venv has a different one installed now. Restart it to run the new code.">restart needed</StatusTag>
      )}
      <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{d.host} / {d.venv}</span>
      {d.owner && <span style={{ color: textMuted(isDark), fontSize: 12 }}>as {d.owner}</span>}
      {bits.length > 0 && <span style={{ color: textSecondary(isDark), fontSize: 12 }}>{bits.join(' · ')}</span>}
      <Tooltip title={keeper ? 'What starts the daemon again after a crash or a reboot' : 'Nothing restarts this daemon after a crash or a reboot. Update venv… sets up a keeper.'}>
        <span style={{ color: keeper ? textMuted(isDark) : undefined, fontSize: 12 }}>
          {keeper ? `kept by ${keeper}` : <Text type="warning" style={{ fontSize: 12 }}>no keeper</Text>}
        </span>
      </Tooltip>
      {editable && (
        <Space size={6} style={{ marginLeft: 'auto' }}>
          <Button size="small" icon={<SettingOutlined />} onClick={() => onSettings(d)}>Settings</Button>
          <AskFirst ask={{ level: 'local', title: confirm, action: `${verb} the scheduler on ${d.host} / ${d.venv}`, okText: verb, danger: false }}
            onConfirm={() => onRestart(d)}>
            <Button size="small" icon={running ? <ReloadOutlined /> : <PlayCircleOutlined />} loading={busy}
              type={!running || dm?.restart_needed ? 'primary' : 'default'}>{verb}</Button>
          </AskFirst>
        </Space>
      )}
    </div>
  )
}

/** View and change one venv's `[scheduler]` settings: the zone and what to do
 *  with schedules its packages offer (runspec-scheduler 0.10.0+), plus the
 *  daemon's durations on a daemon venv. The backend is shown read-only (Update
 *  venv… moves a venv between cron and the daemon). */
export function SchedulerSettingsModal({ v, log, onClose, onSaved, zIndex }: {
  v: SchedulerVenv | null
  log?: string
  onClose: () => void
  onSaved: () => void
  zIndex?: number
}) {
  const [form, setForm] = useState<VenvSettingsForm>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setError('')
    setForm(v ? settingsForm(v) : {})
  }, [v])
  const changes = v ? venvSettingsChanges(v, form) : null
  const daemon = v?.backend === 'daemon'
  const pkgs = v ? supportsPackages(v) : false
  const save = async () => {
    if (!v || !changes) return
    setSaving(true); setError('')
    try {
      const res = await bridge.host_scheduler_settings({ host: v.host, venv: v.venv, backend: v.backend, ...changes })
      if (!res.ok) { setError(res.error || 'Saving the settings failed.'); return }
      const after = daemon ? 'the daemon picks them up by itself' : 'the crontab was brought up to date'
      message.success(`Saved the scheduler settings on ${v.host} / ${v.venv}; ${after}.`)
      for (const w of res.warnings ?? []) message.warning(w, 8)
      onSaved()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }
  const zone = (form.timezone ?? '').trim()
  return (
    <Modal open={!!v} onCancel={onClose} zIndex={zIndex} width={580} destroyOnHidden
      title={v ? <span>Scheduler settings · <span style={{ fontFamily: MONO_FONT }}>{v.host} / {v.venv}</span></span> : null}
      okText="Save" onOk={save} okButtonProps={{ disabled: !changes, loading: saving }}>
      {v && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {daemon && !v.settings && (
            <Alert type="info" showIcon message="This venv's runspec-scheduler is older than 0.7.0, so its settings can't be read. Saving sets the values you type; upgrade it (Update venv…) to see them here." />
          )}
          {!pkgs && (
            <Alert type="info" showIcon message="The time zone and package schedules need runspec-scheduler 0.10.0 or later in this venv. Update venv… upgrades it." />
          )}
          <div style={{ fontSize: 13 }}>
            <Text type="secondary">Fires from: </Text><Text strong>{daemon ? 'daemon' : 'crontab'}</Text>
            <Text type="secondary"> (Update venv… moves a venv between crontab and the daemon)</Text>
          </div>
          {pkgs && (
            <>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ width: 130, fontWeight: 500 }}>Time zone</span>
                  <Input size="small" style={{ width: 220, fontFamily: MONO_FONT }} allowClear value={form.timezone ?? ''}
                    placeholder={v.hostZone ? `${v.hostZone} (the host's)` : "the host's"}
                    onChange={e => setForm(f => ({ ...f, timezone: e.target.value }))} aria-label="Time zone" />
                </div>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 138, display: 'block' }}>
                  The clock schedules with no zone of their own keep, e.g. Europe/London. Blank = the host’s.
                  {!daemon && zone && v.hostZone && zone !== v.hostZone
                    ? ' Cron fires on the host’s clock, so on this cron venv those schedules will need the daemon.'
                    : ''}
                </Text>
              </div>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ width: 130, fontWeight: 500 }}>Package schedules</span>
                  <Select size="small" style={{ width: 220 }} value={form.package_schedules}
                    onChange={val => setForm(f => ({ ...f, package_schedules: val }))} aria-label="Package schedules"
                    options={PACKAGE_POLICY.map(p => ({ value: p.value, label: p.label }))} />
                </div>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 138, display: 'block' }}>
                  Schedules a package suggests in its runspec.toml. {PACKAGE_POLICY.find(p => p.value === form.package_schedules)?.help ?? ''}
                  {v.packages?.offered ? ` ${v.packages.offered} waiting to be turned on now.` : ''}
                </Text>
              </div>
            </>
          )}
          {daemon && DAEMON_SETTINGS.map(({ key, label, help }) => (
            <div key={key}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 130, fontWeight: 500 }}>{label}</span>
                <Input size="small" style={{ width: 140, fontFamily: MONO_FONT }} value={form[key] ?? ''}
                  placeholder={v.settings ? settingText(v.settings[key]) : 'e.g. 6h'}
                  onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))} aria-label={label} />
              </div>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 138, display: 'block' }}>{help}</Text>
            </div>
          ))}
          {daemon && <Text type="secondary" style={{ fontSize: 12 }}>Durations: seconds, or 90m, 6h or 1d.</Text>}
          <Text type="secondary" style={{ fontSize: 12 }}>Saved with the venv's own runspec-scheduler, as {v.owner || 'the venv owner'}.</Text>
          {log && <Text type="secondary" style={{ fontSize: 12 }}>Daemon log: <span style={{ fontFamily: MONO_FONT }}>{log}</span></Text>}
          {error && <Alert type="error" showIcon message={error} />}
        </div>
      )}
    </Modal>
  )
}

interface AllSchedulesViewProps {
  scope: ScheduleScope
  collected: Collected
  loading: boolean
  loaded: boolean            // a host read has finished at least once
  updatedAt: number | null
  hostCount: number          // hosts read (connected, in the active profile)
  activeProfile?: string
  onRefresh: () => void
  onOpenHistory?: (search: string) => void
  /** Runnables known to the console, for the host-schedule editor. Without it
   *  the list stays read-only. */
  runnables?: Runnable[]
}

/** A save payload that keeps a host schedule as it is, bar `enabled`. */
export function keepPayload(v: SchedulerVenv, s: SharedSchedule, enabled: boolean): HostScheduleSave {
  const timeout = timeoutText(s.timeout, v.backend, v.settings?.timeout ?? null)
  return {
    host: v.host, venv: v.venv, backend: v.backend,
    id: s.id, original_id: s.id, runnable: s.runnable, args: s.args ?? {}, enabled,
    ...(s.command?.length ? { command: s.command } : {}),
    ...(s.when.every ? { every: s.when.every } : { cron: s.when.cron ?? '' }),
    ...(s.when.timezone ? { timezone: s.when.timezone } : {}),
    ...(timeout ? { timeout } : {}),
    ...(v.backend === 'daemon' && s.catch_up ? { catch_up: true } : {}),
  }
}

/** Whether a host row is fixed by syncing its venv's crontab. */
export function needsSync(r: ScheduleRow): boolean {
  return r.source === 'cron' && (r.state === 'not installed' || r.state === 'stale line' || r.state === 'legacy line')
}

/** One toast for a host-schedule change: what happened, plus any warnings. */
function report(what: string, res: HostScheduleResult) {
  if (!res.ok) { message.error(res.error || `${what} failed`); return }
  const synced = res.sync ? ` Crontab synced (${res.sync.installed} line${res.sync.installed === 1 ? '' : 's'}).` : ''
  message.success(`${what}.${synced}`)
  for (const w of res.warnings ?? []) message.warning(w, 8)
}

export function AllSchedulesView({ scope, collected, loading, loaded, updatedAt, hostCount, activeProfile, onRefresh, onOpenHistory, runnables }: AllSchedulesViewProps) {
  const isDark = useIsDark()
  const { token } = theme.useToken()
  const [filters, setFiltersState] = useState<RowFilters>(readFilters)
  const setFilters = (patch: Partial<RowFilters>) => setFiltersState(prev => {
    const next = { ...prev, ...patch }
    writeFilters(next)
    return next
  })

  // With problems on the page and no filter of the viewer's own yet, open on
  // just the problems: the chip says so and one click shows everything.
  const [filterOpen, setFilterOpen] = useState(false)
  const anyProblem = collected.rows.some(r => r.problem)
  useEffect(() => {
    if (anyProblem && !hasStoredFilters()) setFiltersState(prev => ({ ...prev, onlyProblems: true }))
  }, [anyProblem])

  const scoped = useMemo(() => filterRows(collected.rows, scope, { ...DEFAULT_FILTERS, sort: filters.sort }), [collected.rows, scope, filters.sort])
  const rows = useMemo(() => filterRows(collected.rows, scope, filters), [collected.rows, scope, filters])
  const owners = useMemo(() => [...new Set(scoped.map(r => r.owner).filter(Boolean))].sort(), [scoped])
  const runAs = useMemo(() => [...new Set(scoped.map(r => r.runAs).filter(Boolean))].sort(), [scoped])
  const states = useMemo(() => ALL_STATES.filter(s => scoped.some(r => r.state === s) || filters.states.includes(s)), [scoped, filters.states])
  const notes = collected.notes.filter(n => scope === 'all' || n.source === scope)
  const daemons = scope === 'daemon' || scope === 'all' ? collected.daemons : []
  const chips = filterChips(filters, scope)
  const narrowed = filters.search || filters.states.length || filters.owners.length || filters.runAs.length || filters.onlyProblems || (scope === 'all' && filters.source.length)

  const muted = textMuted(isDark)
  const secondary = textSecondary(isDark)

  // ── host schedule editing ──
  const editable = !!runnables && scope !== 'console'
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<HostScheduleEditing | null>(null)
  const [busy, setBusy] = useState<string>('')   // row key / venv key with an action in flight
  const venvOf = (r: ScheduleRow) => collected.venvs.find(v => v.host === r.host && v.venv === r.venv) ?? null
  const scopeVenvs = collected.venvs.filter(v => scope === 'all' || v.backend === scope)
  const idsByVenv = useMemo(() => {
    const out: Record<string, string[]> = {}
    for (const r of collected.rows) if (r.shared) (out[`${r.host}::${r.venv}`] ??= []).push(r.id)
    return out
  }, [collected.rows])
  const afterChange = (what: string, res: HostScheduleResult) => {
    report(what, res)
    if (res.ok) onRefresh()
  }
  const run = async (key: string, what: string, call: () => Promise<HostScheduleResult>) => {
    setBusy(key)
    try { afterChange(what, await call()) } catch (e) { message.error(`${what} failed: ${String(e)}`) } finally { setBusy('') }
  }
  const runNow = async (r: ScheduleRow, v: SchedulerVenv, s: SharedSchedule) => {
    setBusy(`run::${r.key}`)
    try {
      const res = await bridge.host_schedule_run_now({ host: v.host, venv: v.venv, id: s.id })
      if (!res.ok) { message.error(res.error || `Couldn't start ${s.id}`, 10); return }
      const who = res.user ? ` as ${res.user}` : ''
      if (res.finished) {
        const fn = res.exit_code === 0 ? message.success : message.warning
        fn(`${s.id} ran${who} and finished at once (exit ${res.exit_code}); see History for its output.`, 8)
      } else {
        message.success(`Started ${s.id}${who} on ${v.host} / ${v.venv}; it shows in History when it finishes.`)
      }
      // Re-read in a moment so the row shows the run.
      window.setTimeout(onRefresh, 3000)
    } catch (e) { message.error(`Run now failed: ${String(e)}`) } finally { setBusy('') }
  }
  const [daemonsOpen, setDaemonsOpen] = useState<boolean | null>(readDaemonsOpen)
  // Folded to one status line (plus any that need a look) unless there's only one.
  const daemonsShown = daemonsOpen ?? daemons.length <= 1
  const toggleDaemons = () => { writeDaemonsOpen(!daemonsShown); setDaemonsOpen(!daemonsShown) }
  const visibleDaemons = daemonsShown ? daemons : daemons.filter(daemonNeedsAttention).slice(0, FOLDED_ATTENTION_MAX)
  const foldedHidden = daemons.length - visibleDaemons.length
  const [settingsVenv, setSettingsVenv] = useState<SchedulerVenv | null>(null)
  const openDaemonSettings = (d: DaemonVenv) => setSettingsVenv(collected.venvs.find(v => v.key === d.key) ?? null)
  const restartDaemon = async (d: DaemonVenv) => {
    const verb = d.daemon?.state === 'running' ? 'Restarted' : 'Started'
    setBusy(`daemon::${d.key}`)
    try {
      const res = await bridge.host_scheduler_restart({ host: d.host, venv: d.venv })
      if (!res.ok) {
        message.error([res.error || `${verb.replace(/ed$/, '')} failed`, res.hint].filter(Boolean).join(' '), 10)
      } else {
        const pid = res.daemon?.pid ? ` (pid ${res.daemon.pid})` : ''
        message.success(`${verb} the scheduler on ${d.host} / ${d.venv}${pid}.`)
        for (const w of res.warnings ?? []) message.warning(w, 8)
      }
      onRefresh()
    } catch (e) { message.error(`Restart failed: ${String(e)}`) } finally { setBusy('') }
  }
  // ── who is handling a failure: tick failing host schedules, then claim / resolve / release ──
  const [selected, setSelected] = useState<string[]>([])
  const [noteVerb, setNoteVerb] = useState<NoteVerb | null>(null)
  const selectedRows = useMemo(() => rows.filter(r => selected.includes(r.key) && noteable(r)), [rows, selected])
  useEffect(() => {
    // Drop selections that left the list (filtered out, or no longer failing).
    setSelected(prev => prev.filter(k => rows.some(r => r.key === k && noteable(r))))
  }, [rows])
  const verbs = noteVerbs(selectedRows)
  const syncVenv = (v: SchedulerVenv) => run(v.key, `Synced ${v.host} / ${v.venv}`, () => bridge.host_schedule_sync({ host: v.host, venv: v.venv }))

  const columns: ColumnType<ScheduleRow>[] = [
    {
      title: 'State',
      key: 'state',
      width: 150,
      render: (_: unknown, r) => (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
          <StatusTag tone={STATE_TONE[r.state]} tip={r.stateTip}>{r.state}</StatusTag>
          {r.attention?.note && (r.state === 'being handled' || r.state === 'dealt with') && (
            <Tooltip title={noteText(r.attention.note)}>
              <span style={{ fontSize: 12, color: claimStale(r.attention.note) ? token.colorWarning : secondary, maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.attention.note.actor || r.attention.note.os_user}{r.attention.note.note ? `: ${r.attention.note.note}` : ''}
              </span>
            </Tooltip>
          )}
          {scope === 'all' && <span style={{ fontSize: 12, color: muted }}>{SOURCE_LABEL[r.source]}</span>}
        </div>
      ),
    },
    {
      title: 'Schedule',
      key: 'id',
      render: (_: unknown, r) => r.kind === 'unreadable'
        ? <Text type="secondary" style={{ fontSize: 13 }}>venv couldn’t be read</Text>
        : (
          <span>
            {r.name && <div style={{ fontWeight: 600 }}>{r.name}</div>}
            <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: r.name ? muted : undefined }}>{r.id}</span>
            {r.fromPackage && (
              <Tooltip title={`Suggested by the package${r.shared?.package?.source ? ` (${r.shared.package.source})` : ''}${r.shared && packageOverridden(r.shared) ? `; this venv changes: ${(r.shared.package?.overridden ?? []).join(', ')}` : ''}`}>
                <Tag bordered={false} style={{ marginLeft: 6, fontSize: 11 }}>package{r.shared && packageOverridden(r.shared) ? ' · changed' : ''}</Tag>
              </Tooltip>
            )}
            {r.description && <div style={{ fontSize: 12, color: muted, maxWidth: 260 }}>{r.description}</div>}
          </span>
        ),
    },
    {
      title: 'Runs',
      key: 'runnable',
      render: (_: unknown, r) => (
        <div style={{ fontFamily: MONO_FONT, fontSize: 13, maxWidth: 320 }}>
          <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.runnable || <span style={{ color: muted }}>—</span>}
          </div>
          {r.argsText && (
            <Tooltip title={r.argsText}>
              <div style={{ color: muted, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.argsText}
              </div>
            </Tooltip>
          )}
        </div>
      ),
    },
    {
      // Cadence and the next fire in one column (they answer one question).
      title: 'When',
      key: 'when',
      render: (_: unknown, r) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <WhenCell when={r.when} />
          {r.nextRun && (
            <span style={{ fontSize: 12, color: secondary }}>next <When t={r.nextRun} /></span>
          )}
        </div>
      ),
    },
    {
      title: 'Last run',
      key: 'last',
      render: (_: unknown, r) => {
        const l = r.lastRun
        if (!l) return <span style={{ color: muted }}>—</span>
        const label = (l.status === 'failed' || l.status === 'crashed') && l.exitCode != null ? `exit ${l.exitCode}` : l.status
        return (
          <Space size={4}>
            <StatusTag tone={RUN_TONE[l.status] ?? 'neutral'}>{label}</StatusTag>
            <Tooltip title={`${fullWhen(l.ts)}${l.durationMs != null ? ` · ${(l.durationMs / 1000).toFixed(1)}s` : ''}`}>
              <span style={{ color: secondary, fontSize: 13 }}>{fmtWhen(l.ts)}</span>
            </Tooltip>
            {onOpenHistory && r.historySearch && (
              <IconAction label="Open in History" tip={`Open in History (search “${r.historySearch}”)`} icon={<HistoryOutlined />}
                onClick={() => onOpenHistory(r.historySearch)} />
            )}
          </Space>
        )
      },
    },
    {
      title: 'Where',
      key: 'where',
      render: (_: unknown, r) => (
        <div style={{ fontFamily: MONO_FONT, fontSize: 13 }}>
          <div style={{ whiteSpace: 'nowrap' }}>{r.host}{r.venv ? ` / ${r.venv}` : ''}</div>
          {(r.owner || r.runAs) && (
            <Tooltip title={`Venv owner${r.runAs ? ' → runs as' : ''}`}>
              <div style={{ color: muted, fontSize: 12, whiteSpace: 'nowrap' }}>
                {r.owner || '?'}{r.runAs && r.runAs !== r.owner ? ` → ${r.runAs}` : ''}
              </div>
            </Tooltip>
          )}
        </div>
      ),
    },
  ]

  if (editable) {
    columns.push({
      title: '',
      key: 'actions',
      width: 150,
      render: (_: unknown, r) => {
        const v = venvOf(r)
        if (!v) return null
        if (r.kind === 'stale' || (r.shared && needsSync(r))) {
          const syncBtn = (
            <Tooltip title={`Sync ${r.host} / ${r.venv}'s crontab with its config (only # runspec:sched: lines change)`}>
              <Button type="text" size="small" icon={<SyncOutlined />} aria-label="Sync crontab"
                loading={busy === v.key} onClick={() => syncVenv(v)} />
            </Tooltip>
          )
          if (r.kind === 'stale') return syncBtn
          return <Space size={0}>{syncBtn}{rowActions(r, v)}</Space>
        }
        return r.shared ? rowActions(r, v) : null
      },
    })
  }

  function rowActions(r: ScheduleRow, v: SchedulerVenv) {
    const s = r.shared as SharedSchedule
    const pkg = s.origin === 'package'
    // 0.10.0+ turns a schedule on / off without rewriting it (a package one keeps
    // following its package); older ones rewrite the whole entry.
    const toggle = (enabled: boolean) => (supportsPackages(v)
      ? bridge.host_schedule_enable({ host: v.host, venv: v.venv, backend: v.backend, id: s.id, enabled })
      : bridge.host_schedule_save(keepPayload(v, s, enabled)))
    const runNowBtn = (
      <Tooltip title="Run now">
        <AskFirst onConfirm={() => runNow(r, v, s)} ask={{
          level: 'local', title: `Run ${s.id} now?`, action: `${s.runnable || s.id} on ${r.host} / ${r.venv}${r.runAs ? ` as ${r.runAs}` : ''}`,
          consequence: `It starts in the background, the way a scheduled fire runs, and shows in History${s.enabled ? '' : ' (even though the schedule is off)'}. If it is already running, the new run is recorded as skipped.`,
          okText: 'Run now', danger: false,
        }}>
          <Button type="text" size="small" icon={<ThunderboltOutlined />} aria-label="Run now" loading={busy === `run::${r.key}`} />
        </AskFirst>
      </Tooltip>
    )
    const editBtn = (
      <Tooltip title={pkg ? 'Change it for this venv (only what you change is saved)' : 'Edit'}>
        <Button type="text" size="small" icon={<EditOutlined />} aria-label="Edit"
          onClick={() => { setEditing({ venv: v, schedule: s }); setEditorOpen(true) }} />
      </Tooltip>
    )
    if (s.offered) {
      const blocked = v.backend === 'cron' && !s.installable
      return (
        <Space size={0}>
          <Tooltip title={blocked ? (s.not_installable_reason ?? '') : `Turn on: it starts firing ${r.when}`}>
            <Button size="small" type="primary" ghost icon={<PlayCircleOutlined />} aria-label="Turn on"
              loading={busy === r.key} disabled={blocked}
              onClick={() => run(r.key, `Turned on ${s.id}`, () => toggle(true))}>Turn on</Button>
          </Tooltip>
          {runNowBtn}
          {editBtn}
        </Space>
      )
    }
    return (
      <Space size={0}>
        {runNowBtn}
        {editBtn}
        <Tooltip title={s.enabled ? (pkg ? 'Turn off (it stays offered by its package)' : 'Pause (stays in the config, never fires)') : 'Resume'}>
          <Button type="text" size="small" aria-label={s.enabled ? 'Pause' : 'Resume'}
            icon={s.enabled ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
            loading={busy === r.key}
            onClick={() => run(r.key, s.enabled ? `Paused ${s.id}` : `Resumed ${s.id}`, () => toggle(!s.enabled))} />
        </Tooltip>
        {pkg ? (
          packageOverridden(s) && (
            <Tooltip title="Put back to the package's settings">
              <AskFirst onConfirm={() => run(r.key, `Put back ${s.id}`,
                () => bridge.host_schedule_remove({ host: v.host, venv: v.venv, backend: v.backend, id: s.id }))} ask={{
                level: 'local', title: `Put ${s.id} back to its package's settings?`,
                action: `Drop this venv's changes: ${(s.package?.overridden ?? []).join(', ') || 'none'}`,
                consequence: `Whether it is on is dropped too; it then follows ${v.packages?.policy === 'auto' ? 'the package and stays on' : 'the venv’s package policy'}.`,
                okText: 'Put back',
              }}>
                <Button type="text" size="small" icon={<ReloadOutlined />} aria-label="Put back" />
              </AskFirst>
            </Tooltip>
          )
        ) : (
          <Tooltip title="Remove">
            <AskFirst onConfirm={() => run(r.key, `Removed ${s.id}`,
              () => bridge.host_schedule_remove({ host: v.host, venv: v.venv, backend: v.backend, id: s.id }))} ask={{
              level: 'local', title: 'Remove this schedule?', action: `${s.id} on ${r.host} / ${r.venv}`,
              consequence: v.backend === 'cron' ? 'Its crontab line is removed too.' : 'The daemon stops firing it.',
              okText: 'Remove',
            }}>
              <Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label="Remove" />
            </AskFirst>
          </Tooltip>
        )}
      </Space>
    )
  }

  const expanded = (r: ScheduleRow) => {
    if (r.shared) return <ScheduleDetail s={r.shared} daemon={r.source === 'daemon'} />
    if (r.kind === 'unreadable') return <Alert type="error" showIcon message="Couldn't read this venv's schedules" description={r.error} />
    if (r.kind === 'stale') return <Text type="secondary" style={{ fontSize: 13 }}>{r.stateTip}</Text>
    return (
      <div style={{ paddingLeft: 8, fontFamily: MONO_FONT, fontSize: 13, color: muted }}>
        {r.argsText || 'No arguments.'}
      </div>
    )
  }

  const selectStyle = { width: '100%' }
  const emptyText = !loaded && scope !== 'console'
    ? 'Reading schedules…'
    : scope === 'daemon' && collected.daemons.length === 0
      ? 'No venv uses the scheduler daemon yet. A venv moves to it on the host with runspec-scheduler 0.5.0 or later.'
      : narrowed ? 'No schedule matches these filters.' : 'No schedules.'

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <Input.Search
          allowClear
          aria-label="Search schedules"
          placeholder="Search id, runnable, args · host:web group:prod"
          value={filters.search}
          onChange={e => setFilters({ search: e.target.value })}
          style={{ width: 300 }}
        />
        {/* One Filter control instead of five, with the active filters as
            chips under the bar (critique: six filters crowded the page). */}
        <Popover trigger="click" placement="bottomLeft" open={filterOpen} onOpenChange={setFilterOpen}
          content={(
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: 280 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <Switch size="small" checked={filters.onlyProblems} onChange={v => setFilters({ onlyProblems: v })} aria-label="Only problems" />
                Only problems
              </label>
              {scope === 'all' && (
                <Select mode="multiple" allowClear placeholder="Any source" aria-label="Filter by source" style={selectStyle}
                  value={filters.source} onChange={v => setFilters({ source: v })}
                  options={(Object.keys(FILTER_SOURCE_LABEL) as RowSource[]).map(v => ({ label: FILTER_SOURCE_LABEL[v], value: v }))} />
              )}
              <Select mode="multiple" allowClear placeholder="Any state" aria-label="Filter by state" style={selectStyle}
                value={filters.states} onChange={v => setFilters({ states: v })}
                options={states.map(s => ({ label: s, value: s }))} />
              {scope !== 'console' && (
                <>
                  <Select mode="multiple" allowClear placeholder="Any owner" aria-label="Filter by owner" style={selectStyle}
                    value={filters.owners} onChange={v => setFilters({ owners: v })}
                    options={[...new Set([...owners, ...filters.owners])].map(o => ({ label: o, value: o }))} />
                  <Select mode="multiple" allowClear placeholder="Any run-as account" aria-label="Filter by run-as account" style={selectStyle}
                    value={filters.runAs} onChange={v => setFilters({ runAs: v })}
                    options={[...new Set([...runAs, ...filters.runAs])].map(o => ({ label: o, value: o }))} />
                </>
              )}
            </div>
          )}>
          <Button icon={<FilterOutlined />} aria-label={chips.length ? `Filter, ${chips.length} active` : 'Filter'}>
            Filter{chips.length ? ` · ${chips.length}` : ''}
          </Button>
        </Popover>
        <Select size="middle" aria-label="Sort schedules" value={filters.sort} onChange={v => setFilters({ sort: v })} style={{ width: 170 }}
          options={[
            { label: 'Sort: next fire', value: 'next' },
            { label: 'Sort: last run', value: 'last' },
            { label: 'Sort: host', value: 'host' },
            { label: 'Sort: runnable', value: 'runnable' },
          ]} />
        {scope !== 'console' && (
          <Space size={8} style={{ marginLeft: 'auto' }}>
            {activeProfile && (
              <Tooltip title={`Host schedules come from the connected hosts in the active profile "${activeProfile}" (and the local venvs).`}>
                <Tag style={{ margin: 0 }}>profile: {activeProfile}</Tag>
              </Tooltip>
            )}
            <span style={{ fontSize: 12, color: muted }}>
              {hostCount} host{hostCount === 1 ? '' : 's'}{updatedAt ? ` · read ${fmtWhen(Math.min(updatedAt, Date.now()))}` : ''}
            </span>
            <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>Refresh</Button>
            {editable && scopeVenvs.length > 0 && (
              scopeVenvs.length === 1
                ? <Button size="small" icon={<SettingOutlined />} onClick={() => setSettingsVenv(scopeVenvs[0])}>Venv settings</Button>
                : (
                  <Dropdown trigger={['click']} menu={{
                    items: scopeVenvs.map(v => ({ key: v.key, label: <span><span style={{ fontFamily: MONO_FONT }}>{v.host} / {v.venv}</span> <Text type="secondary" style={{ fontSize: 12 }}>{v.backend}</Text></span> })),
                    onClick: ({ key }) => setSettingsVenv(scopeVenvs.find(v => v.key === key) ?? null),
                  }}>
                    <Button size="small" icon={<SettingOutlined />}>Venv settings <DownOutlined style={{ fontSize: 10 }} /></Button>
                  </Dropdown>
                )
            )}
            {editable && (scopeVenvs.length
              ? (
                <Tooltip title="Add a schedule to a venv’s runspec-scheduler">
                  <Button size="small" type="primary" icon={<PlusOutlined />}
                    onClick={() => { setEditing(null); setEditorOpen(true) }}>
                    New host schedule
                  </Button>
                </Tooltip>
              )
              // Say what's missing instead of a disabled button with no way on.
              : <span data-testid="no-scheduler" style={{ fontSize: 12, color: muted }}>
                  To schedule on a host, install runspec-scheduler in one of its venvs (Marketplace).
                </span>
            )}
          </Space>
        )}
      </div>

      {chips.length > 0 && (
        <div data-testid="filter-chips" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', margin: '-4px 0 12px' }}>
          <span style={{ fontSize: 12, color: muted }}>
            Showing {rows.length} of {scoped.length}:
          </span>
          {chips.map(ch => (
            <Tag key={ch.key} closable onClose={e => { e.preventDefault(); setFilters(ch.clear) }}
              closeIcon={<CloseOutlined aria-label={`Remove filter ${ch.label}`} />} style={{ margin: 0 }}>
              {ch.label}
            </Tag>
          ))}
          <Button size="small" type="link" style={{ padding: 0 }}
            onClick={() => setFilters({ onlyProblems: false, source: [], states: [], owners: [], runAs: [] })}>
            Show all
          </Button>
        </div>
      )}

      {daemons.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
          <button type="button" onClick={toggleDaemons} aria-expanded={daemonsShown}
            style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0', border: 'none', background: 'none',
              cursor: 'pointer', color: secondary, fontSize: 13, textAlign: 'left',
            }}>
            {daemonsShown ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
            <span>{daemonSummary(daemons)}</span>
            {!daemonsShown && foldedHidden > 0 && <span style={{ color: muted }}>· show all</span>}
          </button>
          {visibleDaemons.map(d => (
            <DaemonStrip key={d.key} d={d} editable={editable} busy={busy === `daemon::${d.key}`}
              onRestart={restartDaemon} onSettings={openDaemonSettings} />
          ))}
        </div>
      )}

      {notes.map(n => (
        <Alert key={n.key} type="error" showIcon style={{ marginBottom: 8 }}
          message={<span><span style={{ fontFamily: MONO_FONT }}>{n.host} / {n.venv}</span>: {n.message}</span>} />
      ))}

      {editable && rows.some(noteable) && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8, padding: '6px 10px',
          borderRadius: RADIUS.md, background: token.colorFillTertiary, fontSize: 13,
        }}>
          {selectedRows.length
            ? <span style={{ color: textColor(isDark), fontWeight: 500 }}>{selectedRows.length} failing schedule{selectedRows.length === 1 ? '' : 's'} selected</span>
            : <span style={{ color: secondary }}>Tick failing schedules to say you're on them, or that they were dealt with, so nobody else picks them up.</span>}
          {verbs.map(v => (
            <Button key={v} size="small" type={v === 'claim' ? 'primary' : 'default'} onClick={() => setNoteVerb(v)}>
              {NOTE_WORDS[v].button}
            </Button>
          ))}
          {selectedRows.length > 0 && <Button size="small" type="text" onClick={() => setSelected([])}>Clear</Button>}
        </div>
      )}

      {rows.length === 0 && !loaded && scope !== 'console' ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spin /></div>
      ) : (
        <Table<ScheduleRow>
          dataSource={rows}
          columns={columns}
          rowKey="key"
          size="small"
          pagination={rows.length > 100 ? { pageSize: 100, showSizeChanger: false } : false}
          scroll={{ x: 'max-content' }}
          rowClassName={r => (r.enabled ? '' : 'rs-schedule-disabled')}
          onRow={r => (r.problem ? { style: { background: withAlpha(token.colorWarning, isDark ? 0.07 : 0.08) } } : {})}
          locale={{ emptyText: <EmptyState message={emptyText} /> }}
          expandable={{ expandedRowRender: expanded, rowExpandable: r => r.kind !== 'schedule' || !!r.shared || !!r.argsText }}
          rowSelection={editable && rows.some(noteable) ? {
            selectedRowKeys: selected,
            onChange: keys => setSelected(keys.map(String)),
            getCheckboxProps: r => ({ disabled: !noteable(r), 'aria-label': `Select ${r.id}` }),
            renderCell: (_checked, r, _i, node) => (noteable(r) ? node : null),
          } : undefined}
        />
      )}

      {scope !== 'console' && collected.without.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: muted }}>
          No runspec-scheduler in {collected.without.length} venv{collected.without.length === 1 ? '' : 's'}: {collected.without.join(', ')}. Install it into a venv from the Marketplace to schedule that venv's runnables.
        </div>
      )}
      {scope !== 'console' && collected.rootOwned.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 12, color: muted }} data-testid="root-owned-venvs">
          Root-owned, left out of shared schedules: {collected.rootOwned.join(', ')}. Every fire there would run as root, so schedule their runnables from This console instead.
        </div>
      )}
      {scope !== 'console' && (
        <div style={{ marginTop: 6, fontSize: 12, color: muted }}>
          <InfoCircleOutlined style={{ marginRight: 4 }} />
          {editable
            ? <>Host schedule changes run the venv’s own <Text code style={{ fontSize: 12 }}>runspec-scheduler</Text> as its owner. On a cron venv the crontab is synced after each change, and only lines marked <Text code style={{ fontSize: 12 }}># runspec:sched:</Text> change.</>
            : <>Read-only: host schedules are managed on the host with <Text code style={{ fontSize: 12 }}>runspec-scheduler</Text>.</>}
        </div>
      )}
      {editable && (
        <HostScheduleModal
          open={editorOpen}
          venvs={editing ? collected.venvs : scopeVenvs}
          runnables={runnables ?? []}
          idsByVenv={idsByVenv}
          editing={editing}
          onClose={() => setEditorOpen(false)}
          onSaved={res => {
            setEditorOpen(false)
            afterChange(editing ? `Saved ${res.id ?? ''}`.trim() : `Added ${res.id ?? ''}`.trim(), res)
          }}
        />
      )}
      {editable && (
        <NoteModal verb={noteVerb} rows={selectedRows} onClose={() => setNoteVerb(null)}
          onDone={() => { setNoteVerb(null); setSelected([]); onRefresh() }} />
      )}
      {editable && (
        <SchedulerSettingsModal v={settingsVenv}
          log={collected.daemons.find(d => d.key === settingsVenv?.key)?.log}
          onClose={() => setSettingsVenv(null)}
          onSaved={() => { setSettingsVenv(null); onRefresh() }} />
      )}
    </div>
  )
}
