import { useCallback, useEffect, useState } from 'react'
import {
  Table, Tag, Typography, Button, Tooltip, message, Modal, Drawer,
  Input, InputNumber, Switch, Form, Empty, Spin, Alert, Popconfirm,
} from 'antd'
import {
  ReloadOutlined, DeleteOutlined, InboxOutlined, EyeOutlined, FolderOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge,
  type LogStatusVenv, type LogStatusRunnable, type LogViewResult,
} from '../bridge'

const { Title, Text } = Typography

const LOG_LEVEL_COLOUR: Record<string, string> = {
  INFO: '#4fc1ff',
  WARNING: '#faad14',
  ERROR: '#ff4d4f',
  CRITICAL: '#ff4d4f',
  DEBUG: '#888',
}

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
  onApplied: () => void
}

function MaintenanceModal({ target, onClose, onApplied }: MaintModalProps) {
  const [olderThan, setOlderThan] = useState('')
  const [maxFiles, setMaxFiles] = useState<number | null>(null)
  const [maxTotalSize, setMaxTotalSize] = useState('')
  const [gzip, setGzip] = useState(true)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)

  // Reset form whenever a fresh target opens the modal.
  useEffect(() => {
    setOlderThan(target?.verb === 'compact' ? '7d' : '')
    setMaxFiles(null)
    setMaxTotalSize('')
    setGzip(true)
    setPreview(null)
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
    try {
      if (verb === 'prune') {
        const r = await bridge.logs_prune(host, group, runnable, olderThan.trim() || undefined, maxFiles ?? undefined, maxTotalSize.trim() || undefined, dryRun)
        if (!r.ok) { message.error(r.error || 'prune failed'); return }
        if (dryRun) {
          setPreview(`${r.count ?? 0} file(s) would be deleted — ${humanSize(r.freed_bytes)} freed.`)
        } else {
          message.success(`Deleted ${r.count ?? 0} file(s) — ${humanSize(r.freed_bytes)} freed`)
          onApplied(); onClose()
        }
      } else {
        const r = await bridge.logs_compact(host, group, runnable, olderThan.trim() || undefined, gzip, dryRun)
        if (!r.ok) { message.error(r.error || 'compact failed'); return }
        if (dryRun) {
          setPreview(`${r.compacted ?? 0} file(s) would roll into ${r.archives?.length ?? 0} archive(s).`)
        } else {
          message.success(`Compacted ${r.compacted ?? 0} file(s) into ${r.archives?.length ?? 0} archive(s)`)
          onApplied(); onClose()
        }
      }
    } catch (e) {
      message.error(String(e))
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
      <Text type="secondary" style={{ fontSize: 12 }}>
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
      {!hasPolicy && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {verb === 'compact' ? 'Set an age threshold to continue.' : 'Set at least one policy to continue.'}
        </Text>
      )}
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
      width={680}
      title={target ? <span style={{ fontFamily: 'monospace' }}>{target.group}/{target.runnable} — merged stream</span> : ''}
    >
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : !result?.ok ? (
        <Alert type="error" showIcon message={result?.error || 'Failed to read logs'} />
      ) : result.records.length === 0 ? (
        <Empty description="No records" />
      ) : (
        <div style={{ background: '#0d0d0d', border: '1px solid #222', borderRadius: 6, padding: '8px 12px', fontFamily: 'monospace', fontSize: 12, lineHeight: 1.8 }}>
          {result.records.map((rec, i) => {
            const r = rec as { ts?: string; level?: string; message?: string; extra?: { run_id?: string } }
            const colour = LOG_LEVEL_COLOUR[r.level ?? ''] ?? '#d4d4d4'
            return (
              <div key={i}>
                <span style={{ color: '#555', marginRight: 8 }}>{r.ts ? new Date(r.ts).toLocaleTimeString() : '—'}</span>
                <span style={{ color: colour, marginRight: 8, minWidth: 56, display: 'inline-block' }}>{r.level ?? ''}</span>
                {r.extra?.run_id && <span style={{ color: '#7a6', marginRight: 8 }}>{String(r.extra.run_id).slice(0, 8)}</span>}
                <span style={{ color: '#d4d4d4' }}>{r.message ?? ''}</span>
              </div>
            )
          })}
        </div>
      )}
    </Drawer>
  )
}

// ── main view ─────────────────────────────────────────────────────────────────

interface LogsViewProps {
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
}

export function LogsView({ selectedHost, activeScope }: LogsViewProps) {
  const [venvs, setVenvs] = useState<LogStatusVenv[]>([])
  const [loading, setLoading] = useState(true)
  const [maintTarget, setMaintTarget] = useState<MaintTarget | null>(null)
  const [viewTarget, setViewTarget] = useState<ViewTarget | null>(null)

  const host = selectedHost || 'local'

  const refresh = useCallback(() => {
    setLoading(true)
    bridge.logs_status(host)
      .then(setVenvs)
      .catch(e => message.error(String(e)))
      .finally(() => setLoading(false))
  }, [host])

  useEffect(() => { refresh() }, [refresh])

  const shown = activeScope.length > 0
    ? venvs.filter(v => activeScope.includes(v.group))
    : venvs

  const runnableColumns = (v: LogStatusVenv): ColumnType<LogStatusRunnable>[] => [
    {
      title: 'Runnable',
      dataIndex: 'runnable',
      key: 'runnable',
      render: (name: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{name}</span>,
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
        <Text type="secondary" style={{ fontSize: 12 }}>
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
              <FolderOutlined style={{ color: '#888' }} />
              <Text strong style={{ fontFamily: 'monospace' }}>{v.group}</Text>
              {v.ok ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {v.total_files ?? 0} file(s), {humanSize(v.total_bytes)}
                </Text>
              ) : (
                <Tag color="red">{v.error || 'unavailable'}</Tag>
              )}
              <div style={{ flex: 1 }} />
              {v.ok && (
                <>
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
              <Text type="secondary" style={{ fontSize: 11 }}>{v.dirs.join('  ·  ')}</Text>
            )}
          </div>
        ))
      )}

      <MaintenanceModal target={maintTarget} onClose={() => setMaintTarget(null)} onApplied={refresh} />
      <StreamDrawer target={viewTarget} onClose={() => setViewTarget(null)} />
    </div>
  )
}
