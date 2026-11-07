/**
 * Who or what started a run, for History's "Run by" column. A runspec-scheduler
 * fire (label `sched:<id>`) runs with RUNSPEC_AGENT set — it is non-interactive —
 * so its `agent` flag is true, but it wasn't the assistant: it shows as the
 * backend that fired it. runspec-scheduler 0.7.0 records that backend in the
 * trigger (`schedule:cron` / `schedule:daemon` / `schedule:run-now`); older
 * fires say only `schedule` and show as a host schedule.
 */

export type RunByKind = 'user' | 'agent' | 'cron' | 'daemon' | 'run-now' | 'host-schedule'

export interface RunBy { kind: RunByKind; label: string; tip: string }

const HOST: Record<string, RunBy> = {
  'schedule:cron': { kind: 'cron', label: 'cron', tip: "Fired by the host's crontab (runspec-scheduler, cron backend)" },
  'schedule:daemon': { kind: 'daemon', label: 'daemon', tip: "Fired by the venv's runspec-scheduler daemon" },
  'schedule:run-now': { kind: 'run-now', label: 'run now', tip: 'A host schedule started by hand with Run now' },
}

export function runBy(r: { initiatedBy?: string; trigger?: string; label?: string }): RunBy {
  const trigger = r.trigger ?? ''
  if (HOST[trigger]) return HOST[trigger]
  if ((r.label ?? '').startsWith('sched:')) {
    return { kind: 'host-schedule', label: 'host sched', tip: 'Fired by a host schedule (runspec-scheduler before 0.7.0 didn’t record whether cron or the daemon fired it)' }
  }
  if (r.initiatedBy === 'llm') return { kind: 'agent', label: 'agent', tip: 'Triggered by the assistant' }
  return { kind: 'user', label: 'user', tip: 'Run by an operator' }
}

/** Whether the run was fired by a schedule (the console's own or a host's). */
export function isScheduled(trigger: string | undefined): boolean {
  return trigger === 'schedule' || (trigger ?? '').startsWith('schedule:')
}

export const RUN_BY_FILTERS: { text: string; value: RunByKind }[] = [
  { text: 'User', value: 'user' },
  { text: 'Agent', value: 'agent' },
  { text: 'Cron', value: 'cron' },
  { text: 'Daemon', value: 'daemon' },
  { text: 'Run now', value: 'run-now' },
  { text: 'Host schedule (older)', value: 'host-schedule' },
]
