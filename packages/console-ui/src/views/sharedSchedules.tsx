import { Tag, Typography, Tooltip, Alert } from 'antd'
import type { SharedSchedule, SharedScheduleOutput, SharedScheduleRun } from '../bridge'
import { isScheduled } from '../runBy'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'
import { timeoutLabel } from './allSchedules'

const { Text } = Typography

// Pieces of a host's *shared* schedules — runspec-scheduler's cron and daemon
// backends (docs/design/cron-scheduler.md), managed on the host with
// `runspec-scheduler add/sync`, not from the console. Each venv's
// `runspec-scheduler status` joins config ⋈ crontab/daemon ⋈ the runs index; every
// fire runs through `runspec exec`, so a run's exit code and captured output are
// here even when the runnable logs nothing itself. The list itself is the
// Schedules tab's All schedules view (AllSchedulesView.tsx).

export const RUN_COLOR: Record<string, string> = {
  completed: 'green', failed: 'red', crashed: 'volcano', running: 'blue', skipped: 'default',
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

export function ScheduleDetail({ s, daemon = false }: { s: SharedSchedule; daemon?: boolean }) {
  const isDark = useIsDark()
  const out: SharedScheduleOutput | null = s.last_output
  return (
    <div style={{ padding: '4px 8px' }}>
      {daemon && (
        <div style={{ marginBottom: 8, fontSize: 13, color: textSecondary(isDark) }}>
          Missed fires: {s.catch_up ? 'run once when the scheduler starts' : 'skipped, like cron'} · Timeout per fire: {timeoutLabel(s.timeout)}
        </div>
      )}
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
              <span style={{ color: textMuted(isDark), fontFamily: MONO_FONT, fontSize: 12 }}>{r.trigger === 'schedule:run-now' ? 'run now' : isScheduled(r.trigger) ? '' : r.trigger}</span>
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
