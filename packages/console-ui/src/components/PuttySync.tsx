import { useEffect, useState } from 'react'
import { Alert, Button, Input, InputNumber, Popconfirm, Select, Space, Switch, Typography, message } from 'antd'
import { SyncOutlined } from '@ant-design/icons'
import { bridge, type HostProfile, type PuttySyncConfig, type PuttySyncResult, type PuttySyncStatus } from '../bridge'
import { textSecondary, useIsDark } from '../ThemeContext'

const { Text } = Typography

export const DEFAULT_PUTTY_SYNC: PuttySyncConfig = { sync: false, profiles: [], keepalive: 30, prefix: '' }

/** The `[putty]` table to save (see runspec_console/putty_sync.py). */
export function puttySyncToConfig(c: PuttySyncConfig): Record<string, unknown> {
  const out: Record<string, unknown> = { sync: c.sync, keepalive: Math.max(0, Math.round(c.keepalive || 0)) }
  const profiles = c.profiles.map(p => p.trim()).filter(Boolean)
  if (profiles.length) out.profiles = profiles
  if (c.prefix.trim()) out.prefix = c.prefix.trim()
  return out
}

/** One line describing a sync result, e.g. "12 sessions written, 1 removed, 2 skipped". */
export function summarizeSync(r: PuttySyncResult): string {
  if (!r.supported) return r.error || 'PuTTY saved sessions are only on Windows.'
  if (!r.ok && r.error) return r.error
  const n = r.written.length
  const parts = [`${n} session${n === 1 ? '' : 's'} written`]
  if (r.removed.length) parts.push(`${r.removed.length} removed`)
  if (r.skipped.length) parts.push(`${r.skipped.length} skipped`)
  return parts.join(', ')
}

/** Settings → SSH → PuTTY sessions: keep one PuTTY saved session per console host. */
export function PuttySync() {
  const isDark = useIsDark()
  const [status, setStatus] = useState<PuttySyncStatus | null>(null)
  const [cfg, setCfg] = useState<PuttySyncConfig>(DEFAULT_PUTTY_SYNC)
  const [profiles, setProfiles] = useState<HostProfile[]>([])
  const [result, setResult] = useState<PuttySyncResult | null>(null)
  const [busy, setBusy] = useState<'' | 'save' | 'sync' | 'remove'>('')

  const refresh = () =>
    bridge.putty_sync_status().then(s => {
      setStatus(s ?? null)
      setCfg({ ...DEFAULT_PUTTY_SYNC, ...(s?.config ?? {}) })
      if (s?.last) setResult(s.last)
    }).catch(() => setStatus(null))

  useEffect(() => {
    void refresh()
    bridge.get_profiles().then(ps => setProfiles(ps ?? [])).catch(() => setProfiles([]))
  }, [])

  const set = (patch: Partial<PuttySyncConfig>) => setCfg(c => ({ ...c, ...patch }))

  const save = async () => {
    setBusy('save')
    try {
      const full = await bridge.get_config()
      await bridge.save_config({ ...full, putty: puttySyncToConfig(cfg) })
      message.success(cfg.sync ? 'Saved. PuTTY sessions are updated in the background.' : 'Saved')
      setTimeout(() => { void refresh() }, 1500)
    } catch (e) {
      message.error(`Could not save: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy('')
    }
  }

  const run = async (kind: 'sync' | 'remove') => {
    setBusy(kind)
    try {
      const r = kind === 'sync' ? await bridge.putty_sync_now() : await bridge.putty_sync_remove()
      setResult(r)
      if (r.ok) message.success(summarizeSync(r))
      else message.error(summarizeSync(r))
      void refresh()
    } catch (e) {
      message.error(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy('')
    }
  }

  const label = (s: string) => <Text style={{ fontSize: 12, color: textSecondary(isDark), display: 'block', marginBottom: 4 }}>{s}</Text>
  const unsupported = status !== null && !status.supported

  return (
    <div style={{ marginTop: 8 }} data-testid="putty-sync">
      <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 4 }}>PuTTY sessions</Text>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        Save every console host as a PuTTY session, so PuTTY on its own lists them with the host, user, key,
        keepalive, proxy or jump host and terminal look already set. Sessions are named <Text code>group/host</Text>.
        Only sessions the console made are ever changed or removed.
      </Text>

      {unsupported && (
        <Alert type="info" showIcon style={{ marginBottom: 10 }} message="PuTTY saved sessions are only on Windows." />
      )}
      {status?.supported && !status.putty && (
        <Alert type="warning" showIcon style={{ marginBottom: 10 }} message="PuTTY isn't installed here. Sessions are still saved, ready for when it is." />
      )}
      {status?.supported && !status.plink && (
        <Alert type="warning" showIcon style={{ marginBottom: 10 }} message="plink.exe wasn't found, so hosts behind a jump host are skipped. It ships with PuTTY." />
      )}

      <Space wrap size={[12, 8]} align="start" style={{ marginBottom: 8 }}>
        <div>
          {label('Keep in sync automatically')}
          <Switch size="small" checked={cfg.sync} disabled={unsupported} onChange={v => set({ sync: v })} aria-label="Keep PuTTY sessions in sync" />
        </div>
        <div style={{ width: 260 }}>
          {label('Hosts in profiles')}
          <Select
            mode="multiple" size="small" style={{ width: '100%' }} allowClear placeholder="All hosts"
            value={cfg.profiles} onChange={v => set({ profiles: v })} aria-label="Profiles to sync"
            options={profiles.map(p => ({ value: p.name, label: p.name }))}
          />
        </div>
        <div style={{ width: 110 }}>
          {label('Keepalive (s)')}
          <InputNumber size="small" min={0} max={3600} style={{ width: '100%' }} value={cfg.keepalive} onChange={v => set({ keepalive: v ?? 0 })} aria-label="Keepalive seconds" />
        </div>
        <div style={{ width: 120 }}>
          {label('Name prefix')}
          <Input size="small" value={cfg.prefix} placeholder="none" onChange={e => set({ prefix: e.target.value })} aria-label="Session name prefix" />
        </div>
      </Space>

      <Space wrap style={{ marginBottom: 8 }}>
        <Button size="small" type="primary" loading={busy === 'save'} onClick={save}>Save PuTTY settings</Button>
        <Button size="small" icon={<SyncOutlined />} loading={busy === 'sync'} disabled={unsupported} onClick={() => run('sync')}>Sync now</Button>
        <Popconfirm
          title="Remove the PuTTY sessions the console saved?"
          description="Sessions you made yourself are kept."
          okText="Remove" onConfirm={() => run('remove')}
          disabled={unsupported || !status?.sessions.length}
        >
          <Button size="small" danger loading={busy === 'remove'} disabled={unsupported || !status?.sessions.length}>
            Remove synced sessions
          </Button>
        </Popconfirm>
        {status?.supported && (
          <Text type="secondary" style={{ fontSize: 12 }}>{status.sessions.length} saved by the console</Text>
        )}
      </Space>

      {result && result.supported && (
        <div style={{ fontSize: 12 }} data-testid="putty-sync-result">
          <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 12 }}>{summarizeSync(result)}</Text>
          {result.skipped.length > 0 && (
            <ul style={{ margin: '4px 0 0', paddingLeft: 18, color: textSecondary(isDark) }}>
              {result.skipped.map(s => (
                <li key={s.host}><Text code style={{ fontSize: 11 }}>{s.host}</Text> {s.reason}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
