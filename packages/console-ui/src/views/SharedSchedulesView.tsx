import { useCallback, useEffect, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, Alert, Empty, Spin, message } from 'antd'
import { ReloadOutlined, FolderOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge,
  type SharedSchedule, type SharedScheduleOutput, type SharedScheduleRun, type SharedSchedulesVenv,
} from '../bridge'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'

const { Title, Text } = Typography

// Read-only view of a host's *shared* schedules — the cron backend of
// runspec-scheduler (docs/design/cron-scheduler.md), managed on the host with
// `runspec-scheduler add/sync`, not from the console. Each venv's
// `runspec-scheduler status` joins config ⋈ crontab ⋈ the runs index; every fire
// runs through `runspec exec`, so a run's exit code and captured output are here
// even when the runnable logs nothing itself.

const RUN_COLOR: Record<string, string> = {
  completed: 'green', failed: 'red', crashed: 'volcano', running: 'blue', skipped: 'default',
}

export function whenLabel(s: Pick<SharedSchedule, 'when'>): string {
  const w = s.when || {}
  const base = w.cron ? w.cron : w.every ? `every ${w.every}` : '—'
  return w.timezone ? `${base} (${w.timezone})` : base
}

export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const s = Math.round((t - now) / 1000)
  const a = Math.abs(s)
  const v = a < 60 ? `${a}s` : a < 3600 ? `${Math.round(a / 60)}m` : a < 86400 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`
  return s >= 0 ? `in ${v}` : `${v} ago`
}

/** Crontab state → a tag: installed (exec line), legacy line, not installed, unknown. */
export function crontabTag(s: SharedSchedule): { label: string; color: string; tip: string } {
  if (!s.installable) return { label: 'not installable', color: 'default', tip: s.not_installable_reason || '' }
  if (s.installed === null) return { label: 'unknown', color: 'default', tip: `Couldn't read ${s.run_as || 'the owner'}'s crontab without a password prompt` }
  if (!s.installed) return { label: 'not installed', color: 'orange', tip: 'In the config but not in the crontab — run `runspec-scheduler sync` on the host' }
  if (s.line === 'legacy') return { label: 'legacy line', color: 'gold', tip: 'Installed by an older runspec-scheduler: fires run unrecorded (no captured output, silent skips). Re-run `runspec-scheduler sync` on the host.' }
  return { label: 'installed', color: 'green', tip: 'Crontab line runs through `runspec exec` — every fire is recorded' }
}

function RunTag({ run }: { run: SharedScheduleRun }) {
  const label = run.status === 'failed' && run.exit_code != null ? `exit ${run.exit_code}` : run.status
  return <Tag color={RUN_COLOR[run.status] ?? 'default'}>{label}</Tag>
}

function OutputBlock({ title, text }: { title: string; text: string }) {
  if (!text) return null
  return (
    <div style={{ marginTop: 8 }}>
      <Text type="secondary" style={{ fontSize: 12 }}>{title}</Text>
      <pre style={{
        margin: '4px 0 0', padding: '8px 10px', maxHeight: 260, overflow: 'auto',
        fontFamily: MONO_FONT, fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm,
      }}>{text}</pre>
    </div>
  )
}

function ScheduleDetail({ s }: { s: SharedSchedule }) {
  const isDark = useIsDark()
  const out: SharedScheduleOutput | null = s.last_output
  return (
    <div style={{ padding: '4px 8px' }}>
      <Text strong style={{ fontSize: 13 }}>Recent runs</Text>
      {s.runs.length === 0 ? (
        <div><Text type="secondary" style={{ fontSize: 13 }}>No recorded runs yet{s.line === 'legacy' ? ' — a legacy crontab line doesn’t record fires' : ''}.</Text></div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'auto auto auto 1fr', gap: '2px 14px', marginTop: 4, fontSize: 13, alignItems: 'center' }}>
          {s.runs.map(r => (
            <div key={r.run_id} style={{ display: 'contents' }}>
              <RunTag run={r} />
              <Tooltip title={r.start_ts}><span style={{ color: textSecondary(isDark) }}>{relTime(r.start_ts)}</span></Tooltip>
              <span style={{ color: textMuted(isDark) }}>{r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : '—'}</span>
              <span style={{ color: textMuted(isDark), fontFamily: MONO_FONT, fontSize: 12 }}>{r.trigger === 'schedule' ? '' : r.trigger}</span>
            </div>
          ))}
        </div>
      )}
      {out && (
        <div style={{ marginTop: 12 }}>
          <Text strong style={{ fontSize: 13 }}>Last run output</Text>
          {out.result?.message && <div><Text type="secondary" style={{ fontSize: 13 }}>{out.result.message}</Text></div>}
          {out.errors.length > 0 && (
            <Alert type="error" showIcon style={{ marginTop: 6 }} message={out.errors.length === 1 ? out.errors[0] : `${out.errors.length} errors`}
              description={out.errors.length > 1 ? <ul style={{ margin: 0, paddingLeft: 18 }}>{out.errors.map((e, i) => <li key={i}>{e}</li>)}</ul> : undefined} />
          )}
          <OutputBlock title="stdout" text={out.stdout} />
          <OutputBlock title="stderr" text={out.stderr} />
          {!out.stdout && !out.stderr && <div><Text type="secondary" style={{ fontSize: 13 }}>No output captured.</Text></div>}
          {out.truncated && <Text type="secondary" style={{ fontSize: 12 }}>Showing the tail — the full capture is in the run's log (History → the run).</Text>}
        </div>
      )}
    </div>
  )
}

const columns: ColumnType<SharedSchedule>[] = [
  {
    title: 'Schedule',
    key: 'id',
    render: (_: unknown, s) => (
      <span>
        <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{s.id}</span>
        {!s.enabled && <Tag style={{ marginLeft: 6 }}>disabled</Tag>}
      </span>
    ),
  },
  {
    title: 'Runnable',
    key: 'runnable',
    render: (_: unknown, s) => (
      <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>
        {[s.runnable, ...(s.command ?? [])].join(' ')}
        {s.args && Object.keys(s.args).length > 0 && (
          <Text type="secondary" style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
            {' '}{Object.entries(s.args).map(([k, v]) => `--${k}=${String(v)}`).join(' ')}
          </Text>
        )}
      </span>
    ),
  },
  { title: 'When', key: 'when', render: (_: unknown, s) => <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{whenLabel(s)}</span> },
  { title: 'Run as', key: 'run_as', render: (_: unknown, s) => <code style={{ fontSize: 13 }}>{s.run_as || '—'}</code> },
  {
    title: 'Crontab',
    key: 'crontab',
    render: (_: unknown, s) => { const t = crontabTag(s); return <Tooltip title={t.tip}><Tag color={t.color}>{t.label}</Tag></Tooltip> },
  },
  {
    title: 'Last run',
    key: 'last',
    render: (_: unknown, s) => {
      const r = s.runs[0]
      if (!r) return <Text type="secondary">—</Text>
      return <span><RunTag run={r} /><Tooltip title={r.start_ts}><Text type="secondary" style={{ fontSize: 13 }}>{relTime(r.start_ts)}</Text></Tooltip></span>
    },
  },
  { title: 'Next run', key: 'next', render: (_: unknown, s) => <Tooltip title={s.next_run || ''}><span>{s.enabled && s.installed !== false ? relTime(s.next_run) : '—'}</span></Tooltip> },
]

export function SharedSchedulesView({ host }: { host: string }) {
  const [venvs, setVenvs] = useState<SharedSchedulesVenv[] | null>(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(() => {
    setLoading(true)
    Promise.resolve(bridge.shared_schedules(host))
      .then(r => setVenvs(r ?? []))
      .catch(e => { message.error(String(e)); setVenvs([]) })
      .finally(() => setLoading(false))
  }, [host])

  // A host switch drops the old host's data so it never shows under the new title.
  useEffect(() => { setVenvs(null); refresh() }, [refresh])

  const withScheduler = (venvs ?? []).filter(v => v.available)
  const without = (venvs ?? []).filter(v => !v.available).map(v => v.group)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
        <Title level={5} style={{ margin: 0 }}>Shared schedules — {host}</Title>
        <Button size="small" icon={<ReloadOutlined />} onClick={refresh} loading={loading}>Refresh</Button>
        <Text type="secondary" style={{ fontSize: 13 }}>
          Read-only. Host cron schedules managed with <Text code>runspec-scheduler</Text> on the host; every fire is recorded via <Text code>runspec exec</Text>.
        </Text>
      </div>

      {venvs === null ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : withScheduler.length === 0 ? (
        <Empty description={`No venv on ${host} has runspec-scheduler installed. Install it into a venv from the Marketplace.`} />
      ) : (
        withScheduler.map(v => {
          const st = v.status
          return (
            <div key={v.group} style={{ marginBottom: 28 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <FolderOutlined style={{ color: 'var(--rs-text-muted)' }} />
                <Text strong style={{ fontFamily: MONO_FONT }}>{v.group}</Text>
                {st && <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>{st.config}</Text>}
              </div>
              {!v.ok ? (
                <Alert type="error" showIcon message="Couldn't read this venv's schedules" description={v.error} />
              ) : st ? (
                <>
                  {st.errors.map((e, i) => <Alert key={i} type="error" showIcon style={{ marginBottom: 8 }} message={e} />)}
                  {st.stale.length > 0 && (
                    <Alert type="warning" showIcon style={{ marginBottom: 8 }}
                      message={`${st.stale.length} crontab line(s) no longer in the config`}
                      description={<span>{st.stale.map(x => `${x.id} (${x.user})`).join(', ')} — <Text code>runspec-scheduler sync</Text> on the host removes them.</span>} />
                  )}
                  <Table<SharedSchedule>
                    dataSource={st.schedules}
                    columns={columns}
                    rowKey="id"
                    size="small"
                    pagination={false}
                    locale={{ emptyText: 'No schedules configured' }}
                    expandable={{ expandedRowRender: s => <ScheduleDetail s={s} /> }}
                  />
                </>
              ) : null}
            </div>
          )
        })
      )}
      {without.length > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>No runspec-scheduler in: {without.join(', ')}. Install it into a venv from the Marketplace to schedule that venv's runnables.</Text>
      )}
    </div>
  )
}
