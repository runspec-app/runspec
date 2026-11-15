import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  Table, Typography, Button, Tooltip, message, Modal, Drawer, Select,
  Input, InputNumber, Space, Form, Spin, Alert,
} from 'antd'
import {
  ReloadOutlined, DeleteOutlined, EyeOutlined, FolderOutlined,
  BarChartOutlined, ShareAltOutlined, DatabaseOutlined, ClearOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge,
  type LogStatusVenv, type LogStatusRunnable, type LogViewResult,
} from '../bridge'
import { LogStream } from '../components/LogStream'
import { MONO_FONT } from '../ThemeContext'
import { EmptyState, IconAction, PageToolbar, RowActions, StatusTag, type RowAction } from '../components/kit'
import { RunsIndexModal, type RunsIndexTarget } from './RunsIndexModal'
import { LogCleanupModal, type LogCleanupTarget } from './LogCleanupModal'
import { When, plural } from '../when'
import { useRiskConfirm } from '../components/risk'

// Analytics is a per-host drill off the Logs tab (not a standalone tab): the
// operator opens a host's disk usage here, then its run analytics in a drawer,
// which reuses the SSH connection the Logs read just warmed. Lazy so the heavy
// @ant-design/plots chunk only loads when the drawer is first opened.
const AnalyticsView = lazy(() =>
  import('./AnalyticsView').then(m => ({ default: m.AnalyticsView })),
)

const { Text } = Typography

// The backend surfaces the per-user home fallback dir (~/logs) as its own
// inventory entry under this sentinel group id — never folded into a venv, so a
// per-venv prune can't touch it. It's personal to the login user on this host
// (used when a venv's own logs dir isn't writable); the venv dirs are the ones a
// shared venv's users all write to. Badged "home" in the UI, not "shared".
const HOME_GROUP = '~/logs'

function humanSize(n: number | undefined): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = n
  let u = 0
  while (size >= 1024 && u < units.length - 1) { size /= 1024; u++ }
  return u === 0 ? `${size} B` : `${size.toFixed(1)} ${units[u]}`
}



// ── clean up now (one-off compress + delete) ─────────────────────────────────
// One window for the one-off jobs that used to be two (Compact, Prune), in the
// same words as the venv's log cleanup policy (LogCleanupModal): compress after,
// delete after, keep at most. Nothing here is saved; the policy is.

interface MaintTarget {
  host: string
  group: string
  runnable?: string   // undefined = whole venv
  /** The venv's runnables, for the picker (one place to clean up one runnable
   *  or all of them; the rows no longer carry their own trash icon). */
  runnables?: string[]
}

interface MaintModalProps {
  target: MaintTarget | null
  onClose: () => void
  // Passes the host that was actually acted on (frozen at modal open), so the
  // parent refreshes that host's inventory — not whatever host the sidebar
  // happens to show now (which could differ if it was switched mid-modal).
  onApplied: (actedHost: string) => void
  /** Open the venv's log cleanup policy instead (the standing version of this). */
  onOpenPolicy?: (t: { host: string; group: string }) => void
}

/** What a one-off cleanup would do, from its fields. Pure, for tests. */
export function cleanupPlan(f: { compressAfter: string; deleteAfter: string; keepFiles: number | null; keepSize: string }) {
  const compress = f.compressAfter.trim()
  const del = f.deleteAfter.trim() || f.keepFiles != null || f.keepSize.trim()
  return { compress: !!compress, delete: !!del, any: !!compress || !!del }
}

export function CleanupNowModal({ target, onClose, onApplied, onOpenPolicy }: MaintModalProps) {
  const [compressAfter, setCompressAfter] = useState('')
  const [pickRunnable, setPickRunnable] = useState('')
  const [deleteAfter, setDeleteAfter] = useState('')
  const [keepFiles, setKeepFiles] = useState<number | null>(null)
  const [keepSize, setKeepSize] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  // Surfaced *inside* the modal (not just a toast) so a failure — most often an
  // older remote runspec that can't scope the job — is visible next to the
  // button, instead of the modal appearing to "do nothing".
  const [error, setError] = useState<string | null>(null)
  // Run it as the venv's owner via sudo: a venv owned by a service account
  // can't be cleaned by the login user. Pre-filled with the owner (editable);
  // blank = yourself. Not offered for ~/logs (your own folder).
  const [runAs, setRunAs] = useState('')

  useEffect(() => {
    setCompressAfter(''); setDeleteAfter(''); setKeepFiles(null); setKeepSize('')
    setPickRunnable(target?.runnable ?? '')
    setPreview(null); setError(null); setBusy(false); setRunAs('')
    if (!target || target.group === '~/logs') return
    let cancelled = false
    // Promise.resolve tolerates a mock/proxy returning a non-promise.
    Promise.resolve(bridge.venv_owner(target.host, target.group))
      .then(r => { if (!cancelled && r?.ok && r.owner && r.owner !== r.self) setRunAs(r.owner) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [target])

  // Hooks stay above the early return (a hook after it crashes on open).
  const [askRisk, riskHolder] = useRiskConfirm()

  if (!target) return null
  const { host, group } = target
  const runnable = pickRunnable || undefined
  const isHome = group === '~/logs'
  const scopeLabel = runnable ? `${group}/${runnable}` : `${group} (all runnables)`
  const plan = cleanupPlan({ compressAfter, deleteAfter, keepFiles, keepSize })
  const as = runAs.trim() || undefined

  // Both passes, dry or real: compress first (what's compressed is still
  // there), then delete. Returns the lines describing what happened.
  const pass = async (dryRun: boolean): Promise<{ lines: string[]; compressed: number; deleted: number } | null> => {
    const lines: string[] = []
    let compressed = 0, deleted = 0
    if (plan.compress) {
      const r = await bridge.logs_compact(host, group, runnable, compressAfter.trim(), true, dryRun, as)
      if (!r.ok) { setError(r.error || 'Compressing failed.'); return null }
      compressed = r.compacted ?? 0
      if (compressed) lines.push(`${dryRun ? 'Compress' : 'Compressed'} ${plural(compressed, 'run file')} into ${plural(r.archives?.length ?? 0, 'archive')}`)
    }
    if (plan.delete) {
      const r = await bridge.logs_prune(host, group, runnable, deleteAfter.trim() || undefined, keepFiles ?? undefined, keepSize.trim() || undefined, dryRun, as)
      if (!r.ok) { setError(r.error || 'Deleting failed.'); return null }
      deleted = r.count ?? 0
      if (deleted) lines.push(`${dryRun ? 'Delete' : 'Deleted'} ${plural(deleted, 'file')} (${humanSize(r.freed_bytes)} freed)`)
    }
    return { lines, compressed, deleted }
  }

  const doPreview = async () => {
    setBusy(true); setError(null); setPreview(null)
    try {
      const r = await pass(true)
      if (r) setPreview(r.lines.length ? `Right now it would: ${r.lines.join('; ').toLowerCase()}.` : 'Nothing matches, so there is nothing to do.')
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  // Apply previews first and shows exactly what goes. Deleting can't be undone
  // (destructive: type the name); compressing only keeps the data in archives
  // (fleet).
  const apply = async () => {
    setBusy(true); setError(null); setPreview(null)
    let dry: Awaited<ReturnType<typeof pass>>
    try { dry = await pass(true) } catch (e) { setError(String(e)); setBusy(false); return }
    setBusy(false)
    if (!dry) return
    if (!dry.lines.length) { setPreview('Nothing matches, so there is nothing to do.'); return }
    const deleting = dry.deleted > 0
    const a = await askRisk({
      level: deleting ? 'destructive' : 'fleet',
      title: `Clean up the logs of ${scopeLabel} now?`,
      action: dry.lines.join('\n'),
      targets: [`${host} / ${group}`],
      consequence: deleting ? 'Deleted run files are gone from History.' : 'The files stay readable inside the archives.',
      typeToConfirm: deleting ? (runnable || group) : undefined,
      okText: deleting ? 'Delete' : 'Compress',
    })
    if (!a.ok) return
    setBusy(true)
    try {
      const r = await pass(false)
      if (r) { message.success(r.lines.join('; ') || 'Done'); onApplied(host); onClose() }
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  const row = (label: string, help: string, input: ReactNode) => (
    <Form.Item label={label} help={help} style={{ marginBottom: 14 }}>{input}</Form.Item>
  )

  return (
    <Modal
      open
      title={<span>Clean up now · <span style={{ fontFamily: MONO_FONT }}>{host} / {scopeLabel}</span></span>}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose} disabled={busy}>Cancel</Button>,
        <Button key="preview" onClick={() => { void doPreview() }} loading={busy} disabled={!plan.any}>Preview</Button>,
        <Tooltip key="apply" title={plan.any ? '' : 'Fill in at least one field first'}>
          <Button type="primary" disabled={!plan.any || busy} onClick={() => { void apply() }}>Apply…</Button>
        </Tooltip>,
      ]}
    >
      {riskHolder}
      <Text type="secondary" style={{ fontSize: 13 }}>
        A one-off pass; nothing here is saved.
        {!isHome && onOpenPolicy && (
          <> To do this every day, set the venv's <Button type="link" size="small" style={{ padding: 0, height: 'auto', textDecoration: 'underline' }}
            onClick={() => { onClose(); onOpenPolicy({ host, group }) }}>log cleanup policy</Button>.</>
        )}
      </Text>
      <Form layout="vertical" style={{ marginTop: 16 }}>
        {(target.runnables?.length ?? 0) > 0 && row('Runnable', 'Which runs to clean up: every runnable in the venv, or one.', (
          <Select aria-label="Runnable" value={pickRunnable} onChange={setPickRunnable} style={{ maxWidth: 280 }}
            options={[{ value: '', label: 'All runnables' }, ...(target.runnables ?? []).map(n => ({ value: n, label: n }))]} />
        ))}
        {row('Compress after', 'Run files older than this go into a gzip archive, one per day. e.g. 7d', (
          <Input aria-label="Compress after" value={compressAfter} onChange={e => setCompressAfter(e.target.value)} placeholder="7d" style={{ maxWidth: 160, fontFamily: MONO_FONT }} />
        ))}
        {row('Delete after', 'Run files and archives older than this are deleted. e.g. 90d', (
          <Input aria-label="Delete after" value={deleteAfter} onChange={e => setDeleteAfter(e.target.value)} placeholder="90d" style={{ maxWidth: 160, fontFamily: MONO_FONT }} />
        ))}
        {row('Keep at most', 'Then the oldest are deleted until it fits: a number of files per runnable, a size, or both.', (
          <Space>
            <InputNumber aria-label="Keep at most files" value={keepFiles} onChange={v => setKeepFiles(v)} min={0} placeholder="50" style={{ width: 110 }} addonAfter="files" />
            <Input aria-label="Keep at most size" value={keepSize} onChange={e => setKeepSize(e.target.value)} placeholder="5GB" style={{ width: 110, fontFamily: MONO_FONT }} />
          </Space>
        ))}
        {!isHome && row('Run as', "Pre-filled with the venv's owner, who can change its logs. Blank = yourself.", (
          <Input aria-label="Run as" value={runAs} onChange={e => setRunAs(e.target.value)} placeholder="(yourself)" style={{ maxWidth: 200, fontFamily: MONO_FONT }} />
        ))}
      </Form>
      {preview && <Alert type="info" showIcon style={{ marginTop: 8 }} message={preview} />}
      {error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={error} />}
    </Modal>
  )
}

// ── make-logs-shared modal (runs `runspec logs share`) ────

interface ShareTarget { host: string; venv: string }

function ShareLogsModal({ target, onClose, onApplied }: {
  target: ShareTarget | null
  onClose: () => void
  onApplied: (actedHost: string) => void
}) {
  const [group, setGroup] = useState('')
  const [runAs, setRunAs] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set when the share applied but the console's own user still can't write the
  // dir (it isn't in the group) — shown so the "why do logs still go to ~/logs?"
  // gap is visible at share time rather than discovered later.
  const [warning, setWarning] = useState<string | null>(null)

  // Pre-fill "Run as" with the venv owner when it isn't the login user, so a venv
  // owned by a service account is shared by escalating to that owner via sudo.
  useEffect(() => {
    setGroup(''); setRunAs(''); setError(null); setWarning(null); setBusy(false)
    if (!target) return
    let cancelled = false
    Promise.resolve(bridge.venv_owner(target.host, target.venv))
      .then(r => { if (!cancelled && r?.ok && r.owner && r.owner !== r.self) setRunAs(r.owner) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [target])

  if (!target) return null
  const { host, venv } = target

  const apply = async () => {
    setBusy(true)
    setError(null)
    setWarning(null)
    try {
      const r = await bridge.share_venv_logs(host, venv, group.trim() || undefined, runAs.trim() || undefined)
      if (!r.ok) { setError(r.error || 'Could not make logs shared.'); return }
      const via = r.as_user ? ` (as ${r.as_user})` : ''
      message.success(r.inbox ? `${venv}: logs inbox set up${via}` : `${venv}: logs dir made shared${via}`)
      onApplied(host)  // the share applied — refresh the inventory
      if (r.warning) {
        // Applied, but the console's user can't write it yet — keep the modal open
        // with the warning so the operator acts on it (add the user to the group).
        setWarning(r.warning)
      } else {
        onClose()
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title="Make logs shared"
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose} disabled={busy}>Cancel</Button>,
        <Button key="apply" type="primary" loading={busy} onClick={apply}>Make shared</Button>,
      ]}
    >
      <Text type="secondary" style={{ fontSize: 13 }}>
        Sets up <Text code>{venv}</Text>'s <b>logs inbox</b> on <Text code>{host}</Text>{' '}
        (<Text code>logs/inbox</Text>, sticky <Text code>01777</Text>, owned by the venv's owner):
        every other account — operators, root, other crontabs — drops its runs there and they
        reach this venv's History; the owner's runs (or <b>Sweep inbox</b>) index them. No group,
        ACL or extra sudo rule: it runs as the venv's owner, via sudo as the owner or else root
        (the standard scoped grant). Needs runspec &gt;= 0.66 in the venv.
      </Text>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 8 }}>
        Or give a <b>group</b> for the older group share instead: <Text code>logs/</Text>{' '}
        <Text code>2775</Text> (setgid, group-writable) + the runs index repaired, run as{' '}
        <Text code>Run as</Text>.
      </Text>
      <Form layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item
          label="Group (optional — the alternative to the inbox)"
          help="Blank = set up the logs inbox (recommended). A group, e.g. consoles, applies the 2775 group share instead."
          style={{ marginBottom: 16 }}
        >
          <Input value={group} onChange={e => setGroup(e.target.value)} placeholder="consoles" style={{ maxWidth: 200, fontFamily: MONO_FONT }} />
        </Form.Item>
        <Form.Item
          label="Run as"
          help="Escalate to this OS user via sudo (pre-filled with the venv's owner) — for a venv you don't own. Blank runs as you."
          style={{ marginBottom: 8 }}
        >
          <Input value={runAs} onChange={e => setRunAs(e.target.value)} placeholder="(yourself)" style={{ maxWidth: 200, fontFamily: MONO_FONT }} />
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={error} />}
      {warning && <Alert type="warning" showIcon style={{ marginTop: 8 }} message="Shared, with a caveat" description={warning} />}
    </Modal>
  )
}

// ── merged-stream viewer ──────────────────────────────────────────────────────

interface ViewTarget { host: string; group: string; runnable: string }

function StreamDrawer({ target, onClose }: { target: ViewTarget | null; onClose: () => void }) {
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<LogViewResult | null>(null)

  useEffect(() => {
    if (!target) { setResult(null); return }
    setLoading(true)
    bridge.logs_view(target.host, target.group, target.runnable)
      .then(setResult)
      .catch(e => message.error(String(e)))
      .finally(() => setLoading(false))
  }, [target])

  return (
    <Drawer
      open={!!target}
      onClose={onClose}
      // Audit log lines run very long (JSON records, tracebacks); give the reader
      // most of the window rather than a fixed narrow panel. Capped so it never
      // exceeds the viewport on a small screen.
      width={Math.min(1400, Math.max(680, window.innerWidth - 120))}
      title={target ? <span style={{ fontFamily: MONO_FONT }}>{target.group}/{target.runnable} — merged stream</span> : ''}
    >
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : !result?.ok ? (
        <Alert type="error" showIcon message={result?.error || 'Failed to read logs'} />
      ) : result.records.length === 0 ? (
        <EmptyState message="No log records for this runnable yet." />
      ) : (
        <LogStream records={result.records} />
      )}
    </Drawer>
  )
}

// A venv's actions: the cleanup policy is what the operator comes here for, the
// runs index sits beside it, and the one-off jobs (make shared, compact, prune)
// wait behind More, so Prune is never one stray click from the main action.
function VenvActions({ group, home, onShare, onIndex, onCleanup, onCleanNow }: {
  group: string
  home: boolean
  onShare: () => void; onIndex: () => void; onCleanup: () => void; onCleanNow: () => void
}) {
  const cleanup: RowAction = {
    key: 'cleanup', label: 'Log cleanup…', icon: <ClearOutlined />, onClick: onCleanup,
    tip: "This venv's log cleanup policy: compress and delete old runs every day (runspec 0.80.0+). View, change, preview or apply it.",
  }
  const index: RowAction = {
    key: 'index', label: 'Runs index…', icon: <DatabaseOutlined />, onClick: onIndex,
    tip: "The list of runs History reads: inspect it, remove old rows, mark lost runs crashed or rebuild it",
  }
  const more: RowAction[] = []
  if (!home) more.push({ key: 'share', label: 'Make shared…', icon: <ShareAltOutlined />, onClick: onShare })
  more.push(
    { key: 'clean', label: 'Clean up now…', icon: <DeleteOutlined />, danger: true, onClick: onCleanNow },
  )
  return home
    ? <RowActions primary={index} more={more} testId={`venv-actions-${group}`} />
    : <RowActions primary={cleanup} secondary={[index]} more={more} testId={`venv-actions-${group}`} />
}

// ── main view ─────────────────────────────────────────────────────────────────

interface LogsViewProps {
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
  active: boolean
  // Opens the Analytics tab; without it the dashboard opens in a drawer here.
  onOpenAnalytics?: () => void
}

export function LogsView({ selectedHost, activeScope, active, onOpenAnalytics }: LogsViewProps) {
  const [venvs, setVenvs] = useState<LogStatusVenv[]>([])
  const [loading, setLoading] = useState(false)
  const [maintTarget, setMaintTarget] = useState<MaintTarget | null>(null)
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)
  const [indexTarget, setIndexTarget] = useState<RunsIndexTarget | null>(null)
  const [cleanupTarget, setCleanupTarget] = useState<LogCleanupTarget | null>(null)
  const [viewTarget, setViewTarget] = useState<ViewTarget | null>(null)
  const [analyticsOpen, setAnalyticsOpen] = useState(false)

  const host = selectedHost || 'local'

  const refresh = useCallback(() => {
    setLoading(true)
    bridge.logs_status(host)
      .then(setVenvs)
      .catch(e => message.error(String(e)))
      .finally(() => setLoading(false))
  }, [host])

  // Switching host drops the old host's venvs so a switch never briefly shows
  // another host's inventory (and the spinner shows while the new one loads).
  // Also close the per-host Analytics drawer so it can't show the old host's
  // charts under the new host.
  useEffect(() => { setVenvs([]); setAnalyticsOpen(false) }, [host])

  // The view stays mounted across tab switches (App toggles display), so venvs
  // persist — returning shows the inventory instantly. The logs_status SSH call
  // only runs while visible (and on host change), not eagerly at startup or in
  // a hidden tab. The spinner already shows only when venvs is empty.
  useEffect(() => { if (active) refresh() }, [refresh, active])

  const shown = activeScope.length > 0
    ? venvs.filter(v => activeScope.includes(v.group))
    : venvs

  const runnableColumns = (v: LogStatusVenv): ColumnType<LogStatusRunnable>[] => [
    {
      title: 'Runnable',
      dataIndex: 'runnable',
      key: 'runnable',
      render: (name: string) => <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{name}</span>,
      sorter: (a, b) => a.runnable.localeCompare(b.runnable),
    },
    {
      title: 'Runs',
      dataIndex: 'per_run_files',
      key: 'per_run_files',
      align: 'right',
      sorter: (a, b) => a.per_run_files - b.per_run_files,
    },
    {
      title: 'Archives',
      dataIndex: 'archives',
      key: 'archives',
      align: 'right',
      render: (n: number) => n > 0 ? n : <Text type="secondary">0</Text>,
    },
    {
      title: 'Size',
      dataIndex: 'total_bytes',
      key: 'total_bytes',
      align: 'right',
      render: (n: number) => humanSize(n),
      sorter: (a, b) => a.total_bytes - b.total_bytes,
      defaultSortOrder: 'descend',
    },
    {
      title: 'Newest',
      dataIndex: 'newest',
      key: 'newest',
      render: (ts: string | null) => <When t={ts} />,
    },
    {
      title: '',
      key: 'actions',
      align: 'right',
      render: (_: unknown, r: LogStatusRunnable) => (
        <div style={{ display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
          <IconAction label={`View ${r.runnable} log`} tip="View the merged log" icon={<EyeOutlined />}
            onClick={() => setViewTarget({ host, group: v.group, runnable: r.runnable })} />

        </div>
      ),
    },
  ]

  return (
    <div>
      <PageToolbar
        actions={<>
          <Tooltip title="Run analytics for this host — charts, success rate, durations and top runnables, scanned over the connection this tab just warmed">
            <Button size="small" icon={<BarChartOutlined />} onClick={() => (onOpenAnalytics ? onOpenAnalytics() : setAnalyticsOpen(true))}>Charts</Button>
          </Tooltip>
          <IconAction label="Refresh" tip="Read this host's logs again" icon={<ReloadOutlined spin={loading} />} onClick={refresh} />
        </>}
      >
        <Text style={{ color: 'var(--rs-text-secondary)' }}>Host <span style={{ fontFamily: MONO_FONT, fontWeight: 600 }}>{host}</span></Text>
      </PageToolbar>

      {loading && venvs.length === 0 ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : shown.length === 0 ? (
        <EmptyState
          message={activeScope.length > 0 ? 'No run logs in the venvs in scope on this host.' : 'No run logs on this host yet.'}
          action={{ label: 'Refresh', icon: <ReloadOutlined />, onClick: refresh }}
        />
      ) : (
        shown.map(v => (
          <div key={v.group} style={{ marginBottom: 28 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <FolderOutlined style={{ color: 'var(--rs-text-muted)' }} />
              <Text strong style={{ fontFamily: MONO_FONT }}>{v.group}</Text>
              {v.group === HOME_GROUP && (
                <StatusTag tone="neutral" tip="Your home log dir (~/logs) — the per-user fallback runspec writes to when a venv's own logs dir isn't writable. Personal to your login on this host (the venv dirs above are the shared ones); pruning here affects only ~/logs.">home</StatusTag>
              )}
              {v.ok ? (
                <Text type="secondary" style={{ fontSize: 13 }}>
                  {plural(v.total_files ?? 0, 'file')}, {humanSize(v.total_bytes)}
                </Text>
              ) : (
                <StatusTag tone="failed">{v.error || 'unavailable'}</StatusTag>
              )}
              <div style={{ flex: 1 }} />
              {v.ok && <VenvActions
                group={v.group}
                home={v.group === HOME_GROUP}
                onShare={() => setShareTarget({ host, venv: v.group })}
                onIndex={() => setIndexTarget({ host, group: v.group })}
                onCleanup={() => setCleanupTarget({ host, group: v.group })}
                onCleanNow={() => setMaintTarget({ host, group: v.group, runnables: (v.runnables ?? []).map(r => r.runnable) })}
              />}
            </div>
            {v.ok && (
              <Table
                dataSource={v.runnables ?? []}
                columns={runnableColumns(v)}
                rowKey="runnable"
                size="small"
                pagination={false}
              />
            )}
            {v.ok && v.dirs && v.dirs.length > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>{v.dirs.join('  ·  ')}</Text>
            )}
          </div>
        ))
      )}

      <CleanupNowModal
        target={maintTarget}
        onClose={() => setMaintTarget(null)}
        onOpenPolicy={t => setCleanupTarget(t)}
        // Refresh only when the host that was acted on is still the one in view,
        // so a mid-modal host switch can't paint the acted host's inventory under
        // a different host's title.
        onApplied={(actedHost) => { if (actedHost === host) refresh() }}
      />
      <ShareLogsModal
        target={shareTarget}
        onClose={() => setShareTarget(null)}
        onApplied={(actedHost) => { if (actedHost === host) refresh() }}
      />
      <RunsIndexModal target={indexTarget} onClose={() => setIndexTarget(null)} />
      <LogCleanupModal target={cleanupTarget} onClose={() => setCleanupTarget(null)} />
      <StreamDrawer target={viewTarget} onClose={() => setViewTarget(null)} />

      <Drawer
        open={analyticsOpen}
        onClose={() => setAnalyticsOpen(false)}
        // Match the merged-stream drawer's wide, responsive panel — the dashboard
        // needs the room for its chart grid.
        width={Math.min(1500, Math.max(720, window.innerWidth - 80))}
        title={<span>Analytics — {host}</span>}
        // Unmount on close so the plots chunk isn't kept live in the background,
        // and a reopen re-scans fresh.
        destroyOnClose
      >
        <Suspense fallback={<div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>}>
          <AnalyticsView selectedHost={host} activeScope={activeScope} active={analyticsOpen} />
        </Suspense>
      </Drawer>
    </div>
  )
}
