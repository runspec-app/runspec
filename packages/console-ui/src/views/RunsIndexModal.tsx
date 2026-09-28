import { useCallback, useEffect, useState } from 'react'
import {
  Modal, Button, Alert, Descriptions, Tag, Typography, Form, Input, InputNumber,
  Popconfirm, Spin, Space, Tooltip, message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import {
  bridge,
  type HistoryDbAction, type HistoryDbStats,
} from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

// The per-venv runs index (runspec_runs.db) the History tab reads. Core keeps it
// bounded on its own (ring prune + orphan reap on every run's finalize, per
// [config.history]); this modal is the explicit, on-demand side of that —
// inspect its health, then prune / reap / reindex. Each action shells out to
// the venv's own `runspec history … --scope … --json`, so the target venv needs
// runspec >= 0.62.0.

export interface RunsIndexTarget { host: string; group: string }

const STATUS_COLOR: Record<string, string> = {
  completed: 'green', failed: 'red', crashed: 'volcano', running: 'blue',
}

export function humanBytes(n: number | undefined): string {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`
}

const ACTION_DONE: Record<HistoryDbAction, (n: number) => string> = {
  prune: n => `Pruned ${n} row(s) from the runs index`,
  reap: n => `Marked ${n} orphaned running row(s) crashed`,
  reindex: n => `Reindexed ${n} run(s) from the audit logs`,
}

export function RunsIndexModal({ target, onClose }: {
  target: RunsIndexTarget | null
  onClose: () => void
}) {
  const [stats, setStats] = useState<HistoryDbStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<HistoryDbAction | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [olderThan, setOlderThan] = useState('')
  const [maxRows, setMaxRows] = useState<number | null>(null)
  const [runAs, setRunAs] = useState('')

  const load = useCallback((t: RunsIndexTarget) => {
    setLoading(true)
    Promise.resolve(bridge.history_db_stats(t.host, t.group))
      .then(r => setStats(r))
      .catch(e => setStats({ ok: false, group: t.group, error: String(e) }))
      .finally(() => setLoading(false))
  }, [])

  // Fresh target → reset the form, load stats, and pre-fill "Run as" with the venv
  // owner when it isn't the login user (a service-account-owned index isn't
  // writable by the operator). Not for ~/logs — the operator's own dir.
  useEffect(() => {
    setStats(null); setError(null); setBusy(null)
    setOlderThan(''); setMaxRows(null); setRunAs('')
    if (!target) return
    load(target)
    if (target.group === '~/logs') return
    let cancelled = false
    Promise.resolve(bridge.venv_owner(target.host, target.group))
      .then(r => { if (!cancelled && r?.ok && r.owner && r.owner !== r.self) setRunAs(r.owner) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [target, load])

  if (!target) return null
  const { host, group } = target
  const isHome = group === '~/logs'
  const hasPolicy = olderThan.trim().length > 0 || (maxRows != null && maxRows > 0)

  const act = async (action: HistoryDbAction) => {
    setBusy(action)
    setError(null)
    try {
      const r = await bridge.history_db_maintain(
        host, group, action,
        action === 'prune' ? (olderThan.trim() || undefined) : undefined,
        action === 'prune' ? (maxRows ?? undefined) : undefined,
        runAs.trim() || undefined,
      )
      if (!r.ok) { setError(r.error || `${action} failed.`); return }
      message.success(ACTION_DONE[action](r.count ?? 0))
      load(target)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }

  const s = stats
  const statusTags = s?.by_status
    ? Object.entries(s.by_status).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => (
        <Tag key={k} color={STATUS_COLOR[k]}>{k} {v}</Tag>
      ))
    : null

  return (
    <Modal
      open
      width={640}
      title={<span>Runs index — <span style={{ fontFamily: MONO_FONT }}>{group}</span></span>}
      onCancel={onClose}
      footer={[<Button key="close" onClick={onClose}>Close</Button>]}
    >
      <Text type="secondary" style={{ fontSize: 13 }}>
        The SQLite index (<Text code>runspec_runs.db</Text>) the History tab reads for fast loads.
        Runspec keeps it bounded on every run ({'[config.history]'} retention); use these to
        inspect it or force maintenance. It's derived from the audit logs, so <b>Reindex</b> is
        always safe.
      </Text>

      <div style={{ marginTop: 16, minHeight: 80 }}>
        {loading && !s ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 24 }}><Spin /></div>
        ) : s && !s.ok ? (
          <Alert type="error" showIcon message="Couldn't read the runs index" description={s.error} />
        ) : s && !s.exists ? (
          <Alert type="info" showIcon message="No runs index yet"
            description="Nothing has been indexed in this dir — it's created on the next run (or by Reindex, from existing audit logs)." />
        ) : s ? (
          <Descriptions size="small" column={1} bordered
            extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => load(target)} />}>
            <Descriptions.Item label="File">
              <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{s.path}</span>
            </Descriptions.Item>
            <Descriptions.Item label="Rows">
              <Space size={4} wrap>{s.rows ?? 0} {statusTags}</Space>
            </Descriptions.Item>
            <Descriptions.Item label="Size">{humanBytes(s.size_bytes)} (incl. WAL)</Descriptions.Item>
            <Descriptions.Item label="Span">
              {s.oldest ? `${s.oldest.slice(0, 10)} → ${(s.newest || '').slice(0, 10)}` : '—'}
            </Descriptions.Item>
            {(s.stale_running ?? 0) > 0 && (
              <Descriptions.Item label="Stale">
                <Tag color="orange">{s.stale_running} orphaned running row(s)</Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>Reap marks them crashed.</Text>
              </Descriptions.Item>
            )}
            {(s.log_pruned ?? 0) > 0 && (
              <Descriptions.Item label="Pruned logs">
                {s.log_pruned} row(s) outlive their pruned log file
              </Descriptions.Item>
            )}
          </Descriptions>
        ) : null}
        {s?.ok && s.write_error && (
          <Alert style={{ marginTop: 8 }} type="warning" showIcon
            message="Not writable by your login user"
            description={`${s.write_error}. Set Run as to the owner to maintain it.`} />
        )}
      </div>

      <Form layout="vertical" style={{ marginTop: 16 }}>
        {!isHome && (
          <Form.Item label="Run as" style={{ marginBottom: 12 }}
            help="Escalate to this OS user via sudo (pre-filled with the venv's owner). Blank runs as you.">
            <Input value={runAs} onChange={e => setRunAs(e.target.value)} placeholder="(yourself)"
              style={{ maxWidth: 200, fontFamily: MONO_FONT }} />
          </Form.Item>
        )}
        <Form.Item label="Prune policy" style={{ marginBottom: 8 }}
          help="Drop finished rows older than an age (day-granular) and/or beyond a row cap. Running rows are never pruned; the audit logs are untouched.">
          <Space wrap>
            <Input value={olderThan} onChange={e => setOlderThan(e.target.value)} placeholder="older than, e.g. 30d"
              aria-label="older than" style={{ width: 170, fontFamily: MONO_FONT }} />
            <InputNumber value={maxRows} onChange={v => setMaxRows(v)} min={1} placeholder="max rows"
              aria-label="max rows" style={{ width: 140 }} />
          </Space>
        </Form.Item>
      </Form>

      <Space wrap style={{ marginTop: 8 }}>
        <Popconfirm title="Prune the runs index?" description="Deleted rows can be restored by Reindex while their audit logs remain."
          onConfirm={() => act('prune')} disabled={!hasPolicy}>
          <Button danger disabled={!hasPolicy || busy !== null} loading={busy === 'prune'}>Prune</Button>
        </Popconfirm>
        <Tooltip title="Mark 'running' rows whose process is gone (or older than max_run_hours) as crashed">
          <Button disabled={busy !== null} loading={busy === 'reap'} onClick={() => act('reap')}>Reap orphans</Button>
        </Tooltip>
        <Tooltip title="Rebuild rows from the run_summary records in this dir's audit logs — repairs a missing or damaged index">
          <Button disabled={busy !== null} loading={busy === 'reindex'} onClick={() => act('reindex')}>Reindex</Button>
        </Tooltip>
      </Space>

      {error && <Alert type="error" showIcon style={{ marginTop: 12 }} message={error} />}
    </Modal>
  )
}
