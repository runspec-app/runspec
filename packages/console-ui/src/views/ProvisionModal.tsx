import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Input, Checkbox, Select, Segmented, Tag, Alert, Collapse, Space, Tooltip, message } from 'antd'
import { CloudServerOutlined, ReloadOutlined, CheckCircleFilled, CloseCircleFilled, LoadingOutlined, WarningFilled } from '@ant-design/icons'
import {
  bridge,
  type ProvisionOptions,
  type ProvisionInterpreter,
  type ProvisionPayload,
  type ProvisionPreflightRow,
  type ProvisionResult,
  type ProvisionSchedulerKeeper,
  type ProvisionSchedulerSettings,
  type ProvisionRetention,
  type PackageSchedulePolicy,
} from '../bridge'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'
import { PathInput } from './PathInput'
import { StatusIcon, StatusTag, pressable } from '../components/kit'

const { Text } = Typography

type PyMode = 'path' | 'version'

export interface HostProgress {
  step: string
  lines: string[]
  done: boolean
  ok?: boolean
  summary?: string
  warnings?: string[]
  scheduler?: { manager: string; reason: string }
}

/** Join a parent directory and a venv folder name into an absolute POSIX path. */
export function joinDest(parent: string, name: string): string {
  const p = (parent || '').trim().replace(/\/+$/, '')
  const n = (name || '').trim().replace(/^\/+|\/+$/g, '')
  if (!p || !n) return ''
  return `${p}/${n}`
}

/** Host names a group / profile chip ticks (connected hosts only). */
export function membersOf(
  hosts: ProvisionOptions['hosts'],
  kind: 'group' | 'profile',
  name: string,
): string[] {
  return hosts
    .filter(h => h.connected && (kind === 'group' ? h.group === name : h.profiles.includes(name)))
    .map(h => h.name)
}

/** The provision request the form describes (shared by preflight and start). */
/** Shown wherever a root-owned venv would get shared schedules (0.406.0). */
export const ROOT_SCHEDULES_NOTE =
  "Root-owned venv: shared schedules aren't set up here (every fire would run as root). Schedule its runnables from This console instead."

/** True when the venv will belong to root, so it gets no shared schedules. */
export function rootOwnedRunAs(runAs: string): boolean {
  return runAs.trim() === 'root'
}

export function buildPayload(f: {
  hosts: string[]
  parent: string
  name: string
  pyMode: PyMode
  pyPath: string
  pyVersion: string
  runAs: string
  become: 'sudo' | 'su'
  packages: string[]
  repo: string
  scheduler?: '' | ProvisionSchedulerKeeper
  retention?: ProvisionRetention | null
  schedulerSettings?: ProvisionSchedulerSettings
}): ProvisionPayload {
  const settings = f.scheduler ? schedulerSettingsPayload(f.schedulerSettings) : undefined
  return {
    hosts: f.hosts,
    dest: joinDest(f.parent, f.name),
    python: (f.pyMode === 'path' ? f.pyPath : f.pyVersion).trim(),
    run_as: f.runAs.trim(),
    become_method: f.become,
    packages: f.packages.map(p => p.trim()).filter(Boolean),
    repo: f.repo || undefined,
    scheduler: f.scheduler || undefined,
    retention: f.retention ? retentionPayload(f.retention) : undefined,
    ...(settings ? { scheduler_settings: settings } : {}),
  }
}

/** The scheduler zone / package policy to send, blanks dropped; undefined when
 *  nothing is set (the venv keeps the host's zone and "offer"). */
export function schedulerSettingsPayload(s: ProvisionSchedulerSettings | undefined): ProvisionSchedulerSettings | undefined {
  const out: ProvisionSchedulerSettings = {}
  const zone = (s?.timezone ?? '').trim()
  if (zone) out.timezone = zone
  if (s?.package_schedules) out.package_schedules = s.package_schedules
  return Object.keys(out).length ? out : undefined
}

/** What a venv does with schedules its packages suggest (runspec-scheduler 0.10.0+). */
export const PACKAGE_POLICY_OPTIONS: { value: PackageSchedulePolicy; label: string; help: string }[] = [
  { value: 'offer', label: 'Offer them (off until turned on)', help: 'They show on the Schedules tab, off, until someone turns each one on.' },
  { value: 'auto', label: 'Turn them on', help: 'They run as soon as the package is installed.' },
  { value: 'off', label: 'Ignore them', help: 'They don’t show and never run.' },
]

/** The schedule time zone + package schedules fields, shared by Provision venv
 *  and Update venv. `leave` adds a "leave as it is" policy choice (Update). */
export function SchedulerSettingsFields({ value, onChange, leave = false, hint }: {
  value: ProvisionSchedulerSettings
  onChange: (v: ProvisionSchedulerSettings) => void
  leave?: boolean
  hint?: string
}) {
  const isDark = useIsDark()
  const policy = value.package_schedules ?? ''
  const help = PACKAGE_POLICY_OPTIONS.find(o => o.value === policy)?.help
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
      <div>
        <div><Text style={{ fontSize: 12, color: textSecondary(isDark) }}>Schedule time zone</Text></div>
        <Input value={value.timezone ?? ''} allowClear aria-label="Schedule time zone" style={{ fontFamily: MONO_FONT }}
          placeholder={leave ? 'leave as it is' : "the host's"}
          onChange={e => onChange({ ...value, timezone: e.target.value })} />
        <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
          An IANA zone like Europe/London for schedules with none of their own.{hint ? ` ${hint}` : ''}
        </Text>
      </div>
      <div>
        <div><Text style={{ fontSize: 12, color: textSecondary(isDark) }}>Package schedules</Text></div>
        <Select value={policy} aria-label="Package schedules" style={{ width: '100%' }}
          onChange={(v: PackageSchedulePolicy | '') => onChange({ ...value, package_schedules: v })}
          options={[
            ...(leave ? [{ value: '', label: 'Leave as it is' }] : []),
            ...PACKAGE_POLICY_OPTIONS.map(o => ({ value: o.value, label: o.label })),
          ]} />
        <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
          Schedules a package suggests in its runspec.toml. {help ?? ''}
        </Text>
      </div>
    </div>
  )
}

/** What the log cleanup defaults to: compress after a week, delete after ~3 months. */
export const RETENTION_DEFAULT: ProvisionRetention = { compact_after: '7d', delete_after: '90d' }
const DURATION_RE = /^[1-9]\d*[smhdw]$/
const DURATION_S: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 }

function durationS(v: string): number {
  return Number(v.slice(0, -1)) * DURATION_S[v.slice(-1)]
}

/** Words that switch one cleanup step off. A blank field means the same. */
const OFF_WORDS = new Set(['', 'off', 'never', 'none', 'no', '0'])

/** True when a compress-after / delete-after value switches that step off. */
export function retentionOff(v: string | undefined): boolean {
  return OFF_WORDS.has((v ?? '').trim().toLowerCase())
}

/** The policy to send: each field trimmed and lower-cased, and a blank or
 * "off"/"never"/"0" field sent as "off" so the step is switched off (or
 * cleared, when the venv already has a policy). */
export function retentionPayload(r: ProvisionRetention): ProvisionRetention {
  const norm = (v: string | undefined) => (retentionOff(v) ? 'off' : (v ?? '').trim().toLowerCase())
  return { compact_after: norm(r.compact_after), delete_after: norm(r.delete_after) }
}

/** Why a log cleanup setting can't be used, or '' when it's fine. */
export function retentionProblem(r: ProvisionRetention): string {
  const { compact_after: c = 'off', delete_after: d = 'off' } = retentionPayload(r)
  if (c === 'off' && d === 'off') return 'Set at least one of compress after / delete after (blank or off turns one off).'
  for (const v of [c, d]) if (v !== 'off' && !DURATION_RE.test(v)) return `"${v}" isn't a duration: use a number and s/m/h/d/w, like 7d or 2w, or off.`
  if (c !== 'off' && d !== 'off' && durationS(c) >= durationS(d)) return 'Compress after must be shorter than delete after.'
  return ''
}

/** The keepers the dialog offers for the scheduler daemon. */
export const SCHEDULER_OPTIONS: { value: '' | ProvisionSchedulerKeeper; label: string }[] = [
  { value: '', label: 'No scheduler daemon' },
  { value: 'auto', label: 'Daemon, keeper picked per host' },
  { value: 'systemd-user', label: 'Daemon under systemd --user' },
  { value: 'supervisor', label: 'Daemon under supervisord in the venv' },
  { value: 'cron', label: 'Daemon with a crontab watchdog' },
]

export const STEP_LABEL: Record<string, string> = {
  preflight: 'checking',
  inspect: 'checking',
  folder: 'creating folder as root',
  create: 'creating venv',
  pip: 'upgrading pip',
  install: 'installing packages',
  permissions: 'making the venv readable by other accounts',
  inbox: 'setting up logs inbox',
  retention: 'setting log cleanup',
  verify: 'verifying',
  access: 'checking the console can run it',
  scheduler: 'switching schedules to the daemon',
  keeper: 'starting the scheduler daemon',
  settings: 'setting the schedule zone and package schedules',
  sync: 'bringing the crontab up to date',
  restart: 'restarting the scheduler daemon',
}

/** One ``runspec:provision_progress`` event's detail. */
export interface ProgressEvent {
  op: string
  host: string
  phase: string
  step?: string
  line?: string
  ok?: boolean
  summary?: string
  warnings?: string[]
  scheduler?: { manager: string; reason: string }
}

/** Fold a progress event into the per-host progress map. */
export function applyProgress(prev: Record<string, HostProgress>, d: ProgressEvent): Record<string, HostProgress> {
  const cur: HostProgress = prev[d.host] ?? { step: '', lines: [], done: false }
  const next = { ...cur }
  if (d.phase === 'step' && d.step) next.step = d.step
  if (d.phase === 'line' && d.line !== undefined) next.lines = [...cur.lines, d.line].slice(-400)
  if (d.phase === 'done') { next.done = true; next.ok = d.ok; next.summary = d.summary; next.warnings = d.warnings; next.scheduler = d.scheduler }
  return { ...prev, [d.host]: next }
}

/** Per-host live progress of a running provision / update: step, then the
 *  result, with the host's output in a collapsible panel. */
export function ProgressPanel({ progress, stepLabel = STEP_LABEL }: { progress: Record<string, HostProgress>; stepLabel?: Record<string, string> }) {
  return (
    <Collapse
      size="small"
      style={{ marginTop: 14 }}
      items={Object.keys(progress).length === 0
        ? [{ key: 'wait', label: <Text type="secondary"><LoadingOutlined /> Starting…</Text>, children: null }]
        : Object.entries(progress).map(([host, p]) => ({
          key: host,
          label: (
            <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {!p.done ? <LoadingOutlined aria-label="working" /> : <StatusIcon status={p.ok ? 'ok' : 'failed'} />}
              <span style={{ fontFamily: MONO_FONT }}>{host}</span>
              <Text type="secondary" style={{ fontSize: 12 }}>{p.done ? p.summary : stepLabel[p.step] ?? p.step}</Text>
              {p.scheduler && <Tooltip title={p.scheduler.reason}><Tag style={{ margin: 0, fontSize: 11 }}>scheduler: {p.scheduler.manager}</Tag></Tooltip>}
              {(p.warnings ?? []).length > 0 && <Tooltip title={(p.warnings ?? []).join('\n')}><span><StatusIcon status="warn" /></span></Tooltip>}
            </span>
          ),
          children: (
            <pre style={{ margin: 0, maxHeight: 220, overflow: 'auto', fontFamily: MONO_FONT, fontSize: 11, whiteSpace: 'pre-wrap' }}>
              {p.lines.join('\n') || '…'}
            </pre>
          ),
        }))}
    />
  )
}

/** The Log cleanup controls: a checkbox plus compress / delete durations. */
export function LogCleanupField({
  checked,
  onChecked,
  checkboxLabel = 'Clean up old run logs',
  compactAfter,
  onCompactAfter,
  deleteAfter,
  onDeleteAfter,
  error,
  hint,
}: {
  checked: boolean
  onChecked: (v: boolean) => void
  checkboxLabel?: string
  compactAfter: string
  onCompactAfter: (v: string) => void
  deleteAfter: string
  onDeleteAfter: (v: string) => void
  error: string
  hint: string
}) {
  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Checkbox checked={checked} onChange={e => onChecked(e.target.checked)}>{checkboxLabel}</Checkbox>
        {checked && (
          <>
            <Text style={{ fontSize: 12 }}>compress after</Text>
            <Input size="small" value={compactAfter} onChange={e => onCompactAfter(e.target.value)} placeholder="off" style={{ width: 70 }} aria-label="Compress after" />
            <Text style={{ fontSize: 12 }}>delete after</Text>
            <Input size="small" value={deleteAfter} onChange={e => onDeleteAfter(e.target.value)} placeholder="off" style={{ width: 70 }} aria-label="Delete after" />
          </>
        )}
      </div>
      <Text type={error ? 'danger' : 'secondary'} style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
        {error || hint}
      </Text>
    </>
  )
}

// Provision a runspec venv on one or many hosts. Modelled on Copy key to all
// hosts: a host checklist (with group / profile quick-picks), one location,
// interpreter, run-as account and package list for every host, a preflight
// table that changes nothing, then a parallel run with per-host logs.
export function ProvisionModal({
  open,
  initialHosts,
  onClose,
  onDone,
  zIndex,
}: {
  open: boolean
  initialHosts?: string[]
  onClose: () => void
  onDone?: () => void
  // Stack above a modal it is opened from (Ant siblings share zIndex 1000,
  // so render order alone can leave this dialog hidden underneath).
  zIndex?: number
}) {
  const isDark = useIsDark()
  const [opts, setOpts] = useState<ProvisionOptions | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [parent, setParent] = useState('/opt/venvs')
  const [name, setName] = useState('')
  const [pyMode, setPyMode] = useState<PyMode>('path')
  const [pyPath, setPyPath] = useState('')
  const [browsePy, setBrowsePy] = useState('')
  const [pyVersion, setPyVersion] = useState('3.12')
  const [pythons, setPythons] = useState<ProvisionInterpreter[]>([])
  const [pyLoading, setPyLoading] = useState(false)
  const [runAs, setRunAs] = useState('')
  const rootOwner = rootOwnedRunAs(runAs)
  const [become, setBecome] = useState<'sudo' | 'su'>('sudo')
  const [packages, setPackages] = useState<string[]>([])
  const [repo, setRepo] = useState('')
  const [scheduler, setScheduler] = useState<'' | ProvisionSchedulerKeeper>('')
  const [schedSettings, setSchedSettings] = useState<ProvisionSchedulerSettings>({ package_schedules: 'offer' })
  const [cleanup, setCleanup] = useState(true)
  const [compactAfter, setCompactAfter] = useState(RETENTION_DEFAULT.compact_after ?? '')
  const [deleteAfter, setDeleteAfter] = useState(RETENTION_DEFAULT.delete_after ?? '')
  const retention = useMemo(
    () => (cleanup ? { compact_after: compactAfter, delete_after: deleteAfter } : null),
    [cleanup, compactAfter, deleteAfter],
  )
  const retentionError = retention ? retentionProblem(retention) : ''
  const [checking, setChecking] = useState(false)
  const [rows, setRows] = useState<ProvisionPreflightRow[] | null>(null)
  const [op, setOp] = useState<string | null>(null)
  const [progress, setProgress] = useState<Record<string, HostProgress>>({})
  const [results, setResults] = useState<ProvisionResult[] | null>(null)
  const opRef = useRef<string | null>(null)

  const refHost = selected[0] ?? ''
  const running = op !== null && results === null

  // Reset on the closed→open transition only (see CopyKeyAllModal).
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setRows(null); setOp(null); opRef.current = null; setProgress({}); setResults(null)
      setPythons([]); setPyPath('')
      bridge.provision_options().then(o => {
        setOpts(o)
        const connected = new Set(o.hosts.filter(h => h.connected).map(h => h.name))
        setSelected((initialHosts ?? []).filter(h => connected.has(h)))
      }).catch(e => message.error(String(e)))
    }
    wasOpen.current = open
  }, [open, initialHosts])

  const loadPythons = useCallback(async (extra?: string) => {
    if (!refHost) return
    setPyLoading(true)
    try {
      const res = await bridge.provision_interpreters({ host: refHost, extra_paths: extra ? [extra] : [] })
      if (!res.ok) { message.error(res.error || 'Could not list interpreters'); return }
      setPythons(res.pythons)
      setPyPath(prev => {
        if (extra) return res.pythons.find(p => p.path === extra || p.real === extra)?.path ?? prev
        if (prev && res.pythons.some(p => p.path === prev)) return prev
        return res.pythons.find(p => p.runspec_ok)?.path ?? ''
      })
      if (extra && !res.pythons.some(p => p.path === extra || p.real === extra)) {
        message.warning(`${extra} didn't run as a Python interpreter on ${refHost}.`)
      }
    } catch (e) {
      message.error(String(e))
    } finally {
      setPyLoading(false)
    }
  }, [refHost])

  // The interpreter list follows the reference host (the first one ticked).
  useEffect(() => { if (open && refHost) loadPythons() }, [open, refHost, loadPythons])

  // Live progress for the running op.
  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as ProgressEvent
      if (!d || d.op !== opRef.current) return
      setProgress(prev => applyProgress(prev, d))
    }
    const onDoneEvt = (e: Event) => {
      const d = (e as CustomEvent).detail as { op: string; kind: string; results: ProvisionResult[] }
      if (!d || d.op !== opRef.current) return
      setResults(d.results)
      const ok = d.results.filter(r => r.ok).length
      if (ok === d.results.length) message.success(`Provisioned on ${ok} host${ok !== 1 ? 's' : ''}`)
      else message.warning(`Provisioned on ${ok} of ${d.results.length} hosts`)
      onDone?.()
    }
    window.addEventListener('runspec:provision_progress', onProgress)
    window.addEventListener('runspec:provision_done', onDoneEvt)
    return () => {
      window.removeEventListener('runspec:provision_progress', onProgress)
      window.removeEventListener('runspec:provision_done', onDoneEvt)
    }
  }, [onDone])

  const payload = useMemo(
    () => buildPayload({ hosts: selected, parent, name, pyMode, pyPath, pyVersion, runAs, become, packages, repo, scheduler: rootOwner ? '' : scheduler, retention, schedulerSettings: schedSettings }),
    [selected, parent, name, pyMode, pyPath, pyVersion, runAs, rootOwner, become, packages, repo, scheduler, retention, schedSettings],
  )
  // Any change to the request invalidates an earlier preflight.
  useEffect(() => { if (!op) setRows(null) }, [payload, op])

  const formReady = payload.hosts.length > 0 && !!payload.dest && !!payload.python && !retentionError
  const readyHosts = (rows ?? []).filter(r => r.ok).map(r => r.host)

  const check = async () => {
    if (!formReady) return
    setChecking(true)
    try {
      const res = await bridge.provision_preflight(payload)
      setRows(res.rows)
    } catch (e) {
      message.error(String(e))
    } finally {
      setChecking(false)
    }
  }

  const start = async () => {
    if (readyHosts.length === 0) return
    try {
      setProgress({}); setResults(null)
      const res = await bridge.provision_start({ ...payload, hosts: readyHosts })
      opRef.current = res.op
      setOp(res.op)
    } catch (e) {
      message.error(String(e))
    }
  }

  const retryFailed = () => {
    const failed = (results ?? []).filter(r => !r.ok).map(r => r.host)
    setSelected(failed)
    setOp(null); opRef.current = null; setResults(null); setProgress({}); setRows(null)
  }

  const hosts = opts?.hosts ?? []
  const options = hosts.map(h => ({
    value: h.name,
    disabled: !h.connected,
    label: (
      <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
        {h.name}
        {!h.connected && <Text type="secondary" style={{ fontSize: 11 }}> (not connected)</Text>}
      </span>
    ),
  }))
  const addMembers = (kind: 'group' | 'profile', n: string) =>
    setSelected(prev => [...new Set([...prev, ...membersOf(hosts, kind, n)])])

  const pyOptions = pythons.map(p => ({
    value: p.path,
    label: (
      <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
        {p.path} <Text type="secondary" style={{ fontSize: 11 }}>{p.version}{p.real !== p.path ? ` → ${p.real}` : ''}</Text>
        {!p.runspec_ok && <StatusTag tone="attention" style={{ marginLeft: 6 }}>too old for runspec</StatusTag>}
      </span>
    ),
  }))
  const pickedPy = pythons.find(p => p.path === pyPath)

  const label = (s: string) => <Text style={{ fontSize: 12, color: textSecondary(isDark) }}>{s}</Text>
  const box = { border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: '8px 10px' }

  const footer = (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
      {running && <Button onClick={() => op && bridge.provision_cancel(op)}>Cancel</Button>}
      {results && results.some(r => !r.ok) && <Button icon={<ReloadOutlined />} onClick={retryFailed}>Retry failed</Button>}
      <Button onClick={onClose}>Close</Button>
      {!op && <Button onClick={check} loading={checking} disabled={!formReady}>Check hosts</Button>}
      {!op && (
        <Button type="primary" onClick={start} disabled={readyHosts.length === 0}>
          Provision {readyHosts.length || ''} host{readyHosts.length !== 1 ? 's' : ''}
        </Button>
      )}
    </div>
  )

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={680}
      zIndex={zIndex}
      maskClosable={false}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><CloudServerOutlined /> Provision a runspec venv</span>}
      footer={footer}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, opacity: op ? 0.6 : 1, pointerEvents: op ? 'none' : 'auto' }}>
        <Text style={{ fontSize: 13 }}>
          Creates a Python venv on each selected host, installs runspec and your packages into it, sets up the logs inbox, and adds the new path to the host.
        </Text>

        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
            {label('Hosts')}
            {(opts?.groups ?? []).map(g => (
              <Tag key={`g:${g}`} style={{ cursor: 'pointer', margin: 0 }} {...pressable(() => addMembers('group', g))}>+ Group: {g}</Tag>
            ))}
            {(opts?.profiles ?? []).map(p => (
              <Tag key={`p:${p}`} style={{ cursor: 'pointer', margin: 0 }} {...pressable(() => addMembers('profile', p))}>+ Profile: {p}</Tag>
            ))}
            {selected.length > 0 && <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSelected([])}>Clear</Button>}
          </div>
          {hosts.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 13 }}>No remote hosts configured.</Text>
          ) : (
            <div style={{ ...box, maxHeight: 140, overflowY: 'auto' }}>
              <Checkbox.Group value={selected} onChange={v => setSelected(v as string[])} options={options} style={{ display: 'flex', flexDirection: 'column', gap: 2 }} />
            </div>
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
          <div>
            {label('Parent folder')}
            <PathInput host={refHost || 'host'} value={parent} onChange={setParent} dirsOnly browseDisabled={!refHost} placeholder="/opt/venvs" ariaLabel="Parent folder" />
          </div>
          <div>
            {label('Venv name')}
            <Input value={name} onChange={e => setName(e.target.value)} placeholder="ops" style={{ fontFamily: MONO_FONT }} aria-label="Venv name" />
          </div>
        </div>
        {payload.dest && <Text style={{ fontSize: 12, color: textMuted(isDark), marginTop: -6 }}>Venv: <Text code>{payload.dest}</Text></Text>}

        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            {label('Python')}
            <Segmented
              size="small"
              value={pyMode}
              onChange={v => setPyMode(v as PyMode)}
              options={[{ label: 'Pick an interpreter', value: 'path' }, { label: 'Same version on each host', value: 'version' }]}
            />
          </div>
          {pyMode === 'path' ? (
            <Space.Compact style={{ width: '100%' }}>
              <Select
                aria-label="Python interpreter"
                style={{ flex: 1 }}
                value={pyPath || undefined}
                onChange={setPyPath}
                options={pyOptions}
                loading={pyLoading}
                disabled={!refHost}
                placeholder={refHost ? `Interpreters on ${refHost}` : 'Tick a host first'}
                notFoundContent={pyLoading ? <LoadingOutlined /> : 'None found; browse for one'}
              />
              <div style={{ width: 220 }}>
                <PathInput host={refHost || 'host'} value={browsePy} onChange={setBrowsePy} browseDisabled={!refHost} placeholder="or browse to one…" ariaLabel="Python interpreter path" />
              </div>
              <Tooltip title="Check this interpreter on the host and pick it">
                <Button disabled={!browsePy.trim().startsWith('/')} onClick={() => { loadPythons(browsePy.trim()); setBrowsePy('') }}>Use</Button>
              </Tooltip>
            </Space.Compact>
          ) : (
            <Input value={pyVersion} onChange={e => setPyVersion(e.target.value)} placeholder="3.12" style={{ width: 120, fontFamily: MONO_FONT }} aria-label="Python version" />
          )}
          <Text style={{ fontSize: 11, color: textMuted(isDark) }}>
            {pyMode === 'path'
              ? `Uses this path on every host. runspec needs Python ${opts?.min_python ?? '3.10'} or newer.`
              : 'Each host uses its own interpreter of this version (e.g. /usr/bin/python3.12 on one, /opt/python312/bin/python3.12 on another).'}
          </Text>
          {pyMode === 'path' && pickedPy && !pickedPy.runspec_ok && (
            <Alert type="warning" showIcon style={{ marginTop: 6 }} message={`Python ${pickedPy.version} is too old to run runspec; pick ${opts?.min_python ?? '3.10'} or newer.`} />
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8 }}>
          <div>
            {label('Run as (owner of the venv)')}
            <Input value={runAs} onChange={e => setRunAs(e.target.value)} placeholder="the SSH user, or a service account" style={{ fontFamily: MONO_FONT }} aria-label="Run as" />
          </div>
          <div>
            {label('Become via')}
            <Select aria-label="Become via" value={become} onChange={setBecome} options={[{ value: 'sudo', label: 'sudo' }, { value: 'su', label: 'su' }]} style={{ width: '100%' }} disabled={!runAs.trim()} />
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
          <div>
            {label('Packages (runspec is always installed)')}
            <Select aria-label="Packages" mode="tags" value={packages} onChange={setPackages} placeholder="runspec-linux, runspec-fs…" style={{ width: '100%' }} tokenSeparators={[',', ' ']} open={false} suffixIcon={null} />
          </div>
          <div>
            {label('Package index')}
            <Select
              aria-label="Package index"
              value={repo}
              onChange={setRepo}
              style={{ width: '100%' }}
              options={[{ value: '', label: "Host's own pip config" }, ...(opts?.repos ?? []).map(r => ({ value: r.id, label: r.name }))]}
            />
          </div>
        </div>

        <div>
          {label('Shared schedules')}
          <Select value={rootOwner ? '' : scheduler} onChange={setScheduler} options={SCHEDULER_OPTIONS} disabled={rootOwner} style={{ width: '100%' }} aria-label="Shared schedules" />
          <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
            {rootOwner
              ? ROOT_SCHEDULES_NOTE
              : scheduler
              ? `Installs runspec-scheduler, fires this venv's schedules from its daemon (run as the owner) and sets up what restarts it.${scheduler === 'auto' ? ' Per host: systemd --user when lingering is on, else supervisord in the venv.' : ''}`
              : 'Schedules in this venv can still run from the owner\'s crontab (runspec-scheduler sync).'}
          </Text>
          {scheduler && !rootOwner && (
            <div style={{ marginTop: 8 }}>
              <SchedulerSettingsFields value={schedSettings} onChange={setSchedSettings} />
            </div>
          )}
        </div>

        <div>
          {label('Log cleanup')}
          <LogCleanupField
            checked={cleanup}
            onChecked={setCleanup}
            compactAfter={compactAfter}
            onCompactAfter={setCompactAfter}
            deleteAfter={deleteAfter}
            onDeleteAfter={setDeleteAfter}
            error={retentionError}
            hint={cleanup
              ? 'The venv owner\'s runs apply it once a day: old run logs are compressed, then deleted (runspec logs retention, runspec 0.80.0+). Leave a box blank or type off to skip that step. The audit log is never touched.'
              : 'Run logs from other accounts pile up in logs/ until someone prunes them.'}
          />
        </div>
      </div>

      {rows && !op && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {label(`Preflight: ${readyHosts.length} of ${rows.length} ready (nothing has been changed yet)`)}
          {rows.map(r => (
            <div key={r.host} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
              <StatusIcon status={r.ok ? 'ok' : 'failed'} style={{ marginTop: 3 }} />
              <span style={{ fontFamily: MONO_FONT, minWidth: 110 }}>{r.host}</span>
              <span style={{ flex: 1, minWidth: 0 }}>
                {r.python && <Text style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{r.python} {r.version && `(${r.version})`}</Text>}
                {r.user && <Text type="secondary" style={{ fontSize: 11 }}> as {r.user}</Text>}
                {r.blockers.map(b => <div key={b}><Text type="danger" style={{ fontSize: 12 }}>{b}</Text></div>)}
                {r.warnings.map(w => <div key={w}><Text type="warning" style={{ fontSize: 12 }}><WarningFilled /> {w}</Text></div>)}
              </span>
            </div>
          ))}
        </div>
      )}

      {op && <ProgressPanel progress={progress} />}
    </Modal>
  )
}
