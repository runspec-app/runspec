import { recordCallStart, recordCallEnd, recordCallError } from '../devbus'

export interface ArgDef {
  name: string
  type: string
  required: boolean
  description?: string
  default?: unknown
  options?: string[]      // populated when type === 'choice'
  range?: [number, number]
  multiple?: boolean
  short?: string          // short flag alias e.g. "-v"
  env?: string | string[] // env var aliases checked before spec default
  deprecated?: string
  autonomy?: string       // per-arg autonomy override
  position?: number       // 1-based positional index
  ui?: string             // form control hint
  hint?: string           // placeholder / hint text for the form field
  inherited?: boolean     // arg inherited from an ancestor (global / parent-command) — set by discovery
}

export interface Runnable {
  name: string
  group: string   // venv directory name — the execution environment identity
  host: string
  description?: string
  args: ArgDef[]
  commands?: Record<string, Runnable>  // subcommands, max two levels deep
  requireCommand?: boolean  // true when a command must be chosen at this level (core enforces require-command)
  autonomy?: string   // effective autonomy — 'confirm' | 'autonomous' | 'supervised' | 'manual'
  runAs?: string      // OS user the runnable executes as on the host
  rawSpec?: Record<string, unknown>  // full raw spec dict for the holistic Specs view
}

export interface DirEntry {
  name: string
  type: 'directory' | 'file'
  size: number
  mtime: number   // epoch seconds
}

export interface DirListing {
  cwd: string
  parent: string | null   // null at a filesystem / drive root
  entries: DirEntry[]
  truncated: boolean
  error?: string
}

// runspec:transfer_progress event detail — one per throttled progress tick (or a
// per-file error when `error` is set).
export interface TransferProgress {
  id: string
  file: string
  index: number
  count: number
  transferred: number
  total: number
  error?: string
  host?: string   // set for fan-out (which target host this tick is for)
}

export interface TransferHostResult {
  host: string
  ok: boolean
  error?: string | null
}

// A relay source: a configured host, or an ad-hoc host reached via a jump. The
// password (ad-hoc only) is used for the single connection and never stored.
export interface RelaySource {
  kind: 'host' | 'adhoc'
  host: string
  user?: string
  port?: number
  jump?: string       // configured host to tunnel through (ad-hoc)
  password?: string   // optional, used only if key/agent auth fails
}

// runspec:transfer_end event detail — fired once when a transfer finishes.
export interface TransferEnd {
  id: string
  ok: boolean
  files: number   // files completed
  total: number
  error?: string | null
  hosts?: TransferHostResult[]   // per-host results for a fan-out
}

export interface Host {
  name: string
  connected: boolean
  runnableCount: number
  groups: string[]
  role?: 'primary' | 'secondary'  // undefined = local machine, always included
  group?: string                  // sidebar display group label, joined from JumpHost config
}

export interface JumpHost {
  name: string        // identifier — displayed and used as SSH host alias
  hostname: string
  runspec_paths?: string[]  // paths to runspec binaries (one per venv) on the remote host
  user?: string       // falls back to SSH default in settings
  port?: number       // falls back to SSH default (22)
  identityFile?: string
  group?: string      // sidebar display group label (e.g. "Production", "Staging")
}

export interface HistoryLogLine {
  ts: string
  level: string
  message: string
}

export interface HistoryRecord {
  id: string
  runnable: string
  group: string
  host: string
  operator: string   // who triggered the run (console user or "Scheduled Task")
  runAs: string      // OS user the runnable executed as on the host (from run_as in spec)
  exitCode: number
  durationMs: number
  ts: string
  args: Record<string, unknown>      // arguments used for this invocation (from run_summary)
  argSources?: Record<string, string> // provenance of each arg: "cli" | "env" | "runspec_env" | "spec_default" | "not_set"
  logLines: HistoryLogLine[]         // log records belonging to this invocation
  initiatedBy?: 'llm' | 'user'      // 'llm' when triggered by the agentic chat loop
}

export interface Schedule {
  id: string
  runnable: string
  host: string
  schedule: string
  args?: Record<string, unknown>
  nextRun?: string
}

export interface InFlightRecord {
  id: string
  runnable: string
  group: string
  host: string
  operator: string
  runAs: string
  startedAt: string
  args: Record<string, unknown>
}

export interface TodayByRunnable {
  runnable: string
  host: string
  count: number
  lastExitCode: number
  lastRun: string
}

export interface TodayUpcoming {
  scheduleId: string
  runnable: string
  host: string
  nextRun: string
}

export interface TodaySummary {
  date: string           // YYYY-MM-DD
  generatedAt: string    // ISO timestamp of last digest run
  totalRuns: number
  successCount: number
  failureCount: number
  byRunnable: TodayByRunnable[]
  upcomingToday: TodayUpcoming[]
}

export interface AnalyticsDaily {
  date: string          // YYYY-MM-DD
  total: number
  success: number
  failure: number
  p50DurationMs: number
  p95DurationMs: number
}

export interface AnalyticsBucket {
  date: string
  host: string
  group: string
  runnable: string
  operator: string
  initiatedBy: 'user' | 'llm'
  autonomy: string
  total: number
  success: number
  failure: number
  durationMsSum: number
  events: Record<string, number>   // per-level log counts (DEBUG…CRITICAL)
}

export interface AnalyticsException {
  exception: string
  runnable: string
  host: string
  count: number
  lastTs: string
}

export interface AnalyticsDimensions {
  hosts: string[]
  groups: string[]
  runnables: string[]
  operators: string[]
  initiatedBy: string[]
}

export interface AnalyticsDurationStats {
  p50: number
  p95: number
  max: number
  mean: number
}

export interface AnalyticsData {
  since: string          // YYYY-MM-DD inclusive lower bound (UTC)
  until: string          // YYYY-MM-DD upper bound (today, UTC)
  totalRuns: number
  successCount: number
  failureCount: number
  durationMs: AnalyticsDurationStats
  daily: AnalyticsDaily[]
  buckets: AnalyticsBucket[]       // pre-aggregated drill grain — filtered client-side
  exceptions: AnalyticsException[]
  dimensions: AnalyticsDimensions
  partial: boolean                 // true if any host scan failed/timed out
  errors: { host: string; message: string }[]
}

// ── logs management (Logs tab) ──────────────────────────────────────────────
// One uniform shape per `runspec logs <verb> --json`, driven per venv.

export interface LogStatusRunnable {
  runnable: string
  per_run_files: number
  archives: number
  total_bytes: number
  oldest: string | null
  newest: string | null
}

export interface LogStatusVenv {
  group: string                    // venv directory name
  ok: boolean
  error?: string                   // set when ok === false
  dirs?: string[]
  runnables?: LogStatusRunnable[]
  total_bytes?: number
  total_files?: number
}

export interface LogPruneResult {
  group: string
  ok: boolean
  error?: string
  dry_run?: boolean
  count?: number
  freed_bytes?: number
  deleted?: { path: string; bytes: number }[]
}

export interface LogCompactResult {
  group: string
  ok: boolean
  error?: string
  dry_run?: boolean
  compacted?: number
  archives?: { archive: string; count: number; sources: string[] }[]
}

export interface LogViewResult {
  ok: boolean
  error?: string
  records: Record<string, unknown>[]   // parsed JSON log lines, timestamp-sorted
}

export interface RotateHostResult {
  host: string
  pushed: boolean
  verified: boolean
  skipped: boolean
  error: string
}

export interface TestResult {
  connected: boolean
  runspec_ok: boolean
  runnable_count: number
  stdout: string
  stderr: string
  exit_code: number
}

export interface BridgeApi {
  get_hosts: () => Promise<Host[]>
  get_runnables: (host: string) => Promise<Runnable[]>
  get_history: (host: string, runnable?: string) => Promise<HistoryRecord[]>
  get_schedules: () => Promise<Schedule[]>
  get_config: () => Promise<Record<string, unknown>>
  config_dir: () => Promise<string>
  // ── version / docs ─────────────────────────────────────────────────────────
  app_version: () => Promise<string>
  check_for_update: () => Promise<{ ok: boolean; current: string; latest: string | null; update_available: boolean }>
  docs_url: () => Promise<string>
  open_docs: () => Promise<void>
  list_providers: () => Promise<{ value: string; label: string }[]>
  save_config: (data: Record<string, unknown>) => Promise<void>
  create_schedule: (data: Record<string, unknown>) => Promise<void>
  delete_schedule: (id: string) => Promise<void>
  get_jump_hosts: () => Promise<JumpHost[]>
  save_jump_hosts: (hosts: JumpHost[]) => Promise<void>
  import_jump_hosts: (tomlContent: string) => Promise<JumpHost[]>
  test_host: (name: string) => Promise<TestResult>
  invoke_runnable: (host: string, runnable: string, args: Record<string, unknown>, commandPath?: string[], group?: string) => Promise<string>
  // List a directory on a host for the Forms path-picker. With asServiceAccount +
  // a runnable, the remote listing runs as that runnable's run_as (sudo/become).
  list_dir: (host: string, path?: string, group?: string, dirsOnly?: boolean, runnable?: string, asServiceAccount?: boolean) => Promise<DirListing>
  cancel_invocation: (invId: string) => Promise<void>
  // Copy files between the local machine and a remote host over SFTP (local↔remote
  // only). Returns an id, then streams runspec:transfer_progress / transfer_end.
  start_transfer: (sourceHost: string, sourcePaths: string[], destHost: string, destDir: string) => Promise<{ id?: string; error?: string }>
  // Fan-out: upload the same local source file(s) to dest_dir on many remote hosts.
  start_fanout: (sourcePaths: string[], destHosts: string[], destDir: string) => Promise<{ id?: string; error?: string }>
  // Relay: stream file(s) from one host to another through the console. Source is a
  // configured host, or an ad-hoc host reached via a configured jump (key first,
  // then an optional password — used once, never stored/logged).
  start_relay: (source: RelaySource, sourcePaths: string[], destHost: string, destDir: string) => Promise<{ id?: string; error?: string }>
  // Browse a directory on an ad-hoc relay source (reached via a jump). Password
  // is used only for this listing connection, never stored.
  list_dir_adhoc: (jump: string, host: string, path?: string, user?: string, password?: string, port?: number, dirsOnly?: boolean) => Promise<DirListing>
  // Close cached ad-hoc browse connections (call when the relay dialog closes).
  close_adhoc: () => Promise<void>
  // Pre-flight: does `path` exist as a directory on each host? (fan-out dest check)
  check_dir_exists: (hosts: string[], path: string) => Promise<Record<string, { exists: boolean; error?: string | null }>>
  cancel_transfer: (id: string) => Promise<void>
  send_chat: (message: string, invocationId?: string) => Promise<string>
  cancel_chat: (chatId: string) => Promise<void>
  clear_chat: () => Promise<void>
  resolve_tool_confirmation: (requestId: string, approved: boolean) => Promise<void>
  get_in_flight: () => Promise<InFlightRecord[]>
  get_today: (host: string, group: string) => Promise<TodaySummary | null>
  get_analytics: (hosts: string[], sinceDays?: number, runnable?: string) => Promise<AnalyticsData>
  // ── logs management ──────────────────────────────────────────────────────
  logs_status: (host: string, runnable?: string) => Promise<LogStatusVenv[]>
  logs_view: (host: string, group: string, runnable: string, since?: string, user?: string, run?: string) => Promise<LogViewResult>
  logs_prune: (host: string, group: string, runnable?: string, olderThan?: string, maxFiles?: number, maxTotalSize?: string, dryRun?: boolean) => Promise<LogPruneResult>
  logs_compact: (host: string, group: string, runnable?: string, olderThan?: string, gzip?: boolean, dryRun?: boolean) => Promise<LogCompactResult>
  generate_ssh_key: () => Promise<{ ok: boolean; public_key: string; key_path: string; committed: boolean; per_host: RotateHostResult[]; message: string }>
  rotate_ssh_key: () => Promise<{ ok: boolean; committed: boolean; public_key: string; key_path: string; per_host: RotateHostResult[]; message: string }>
  get_public_key: () => Promise<{ ok: boolean; public_key: string; message: string }>
  copy_ssh_id: (host: string, password: string) => Promise<{ ok: boolean; connected: boolean; message: string }>
  puttygen_path: () => Promise<string>
  launch_puttygen: () => Promise<void>
  open_putty_url: (url: string) => Promise<void>
  browse_ssh_binary: () => Promise<string>
  minimize_window: () => Promise<void>
  toggle_maximize_window: () => Promise<void>
  close_window: () => Promise<void>
  resize_window: (width: number, height: number) => Promise<void>
  move_window: (x: number, y: number) => Promise<void>
  // ── terminal sessions ──────────────────────────────────────────────────────
  launch_terminal: (host: string) => Promise<void>
  launch_local_terminal: () => Promise<void>
  // ── dev tools ────────────────────────────────────────────────────────────
  is_debug_enabled: () => Promise<boolean>
  open_devtools: () => Promise<void>
}

declare global {
  interface Window {
    pywebview?: { api: BridgeApi }
  }
}

// Use real pywebview bridge in the desktop app, mock in a plain browser.
// pywebview injects window.pywebview.api asynchronously — wait for the
// 'pywebviewready' event with a short timeout to fall back to mock when
// running directly in a browser (no pywebview present).
let _bridge: Promise<BridgeApi> | null = null

function resolveBridge(): Promise<BridgeApi> {
  if (_bridge) return _bridge
  _bridge = new Promise<BridgeApi>(resolve => {
    if (window.pywebview?.api) {
      resolve(window.pywebview.api)
      return
    }
    let settled = false
    const settle = (api: BridgeApi) => { if (!settled) { settled = true; resolve(api) } }

    window.addEventListener('pywebviewready', () => settle(window.pywebview!.api), { once: true })

    // Poll — pywebview may inject the api without firing the event on some platforms
    const poll = setInterval(() => {
      if (window.pywebview?.api) { clearInterval(poll); settle(window.pywebview.api) }
    }, 50)

    // After 2500 ms with no pywebview, we're in a plain browser — use mock
    setTimeout(async () => {
      clearInterval(poll)
      if (!settled) {
        settled = true
        const { mockApi } = await import('./mock')
        resolve(mockApi)
      }
    }, 2500)
  })
  return _bridge
}

export const bridge: BridgeApi = new Proxy({} as BridgeApi, {
  get(_target, prop) {
    // Symbol probes (thenable checks, etc.) must not be mistaken for methods —
    // returning a function here would make the Proxy look like a Promise.
    if (typeof prop === 'symbol') return undefined
    const name = String(prop)
    return async (...args: unknown[]) => {
      const entryId = recordCallStart(name, args)
      const t0 = performance.now()
      try {
        const api = await resolveBridge()
        const result = await (api[prop as keyof BridgeApi] as (...a: unknown[]) => unknown)(...args)
        recordCallEnd(entryId, result, performance.now() - t0)
        return result
      } catch (err) {
        recordCallError(entryId, err, performance.now() - t0)
        throw err
      }
    }
  },
})
