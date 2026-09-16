/**
 * DenylistModal — the fleet kill switch, opened from the bottom of the hosts
 * panel. Scoped to the hosts that are *connected right now* in the active
 * profile: the button that opens it stays disabled until the fleet sweep has
 * settled, and every status read / `runspec deny` mutation targets only those
 * hosts — so it is fast (no waiting out SSH timeouts on down hosts) and the
 * host count it shows is the one you can see is green in the sidebar. Was a
 * Settings tab querying every configured host regardless of profile/state.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Modal, Alert, Button, Divider, Input, Popconfirm, Segmented, Select, Space, Table, Tag, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined, StopOutlined, WarningOutlined, LoadingOutlined } from '@ant-design/icons'
import { bridge, type DenylistHostStatus, type DenylistPayload, type Host } from '../bridge'
import { RADIUS } from '../ThemeContext'

const { Text } = Typography

export interface DenylistModalProps {
  open: boolean
  onClose: () => void
  /** The hosts in scope — the currently *connected* hosts of the active
   *  profile (plus local). Every query and mutation is limited to these, so a
   *  disconnected host never waits out an SSH timeout here. */
  hosts: Host[]
}

export function DenylistModal({ open, onClose, hosts }: DenylistModalProps) {
  const [targets, setTargets] = useState<string[]>([])
  const [runnableGlobs, setRunnableGlobs] = useState<string[]>([])
  const [userGlobs, setUserGlobs] = useState<string[]>([])
  const [hostGlobs, setHostGlobs] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const [mode, setMode] = useState<'enforce' | 'warn' | 'off'>('enforce')
  const [result, setResult] = useState('')
  const [busy, setBusy] = useState('')
  const [status, setStatus] = useState<DenylistHostStatus[]>([])
  const [loadingStatus, setLoadingStatus] = useState(false)

  // The in-scope host names — what every backend call is limited to.
  const scopeNames = useMemo(() => hosts.map(h => h.name), [hosts])
  const scopeKey = scopeNames.join('\u0000')
  const effectiveTargets = targets.length ? targets : scopeNames
  const scope = targets.length
    ? `${targets.length} selected host(s)`
    : `all ${scopeNames.length} connected host${scopeNames.length === 1 ? '' : 's'}`

  // One read of the connected hosts (in parallel on the backend) when the modal
  // opens, on Refresh, and after a mutation. The host picker filters the loaded
  // rows client-side — it never re-queries on a selection change.
  const refresh = useCallback(async () => {
    if (!scopeNames.length) { setStatus([]); return }
    setLoadingStatus(true)
    try {
      setStatus(await bridge.denylist_status(scopeNames))
    } catch (err) {
      message.error((err as Error)?.message || 'could not read denylist status')
    } finally {
      setLoadingStatus(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- scopeKey stands in for scopeNames
  }, [scopeKey])

  useEffect(() => {
    if (open) refresh()
  }, [open, refresh])
  // Drop any picked host that has since left the connected set.
  useEffect(() => {
    setTargets(prev => prev.filter(n => scopeNames.includes(n)))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey])

  const targetSet = new Set(targets)
  const shownStatus = targets.length ? status.filter(r => targetSet.has(r.host)) : status

  const run = async (payload: DenylistPayload, label: string) => {
    setBusy(label)
    try {
      setResult(await bridge.manage_denylist({ ...payload, targets: effectiveTargets }))
      await refresh()  // reflect the change in the table immediately
    } catch (err) {
      message.error((err as Error)?.message || 'denylist action failed')
    } finally {
      setBusy('')
    }
  }

  const hostOptions = hosts.map(h => ({
    value: h.name,
    label: h.group ? `${h.name} (${h.group})` : h.name,
  }))

  const ruleSummary = (r: DenylistHostStatus): string => {
    if (!r.ok) return r.error || 'unreachable'
    if (r.disabled) return 'ALL disabled' + (r.reason ? ` (${r.reason})` : '')
    if (!r.rules?.length) return 'none'
    return r.rules
      .map(rule => `${rule.runnables.join(',')} [users ${rule.users.join(',')}; hosts ${rule.hosts.join(',')}]${rule.reason ? ` — ${rule.reason}` : ''}`)
      .join(' · ')
  }

  const statusColumns = [
    { title: 'Host', dataIndex: 'host', key: 'host' },
    { title: 'Venv', dataIndex: 'venv', key: 'venv' },
    {
      title: 'Mode',
      key: 'mode',
      render: (_: unknown, r: DenylistHostStatus) =>
        !r.ok ? <Tag color="default">—</Tag>
          : <Tag color={r.mode === 'off' ? 'default' : r.mode === 'warn' ? 'gold' : 'blue'}>{r.mode}</Tag>,
    },
    {
      title: 'Disabled',
      key: 'disabled',
      render: (_: unknown, r: DenylistHostStatus) => {
        if (!r.ok) return <Tag color="red">error</Tag>
        if (r.disabled) return <Tag color="red">ALL</Tag>
        if (r.rules?.length) return <Tag color="orange">{r.rules.length} rule{r.rules.length === 1 ? '' : 's'}</Tag>
        return <Tag color="green">none</Tag>
      },
    },
    {
      title: 'Detail',
      key: 'detail',
      render: (_: unknown, r: DenylistHostStatus) => (
        <Tooltip title={ruleSummary(r)}>
          <Text style={{ fontSize: 13 }} ellipsis={{ tooltip: false }}>{ruleSummary(r)}</Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
      title={<span><StopOutlined style={{ marginRight: 8 }} />Denylist — fleet kill switch</span>}
      destroyOnHidden
    >
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        style={{ marginBottom: 12 }}
        message="Kill switch"
        description={
          <Text type="secondary" style={{ fontSize: 13 }}>
            Disable or re-enable runspec runnables on the <strong>{scopeNames.length} connected</strong> host{scopeNames.length === 1 ? '' : 's'} of the
            active profile. This writes each host's venv denylist via the core <Text code>runspec deny</Text> over SSH —
            it stops the <em>runnables</em> on those hosts, never this console, and
            “Re-enable all” always works. A host that is disconnected right now is not touched; reopen once it
            reconnects. Remote hosts need <Text code>runspec ≥ 0.43.0</Text>.
          </Text>
        }
      />

      <div style={{ marginBottom: 8 }}>
        <Text style={{ fontSize: 13 }}>Target hosts</Text>
        <Select
          mode="multiple"
          allowClear
          size="small"
          style={{ width: '100%', marginTop: 4 }}
          placeholder={`All ${scopeNames.length} connected host${scopeNames.length === 1 ? '' : 's'} (leave empty to apply to every one)`}
          value={targets}
          onChange={setTargets}
          options={hostOptions}
        />
      </div>

      <Table<DenylistHostStatus>
        size="small"
        rowKey={r => `${r.host}/${r.venv}`}
        columns={statusColumns}
        dataSource={shownStatus}
        loading={loadingStatus ? { tip: `Querying ${scopeNames.length} connected host${scopeNames.length === 1 ? '' : 's'}…` } : false}
        pagination={false}
        style={{ marginBottom: 12 }}
        locale={{ emptyText: !scopeNames.length ? 'No connected hosts.' : targets.length && status.length ? 'No rows for the selected hosts.' : 'No connected host has a runspec path.' }}
      />

      <Space wrap style={{ marginBottom: 12 }}>
        <Button
          size="small"
          icon={loadingStatus ? <LoadingOutlined /> : <ReloadOutlined />}
          loading={loadingStatus}
          onClick={refresh}
        >
          Refresh status
        </Button>
        <Popconfirm
          title="Disable ALL runnables?"
          icon={<WarningOutlined style={{ color: '#faad14' }} />}
          description={
            <span style={{ fontSize: 13 }}>
              Blocks every runnable on {scope}.<br />
              Agent and manual runs are refused until you re-enable.
            </span>
          }
          okText="Disable all"
          okButtonProps={{ danger: true }}
          onConfirm={() => run({ action: 'all', reason }, 'all')}
        >
          <Button size="small" danger icon={<StopOutlined />} loading={busy === 'all'}>
            Disable all
          </Button>
        </Popconfirm>
        <Popconfirm
          title="Re-enable everything?"
          description={<span style={{ fontSize: 13 }}>Clears the denylist on {scope}.</span>}
          okText="Re-enable"
          onConfirm={() => run({ action: 'clear' }, 'clear')}
        >
          <Button size="small" icon={<ReloadOutlined />} loading={busy === 'clear'}>
            Re-enable all (clear)
          </Button>
        </Popconfirm>
      </Space>

      <Divider style={{ margin: '8px 0' }} orientation="left" plain>
        <Text type="secondary" style={{ fontSize: 13 }}>Disable specific runnables</Text>
      </Divider>
      <Space direction="vertical" style={{ width: '100%', marginBottom: 12 }} size={6}>
        <Select mode="tags" size="small" style={{ width: '100%' }} value={runnableGlobs} onChange={setRunnableGlobs} placeholder="Runnable name globs (e.g. deploy, restart-*) — empty = all" />
        <Select mode="tags" size="small" style={{ width: '100%' }} value={userGlobs} onChange={setUserGlobs} placeholder="Username globs — empty = all users" />
        <Select mode="tags" size="small" style={{ width: '100%' }} value={hostGlobs} onChange={setHostGlobs} placeholder="Hostname globs the rule applies to — empty = all hosts" />
        <Input size="small" placeholder="Reason (shown on the refusal)" value={reason} onChange={e => setReason(e.target.value)} />
        <Button
          size="small"
          icon={<StopOutlined />}
          loading={busy === 'add'}
          onClick={() => run({ action: 'add', runnable: runnableGlobs, user: userGlobs, host: hostGlobs, reason }, 'add')}
        >
          Add deny rule
        </Button>
      </Space>

      <Divider style={{ margin: '8px 0' }} orientation="left" plain>
        <Text type="secondary" style={{ fontSize: 13 }}>Enforcement mode</Text>
      </Divider>
      <Space style={{ marginBottom: 12 }}>
        <Segmented
          size="small"
          value={mode}
          onChange={v => setMode(v as 'enforce' | 'warn' | 'off')}
          options={[
            { label: 'Enforce', value: 'enforce' },
            { label: 'Warn', value: 'warn' },
            { label: 'Off', value: 'off' },
          ]}
        />
        <Button size="small" loading={busy === 'mode'} onClick={() => run({ action: 'mode', setting: mode }, 'mode')}>
          Apply mode
        </Button>
      </Space>

      {result && (
        <pre style={{ fontSize: 13, background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: 10, whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>
          {result}
        </pre>
      )}
    </Modal>
  )
}
