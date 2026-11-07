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
import { Table, Tag, Typography, Button, Tooltip, Alert, Empty, Input, Modal, Select, Switch, Space, Spin, Popconfirm, message, theme } from 'antd'
import {
  ReloadOutlined, HistoryOutlined, InfoCircleOutlined, PlusOutlined, EditOutlined, DeleteOutlined,
  PauseCircleOutlined, PlayCircleOutlined, SyncOutlined, SettingOutlined, RightOutlined, DownOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HostScheduleResult, type HostScheduleSave, type Runnable, type SharedSchedule } from '../bridge'
import { HostScheduleModal, timeoutText, type HostScheduleEditing } from './HostScheduleModal'
import { MONO_FONT, RADIUS, useIsDark, textColor, textMuted, textSecondary, withAlpha } from '../ThemeContext'
import {
  DAEMON_SETTINGS, DEFAULT_FILTERS, FOLDED_ATTENTION_MAX, STATE_COLOR, daemonNeedsAttention, daemonSummary, filterRows, settingText, settingsChanges, shortDuration,
  type Collected, type DaemonVenv, type RowFilters, type RowSource, type RowState, type ScheduleRow, type ScheduleScope, type SchedulerVenv, type SortKey,
} from './allSchedules'
import { RUN_COLOR, ScheduleDetail, relTime } from './sharedSchedules'

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
const SOURCE_COLOR: Record<RowSource, string> = {
  console: 'cyan', cron: 'geekblue', daemon: 'purple', unknown: 'default',
}

const ALL_STATES: RowState[] = [
  'ok', 'running', 'last run failed', 'not installed', 'not installable', 'stale line',
  'scheduler stopped', 'scheduler stale', "can't read", 'disabled', 'unknown', 'legacy line',
]

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
  const color = st === 'running' ? 'green' : st === 'stale' ? 'orange' : 'red'
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
      <Tag color={color} style={{ margin: 0 }}>scheduler {st}</Tag>
      {dm?.restart_needed && running && (
        <Tooltip title="The daemon still runs the runspec-scheduler it started with; the venv has a different one installed now. Restart it to run the new code.">
          <Tag color="gold" style={{ margin: 0 }}>restart needed</Tag>
        </Tooltip>
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
          <Popconfirm title={confirm} okText={verb} onConfirm={() => onRestart(d)} overlayStyle={{ maxWidth: 360 }}>
            <Button size="small" icon={running ? <ReloadOutlined /> : <PlayCircleOutlined />} loading={busy}
              type={!running || dm?.restart_needed ? 'primary' : 'default'}>{verb}</Button>
          </Popconfirm>
        </Space>
      )}
    </div>
  )
}

/** View and change a venv daemon's `[scheduler]` durations. The backend is
 *  shown read-only (Update venv… moves a venv between cron and the daemon). */
export function DaemonSettingsModal({ d, onClose, onSaved, zIndex }: {
  d: DaemonVenv | null
  onClose: () => void
  onSaved: () => void
  zIndex?: number
}) {
  const [form, setForm] = useState<Partial<Record<'timeout' | 'misfire_grace' | 'catch_up_window', string>>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const cur = d?.settings ?? null
  useEffect(() => {
    setError('')
    setForm(cur ? Object.fromEntries(DAEMON_SETTINGS.map(({ key }) => [key, settingText(cur[key])])) : {})
  }, [d?.key, cur])
  const changes = settingsChanges(cur, form)
  const save = async () => {
    if (!d || !changes) return
    setSaving(true); setError('')
    try {
      const res = await bridge.host_scheduler_settings({ host: d.host, venv: d.venv, ...changes })
      if (!res.ok) { setError(res.error || 'Saving the settings failed.'); return }
      message.success(`Saved the scheduler settings on ${d.host} / ${d.venv}; the daemon picks them up by itself.`)
      onSaved()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }
  return (
    <Modal open={!!d} onCancel={onClose} zIndex={zIndex} width={560} destroyOnHidden
      title={d ? <span>Scheduler settings · <span style={{ fontFamily: MONO_FONT }}>{d.host} / {d.venv}</span></span> : null}
      okText="Save" onOk={save} okButtonProps={{ disabled: !changes, loading: saving }}>
      {d && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!cur && (
            <Alert type="info" showIcon message="This venv's runspec-scheduler is older than 0.7.0, so its settings can't be read. Saving sets the values you type; upgrade it (Update venv…) to see them here." />
          )}
          <div style={{ fontSize: 13 }}>
            <Text type="secondary">Fires from: </Text><Text strong>daemon</Text>
            <Text type="secondary"> (Update venv… moves a venv between crontab and the daemon)</Text>
          </div>
          {DAEMON_SETTINGS.map(({ key, label, help }) => (
            <div key={key}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 130, fontWeight: 500 }}>{label}</span>
                <Input size="small" style={{ width: 140, fontFamily: MONO_FONT }} value={form[key] ?? ''}
                  placeholder={cur ? settingText(cur[key]) : 'e.g. 6h'}
                  onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))} aria-label={label} />
              </div>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 138, display: 'block' }}>{help}</Text>
            </div>
          ))}
          <Text type="secondary" style={{ fontSize: 12 }}>Seconds, or a duration like 90m, 6h or 1d. Saved with the venv's own runspec-scheduler, as {d.owner || 'the venv owner'}.</Text>
          {d.log && <Text type="secondary" style={{ fontSize: 12 }}>Daemon log: <span style={{ fontFamily: MONO_FONT }}>{d.log}</span></Text>}
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
  const timeout = timeoutText(s.timeout, v.backend)
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

  const scoped = useMemo(() => filterRows(collected.rows, scope, { ...DEFAULT_FILTERS, sort: filters.sort }), [collected.rows, scope, filters.sort])
  const rows = useMemo(() => filterRows(collected.rows, scope, filters), [collected.rows, scope, filters])
  const owners = useMemo(() => [...new Set(scoped.map(r => r.owner).filter(Boolean))].sort(), [scoped])
  const runAs = useMemo(() => [...new Set(scoped.map(r => r.runAs).filter(Boolean))].sort(), [scoped])
  const states = useMemo(() => ALL_STATES.filter(s => scoped.some(r => r.state === s) || filters.states.includes(s)), [scoped, filters.states])
  const notes = collected.notes.filter(n => scope === 'all' || n.source === scope)
  const daemons = scope === 'daemon' || scope === 'all' ? collected.daemons : []
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
  const [daemonsOpen, setDaemonsOpen] = useState<boolean | null>(readDaemonsOpen)
  const daemonsShown = daemonsOpen ?? daemons.length <= FOLDED_ATTENTION_MAX
  const toggleDaemons = () => { writeDaemonsOpen(!daemonsShown); setDaemonsOpen(!daemonsShown) }
  const visibleDaemons = daemonsShown ? daemons : daemons.filter(daemonNeedsAttention).slice(0, FOLDED_ATTENTION_MAX)
  const foldedHidden = daemons.length - visibleDaemons.length
  const [daemonSettings, setDaemonSettings] = useState<DaemonVenv | null>(null)
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
  const syncVenv = (v: SchedulerVenv) => run(v.key, `Synced ${v.host} / ${v.venv}`, () => bridge.host_schedule_sync({ host: v.host, venv: v.venv }))

  const columns: ColumnType<ScheduleRow>[] = [
    {
      title: 'State',
      key: 'state',
      width: 150,
      render: (_: unknown, r) => (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
          <Tooltip title={r.stateTip}>
            <Tag color={STATE_COLOR[r.state]} style={{ margin: 0 }}>{r.state}</Tag>
          </Tooltip>
          {scope === 'all' && <Tag bordered={false} color={SOURCE_COLOR[r.source]} style={{ margin: 0, fontSize: 11 }}>{SOURCE_LABEL[r.source]}</Tag>}
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
    { title: 'When', key: 'when', render: (_: unknown, r) => <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{r.when}</span> },
    {
      title: 'Next fire',
      key: 'next',
      render: (_: unknown, r) => r.nextRun
        ? <Tooltip title={new Date(r.nextRun).toLocaleString()}><span style={{ color: secondary }}>{relTime(r.nextRun)}</span></Tooltip>
        : <span style={{ color: muted }}>—</span>,
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
            <Tag color={RUN_COLOR[l.status] ?? 'default'} style={{ margin: 0 }}>{label}</Tag>
            <Tooltip title={`${new Date(l.ts).toLocaleString()}${l.durationMs != null ? ` · ${(l.durationMs / 1000).toFixed(1)}s` : ''}`}>
              <span style={{ color: secondary, fontSize: 13 }}>{relTime(l.ts)}</span>
            </Tooltip>
            {onOpenHistory && r.historySearch && (
              <Tooltip title={`Open in History (search “${r.historySearch}”)`}>
                <Button type="text" size="small" icon={<HistoryOutlined />} aria-label="Open in History"
                  onClick={() => onOpenHistory(r.historySearch)} />
              </Tooltip>
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
      width: 120,
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
    return (
      <Space size={0}>
        <Tooltip title="Edit">
          <Button type="text" size="small" icon={<EditOutlined />} aria-label="Edit"
            onClick={() => { setEditing({ venv: v, schedule: s }); setEditorOpen(true) }} />
        </Tooltip>
        <Tooltip title={s.enabled ? 'Pause (stays in the config, never fires)' : 'Resume'}>
          <Button type="text" size="small" aria-label={s.enabled ? 'Pause' : 'Resume'}
            icon={s.enabled ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
            loading={busy === r.key}
            onClick={() => run(r.key, s.enabled ? `Paused ${s.id}` : `Resumed ${s.id}`,
              () => bridge.host_schedule_save(keepPayload(v, s, !s.enabled)))} />
        </Tooltip>
        <Popconfirm
          title={`Remove ${s.id} from ${r.host} / ${r.venv}?`}
          description={v.backend === 'cron' ? 'Its crontab line is removed too.' : 'The daemon stops firing it.'}
          okText="Remove" okButtonProps={{ danger: true }}
          onConfirm={() => run(r.key, `Removed ${s.id}`,
            () => bridge.host_schedule_remove({ host: v.host, venv: v.venv, backend: v.backend, id: s.id }))}>
          <Tooltip title="Remove">
            <Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label="Remove" />
          </Tooltip>
        </Popconfirm>
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

  const selectStyle = { minWidth: 130 }
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
          placeholder="Search id, runnable, args · host:web group:prod"
          value={filters.search}
          onChange={e => setFilters({ search: e.target.value })}
          style={{ width: 300 }}
        />
        {scope === 'all' && (
          <Select mode="multiple" allowClear placeholder="Source" style={selectStyle} maxTagCount="responsive"
            value={filters.source} onChange={v => setFilters({ source: v })}
            options={[
              { label: 'This console', value: 'console' },
              { label: 'Host cron', value: 'cron' },
              { label: 'Host daemon', value: 'daemon' },
              { label: 'Unreadable venv', value: 'unknown' },
            ]} />
        )}
        <Select mode="multiple" allowClear placeholder="State" style={selectStyle} maxTagCount="responsive"
          value={filters.states} onChange={v => setFilters({ states: v })}
          options={states.map(s => ({ label: s, value: s }))} />
        {scope !== 'console' && (
          <>
            <Select mode="multiple" allowClear placeholder="Owner" style={selectStyle} maxTagCount="responsive"
              value={filters.owners} onChange={v => setFilters({ owners: v })}
              options={[...new Set([...owners, ...filters.owners])].map(o => ({ label: o, value: o }))} />
            <Select mode="multiple" allowClear placeholder="Run as" style={selectStyle} maxTagCount="responsive"
              value={filters.runAs} onChange={v => setFilters({ runAs: v })}
              options={[...new Set([...runAs, ...filters.runAs])].map(o => ({ label: o, value: o }))} />
          </>
        )}
        <Space size={6}>
          <Switch size="small" checked={filters.onlyProblems} onChange={v => setFilters({ onlyProblems: v })} aria-label="Only problems" />
          <span style={{ fontSize: 13, color: secondary }}>Only problems</span>
        </Space>
        <Select size="middle" value={filters.sort} onChange={v => setFilters({ sort: v })} style={{ width: 170 }}
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
              {hostCount} host{hostCount === 1 ? '' : 's'}{updatedAt ? ` · read ${Date.now() - updatedAt < 10_000 ? 'just now' : relTime(new Date(Math.min(updatedAt, Date.now())).toISOString())}` : ''}
            </span>
            <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>Refresh</Button>
            {editable && (
              <Tooltip title={scopeVenvs.length ? 'Add a schedule to a venv’s runspec-scheduler' : 'No venv with runspec-scheduler on the connected hosts'}>
                <Button size="small" type="primary" icon={<PlusOutlined />} disabled={!scopeVenvs.length}
                  onClick={() => { setEditing(null); setEditorOpen(true) }}>
                  New host schedule
                </Button>
              </Tooltip>
            )}
          </Space>
        )}
      </div>

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
              onRestart={restartDaemon} onSettings={setDaemonSettings} />
          ))}
        </div>
      )}

      {notes.map(n => (
        <Alert key={n.key} type="error" showIcon style={{ marginBottom: 8 }}
          message={<span><span style={{ fontFamily: MONO_FONT }}>{n.host} / {n.venv}</span>: {n.message}</span>} />
      ))}

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
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} /> }}
          expandable={{ expandedRowRender: expanded, rowExpandable: r => r.kind !== 'schedule' || !!r.shared || !!r.argsText }}
        />
      )}

      {scope !== 'console' && collected.without.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: muted }}>
          No runspec-scheduler in {collected.without.length} venv{collected.without.length === 1 ? '' : 's'}: {collected.without.join(', ')}. Install it into a venv from the Marketplace to schedule that venv's runnables.
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
        <DaemonSettingsModal d={daemonSettings} onClose={() => setDaemonSettings(null)}
          onSaved={() => { setDaemonSettings(null); onRefresh() }} />
      )}
    </div>
  )
}
