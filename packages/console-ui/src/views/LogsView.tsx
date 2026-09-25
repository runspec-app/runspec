import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import {
  Table, Tag, Typography, Button, Tooltip, message, Modal, Drawer,
  Input, InputNumber, Switch, Form, Empty, Spin, Alert, Popconfirm,
} from 'antd'
import {
  ReloadOutlined, DeleteOutlined, InboxOutlined, EyeOutlined, FolderOutlined,
  BarChartOutlined, ShareAltOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge,
  type LogStatusVenv, type LogStatusRunnable, type LogViewResult,
} from '../bridge'
import { LogStream } from '../components/LogStream'
import { MONO_FONT } from '../ThemeContext'

// Analytics is a per-host drill off the Logs tab (not a standalone tab): the
// operator opens a host's disk usage here, then its run analytics in a drawer,
// which reuses the SSH connection the Logs read just warmed. Lazy so the heavy
// @ant-design/plots chunk only loads when the drawer is first opened.
const AnalyticsView = lazy(() =>
  import('./AnalyticsView').then(m => ({ default: m.AnalyticsView })),
)

const { Title, Text } = Typography

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

function relTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (secs < 90) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 90) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 36) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

// ── maintenance modal (prune / compact) ──────────────────────────────────────

type Verb = 'prune' | 'compact'

interface MaintTarget {
  host: string
  group: string
  runnable?: string   // undefined = whole venv
  verb: Verb
}

interface MaintModalProps {
  target: MaintTarget | null
  onClose: () => void
  // Passes the host that was actually acted on (frozen at modal open), so the
  // parent refreshes that host's inventory — not whatever host the sidebar
  // happens to show now (which could differ if it was switched mid-modal).
  onApplied: (actedHost: string) => void
}

function MaintenanceModal({ target, onClose, onApplied }: MaintModalProps) {
  const [olderThan, setOlderThan] = useState('')
  const [maxFiles, setMaxFiles] = useState<number | null>(null)
  const [maxTotalSize, setMaxTotalSize] = useState('')
  const [gzip, setGzip] = useState(true)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  // Surfaced *inside* the modal (not just a toast) so a failure — most often an
  // older remote runspec that can't scope the prune — is visible next to the
  // button, instead of the modal appearing to "do nothing".
  const [error, setError] = useState<string | null>(null)

  // Reset form whenever a fresh target opens the modal.
  useEffect(() => {
    setOlderThan(target?.verb === 'compact' ? '7d' : '')
    setMaxFiles(null)
    setMaxTotalSize('')
    setGzip(true)
    setPreview(null)
    setError(null)
    setBusy(false)
  }, [target])

  if (!target) return null
  const { host, group, runnable, verb } = target

  const scopeLabel = runnable ? `${group}/${runnable}` : `${group} (all runnables)`
  const hasPolicy = verb === 'compact'
    ? olderThan.trim().length > 0
    : (olderThan.trim().length > 0 || maxFiles != null || maxTotalSize.trim().length > 0)

  const run = async (dryRun: boolean) => {
    setBusy(true)
    setError(null)
    setPreview(null)
    try {
      if (verb === 'prune') {
        const r = await bridge.logs_prune(host, group, runnable, olderThan.trim() || undefined, maxFiles ?? undefined, maxTotalSize.trim() || undefined, dryRun)
        if (!r.ok) { setError(r.error || 'Prune failed.'); return }
        if (dryRun) {
          setPreview(`${r.count ?? 0} file(s) would be deleted — ${humanSize(r.freed_bytes)} freed.`)
        } else {
          message.success(`Deleted ${r.count ?? 0} file(s) — ${humanSize(r.freed_bytes)} freed`)
          onApplied(host); onClose()
        }
      } else {
        const r = await bridge.logs_compact(host, group, runnable, olderThan.trim() || undefined, gzip, dryRun)
        if (!r.ok) { setError(r.error || 'Compact failed.'); return }
        if (dryRun) {
          setPreview(`${r.compacted ?? 0} file(s) would roll into ${r.archives?.length ?? 0} archive(s).`)
        } else {
          message.success(`Compacted ${r.compacted ?? 0} file(s) into ${r.archives?.length ?? 0} archive(s)`)
          onApplied(host); onClose()
        }
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
      title={verb === 'prune' ? 'Prune logs' : 'Compact logs'}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose} disabled={busy}>Cancel</Button>,
        <Button key="preview" onClick={() => run(true)} loading={busy} disabled={!hasPolicy}>
          Preview (dry-run)
        </Button>,
        <Popconfirm
          key="apply"
          title={verb === 'prune' ? 'Delete matching log files?' : 'Roll matching files into archives?'}
          description="This cannot be undone."
          okText={verb === 'prune' ? 'Delete' : 'Compact'}
          okButtonProps={{ danger: verb === 'prune' }}
          onConfirm={() => run(false)}
          disabled={!hasPolicy || busy}
        >
          <Button type="primary" danger={verb === 'prune'} disabled={!hasPolicy || busy}>
            Apply
          </Button>
        </Popconfirm>,
      ]}
    >
      <Text type="secondary" style={{ fontSize: 13 }}>
        Target: <Text code>{scopeLabel}</Text> on <Text code>{host}</Text>
      </Text>
      <Form layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item
          label="Older than"
          help={verb === 'compact' ? 'Required — e.g. 7d, 24h, 2w' : 'e.g. 90d (optional if another policy is set)'}
          required={verb === 'compact'}
          style={{ marginBottom: 16 }}
        >
          <Input value={olderThan} onChange={e => setOlderThan(e.target.value)} placeholder="7d" style={{ maxWidth: 160 }} />
        </Form.Item>

        {verb === 'compact' ? (
          <Form.Item label="Gzip archive" style={{ marginBottom: 8 }}>
            <Switch checked={gzip} onChange={setGzip} />
          </Form.Item>
        ) : (
          <>
            <Form.Item label="Keep newest N files" help="Per runnable (optional)" style={{ marginBottom: 16 }}>
              <InputNumber value={maxFiles} onChange={v => setMaxFiles(v)} min={0} placeholder="50" style={{ width: 160 }} />
            </Form.Item>
            <Form.Item label="Max total size" help="Delete oldest over budget — e.g. 5GB (optional)" style={{ marginBottom: 8 }}>
              <Input value={maxTotalSize} onChange={e => setMaxTotalSize(e.target.value)} placeholder="5GB" style={{ maxWidth: 160 }} />
            </Form.Item>
          </>
        )}
      </Form>

      {preview && (
        <Alert type="info" showIcon style={{ marginTop: 8 }} message={preview} />
      )}
      {error && (
        <Alert type="error" showIcon style={{ marginTop: 8 }} message={error} />
      )}
      {!hasPolicy && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {verb === 'compact' ? 'Set an age threshold to continue.' : 'Set at least one policy to continue.'}
        </Text>
      )}
    </Modal>
  )
}

// ── make-logs-shared modal (surfaces the Phase 2 share-venv-logs runnable) ────

interface ShareTarget { host: string; venv: string }

function ShareLogsModal({ target, onClose, onApplied }: {
  target: ShareTarget | null
  onClose: () => void
  onApplied: (actedHost: string) => void
}) {
  const [group, setGroup] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { setGroup(''); setError(null); setBusy(false) }, [target])

  if (!target) return null
  const { host, venv } = target

  const apply = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await bridge.share_venv_logs(host, venv, group.trim() || undefined)
      if (!r.ok) { setError(r.error || 'Could not make logs shared.'); return }
      const via = r.as_user ? ` (as ${r.as_user})` : ''
      message.success(`${venv}: logs dir made shared${via}`)
      onApplied(host); onClose()
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
        Runs <Text code>share-venv-logs</Text> on <Text code>{host}</Text> to set{' '}
        <Text code>{venv}</Text>'s <Text code>logs/</Text> dir to <Text code>2775</Text>{' '}
        (setgid + group-writable), so the per-venv runs index and the audit logs are
        writable across a shared console group — the repair for a venv not built with{' '}
        <Text code>create-venv --group</Text>. Needs runspec-linux in that venv; escalates
        to the venv owner over SSH when the login user can't write it.
      </Text>
      <Form layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item
          label="Group (optional)"
          help="Unix group to chgrp the logs/ dir to, e.g. consoles. Leave blank to keep the current group."
          style={{ marginBottom: 8 }}
        >
          <Input value={group} onChange={e => setGroup(e.target.value)} placeholder="consoles" style={{ maxWidth: 200, fontFamily: MONO_FONT }} />
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={error} />}
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
        <Empty description="No records" />
      ) : (
        <LogStream records={result.records} />
      )}
    </Drawer>
  )
}

// ── main view ─────────────────────────────────────────────────────────────────

interface LogsViewProps {
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
  active: boolean
}

export function LogsView({ selectedHost, activeScope, active }: LogsViewProps) {
  const [venvs, setVenvs] = useState<LogStatusVenv[]>([])
  const [loading, setLoading] = useState(false)
  const [maintTarget, setMaintTarget] = useState<MaintTarget | null>(null)
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)
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
      render: (n: number) => n > 0 ? <Tag color="geekblue">{n}</Tag> : <Text type="secondary">0</Text>,
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
      render: (ts: string | null) => <Tooltip title={ts || ''}><span>{relTime(ts)}</span></Tooltip>,
    },
    {
      title: '',
      key: 'actions',
      align: 'right',
      render: (_: unknown, r: LogStatusRunnable) => (
        <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
          <Tooltip title="View merged stream">
            <Button size="small" type="text" icon={<EyeOutlined />} onClick={() => setViewTarget({ host, group: v.group, runnable: r.runnable })} />
          </Tooltip>
          <Tooltip title="Compact this runnable">
            <Button size="small" type="text" icon={<InboxOutlined />} onClick={() => setMaintTarget({ host, group: v.group, runnable: r.runnable, verb: 'compact' })} />
          </Tooltip>
          <Tooltip title="Prune this runnable">
            <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => setMaintTarget({ host, group: v.group, runnable: r.runnable, verb: 'prune' })} />
          </Tooltip>
        </div>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <Title level={5} style={{ margin: 0 }}>Logs — {host}</Title>
        <Button size="small" icon={<ReloadOutlined />} onClick={refresh} loading={loading}>Refresh</Button>
        <Tooltip title="Run analytics for this host — charts, success rate, durations and top runnables, scanned over the connection this tab just warmed">
          <Button size="small" icon={<BarChartOutlined />} onClick={() => setAnalyticsOpen(true)}>Analytics</Button>
        </Tooltip>
        <Text type="secondary" style={{ fontSize: 13 }}>
          Per-invocation audit logs ({'store = "per-run"'}) — view, compact, and prune per venv.
        </Text>
      </div>

      {loading && venvs.length === 0 ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : shown.length === 0 ? (
        <Empty description="No per-invocation logs found on this host" />
      ) : (
        shown.map(v => (
          <div key={v.group} style={{ marginBottom: 28 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <FolderOutlined style={{ color: 'var(--rs-text-muted)' }} />
              <Text strong style={{ fontFamily: MONO_FONT }}>{v.group}</Text>
              {v.group === HOME_GROUP && (
                <Tooltip title="Your home log dir (~/logs) — the per-user fallback runspec writes to when a venv's own logs dir isn't writable. Personal to your login on this host (the venv dirs above are the shared ones); pruning here affects only ~/logs.">
                  <Tag>home</Tag>
                </Tooltip>
              )}
              {v.ok ? (
                <Text type="secondary" style={{ fontSize: 13 }}>
                  {v.total_files ?? 0} file(s), {humanSize(v.total_bytes)}
                </Text>
              ) : (
                <Tag color="red">{v.error || 'unavailable'}</Tag>
              )}
              <div style={{ flex: 1 }} />
              {v.ok && (
                <>
                  {v.group !== HOME_GROUP && (
                    <Tooltip title="Make this venv's logs/ dir group-shared (2775 + setgid) so the runs index and audit logs are writable across the console group — the repair for a venv not built with create-venv --group">
                      <Button size="small" icon={<ShareAltOutlined />} onClick={() => setShareTarget({ host, venv: v.group })}>
                        Make shared…
                      </Button>
                    </Tooltip>
                  )}
                  <Button size="small" icon={<InboxOutlined />} onClick={() => setMaintTarget({ host, group: v.group, verb: 'compact' })}>
                    Compact…
                  </Button>
                  <Button size="small" danger icon={<DeleteOutlined />} onClick={() => setMaintTarget({ host, group: v.group, verb: 'prune' })}>
                    Prune…
                  </Button>
                </>
              )}
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

      <MaintenanceModal
        target={maintTarget}
        onClose={() => setMaintTarget(null)}
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
