/**
 * WebhooksView — the Automation tab's Webhooks section: calls into the profile's
 * hosts' venvs (runspec-scheduler 0.11.0+ `[[hook]]`s), the approvals they wait
 * for, and which hosts this console follows.
 *
 * Reads the same per-venv runspec-scheduler status as the Schedules section
 * (one shared cache), so each host's venvs say whether they take webhooks, have
 * them off, or need a newer runspec-scheduler.
 */
import { readHostsLabel } from '../hostCount'
import { useMemo } from 'react'
import { Button, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { Host, Runnable, WebhooksStatus } from '../bridge'
import { useIsDark, textMuted } from '../ThemeContext'
import { EmptyState, PageToolbar } from '../components/kit'
import { WebhookApprovals } from '../components/WebhookApprovals'
import { collectRows } from './allSchedules'
import { useHostSchedulerReads } from './SchedulesView'
import { WebhooksSection } from './WebhooksSection'

export function WebhooksView({ hosts, runnables, status }: { hosts: Host[]; runnables?: Runnable[]; status: WebhooksStatus }) {
  const isDark = useIsDark()
  const { hostVenvs, hostsLoaded, hostsLoading, readHosts, targets } = useHostSchedulerReads(hosts)
  const hostGroups = useMemo(() => Object.fromEntries(hosts.map(h => [h.name, h.group ?? ''])) as Record<string, string>, [hosts])
  const collected = useMemo(() => collectRows([], hostVenvs, { hostGroups }), [hostVenvs, hostGroups])
  const empty = !status.venvs.length && !collected.venvs.length

  return (
    <div style={{ padding: '4px 0' }} data-testid="webhooks-view">
      <PageToolbar actions={<Button size="small" icon={<ReloadOutlined />} onClick={readHosts} loading={hostsLoading}>Refresh</Button>}>
        <span style={{ fontSize: 12, color: textMuted(isDark) }}>
          {readHostsLabel(targets.length)}
        </span>
      </PageToolbar>
      <WebhookApprovals status={status} />
      {empty ? (
        hostsLoaded
          ? <EmptyState message="No connected host in this profile has runspec-scheduler, so none takes webhooks. Install runspec-scheduler 0.11.0 or later into a venv from the Marketplace." />
          : <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spin /></div>
      ) : (
        <div style={{ marginTop: 8 }}>
          <WebhooksSection status={status} venvs={collected.venvs} editable={!!runnables} onChanged={readHosts} />
        </div>
      )}
    </div>
  )
}
