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
  // Set when a source-side filter is running: output size is unknown up front, so
  // `transferred` is bytes written (a counter) and `total` is only the input size.
  indeterminate?: boolean
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
  credential_id?: string  // optional, a saved credential whose secret is the password
}

// A named, reusable source-side stream filter (Settings → Filters). The pipeline
// runs on the file's host (file fed on stdin), so only matching output crosses
// the network. `{{arg}}` in the pipeline is a runtime value the dialog prompts
// for (labelled by `param_label`); `description` is free-text metadata.
export interface Filter {
  name: string
  pipeline: string
  description?: string
  param_label?: string
}

// A named credential (Settings → Credentials). Metadata only on the wire — the
// secret value lives in the OS keyring and is never read back; `has_secret` says
// whether one is stored. `secret` is write-only: send it to save/update a value,
// omit/blank it to leave the stored value untouched. `username_arg`/`secret_arg`
// bind the values to a runnable's flags; the scopes (fnmatch patterns) limit
// where the credential may be applied.
export interface Credential {
  id: string
  label: string
  kind: 'userpass' | 'password' | 'token'
  username?: string
  username_arg?: string
  secret_arg?: string
  scope_hosts?: string[]
  scope_runnables?: string[]
  has_secret?: boolean
  secret?: string   // write-only; never returned by list_credentials
}

export interface CredentialBackend {
  available: boolean
  backend: string
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
  room?: string       // chat room hosted on this host (runspec-room) — opt-in
  room_port?: number  // the room server's localhost port on this host
  room_user?: string  // display name in the room (defaults to the SSH login)
  room_via?: string   // "exec" to join over an SSH exec channel (no port-forward)
  room_exec_command?: string  // exec-mode relay command on the host (e.g. socat / console-room connect)
  room_queue?: string  // helpdesk queue this console staffs on the host's room server (agent mode)
  room_catalog?: string[]  // optional per-queue catalog allowlist (bare self-service runnable names)
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

// ── event triggers (Triggers tab) ───────────────────────────────────────────

// runspec:trigger_card event detail — one kanban card (an event instance flowing
// across the board). Columns: triggered | triage | awaiting_human | running |
// done | failed.
export interface TimelineEntry {
  ts: number      // epoch seconds
  text: string
}

export interface TriggerCard {
  card_id: string
  trigger: string
  source: string
  event_type: string
  summary: string
  column: string
  detail: Record<string, unknown>
  created_at: number
  timeline?: TimelineEntry[]   // timestamped audit trail of stage changes + steps
}

// A parked autonomy-gate confirm — the runspec:tool_confirm event's shape, also
// returned by get_pending_confirms for cold-start. `id` is the chat/card id; a
// confirm whose id matches a live trigger card belongs to that card (the work
// inbox folds it there, the same id-match the Triggers tab badge uses).
export interface PendingConfirm {
  id: string
  request_id: string
  tool_name: string
  tool_input: Record<string, unknown>
  autonomy: string
  created_at?: number
}

// runspec:trigger_event detail — every event the engine saw, with its
// disposition: which triggers fired (matched), which matched but were gated by
// the rota (skipped), or neither (no match).
export interface TriggerEvent {
  id: string
  source: string
  type: string
  summary: string
  matched: string[]
  skipped: string[]
  ts: number
}

// One weekday of the working-hours rota.
export interface RotaDay {
  on?: boolean
  start?: string   // "HH:MM"
  end?: string     // "HH:MM"
}

// The working-hours rota. `source` picks the authority: "console" (the manual
// grid of RotaDay per mon..sun) or "outlook" (calendar shift-marker events).
export interface Rota {
  enabled?: boolean
  source?: string                 // "console" | "outlook"
  shift_marker?: string           // outlook: subject keyword marking a shift
  hold_during_meetings?: boolean  // outlook: also pause while in a meeting/OOO
  [day: string]: boolean | string | RotaDay | undefined
}

export interface TriggerSourceInfo {
  id: string
  available: boolean
  reason: string
  event_types: string[]
}

export interface TriggerListResult {
  triggers: Record<string, unknown>[]   // raw [triggers.<name>] tables from config
  errors: string[]                        // per-table parse errors (skipped)
  sources: TriggerSourceInfo[]
  rota: Rota                              // working-hours schedule ({} when unset)
  running: boolean
}


// ── chat room (Rooms view) ───────────────────────────────────────────────────
// The room is a "dumb message pipe with presence" the console connects to over
// the SSH tunnel; the bridge fans its events to the UI as runspec:room_* events.

// A helpdesk conversation's lifecycle (the room server's `conversation` frames).
export interface ThreadState {
  state: 'open' | 'assigned' | 'resolved' | 'closed' | string
  assignee: string | null
}

// One configured room (declared on a fleet host entry) + its live state.
// `queue` marks a helpdesk agent entry (room_queue on the host): the console is
// subscribed to that queue's namespace and `room` is the namespace key
// (helpdesk/<queue>); `threads` seeds the inbox (conversation room → state).
export interface RoomInfo {
  room: string         // the room name (or queue namespace in agent mode)
  host: string         // the fleet host it's hosted on
  user: string         // this console's display name in the room
  connected: boolean   // currently joined (tunnel up)
  queue: string        // bare queue name in agent mode; '' for group chat
  threads?: Record<string, ThreadState>
}

// All configured rooms (bridge.get_room_state). Empty when none are configured.
export interface RoomsState {
  rooms: RoomInfo[]
}

// bridge.test_room_connection — an isolated, on-demand diagnosis of a room's
// connection over the real paramiko path (proxy/key/channel). `stage` localises
// a failure: ssh (reach/auth the host), channel (tunnel/exec relay), room (relay
// up but room server silent), config, or ok.
export interface RoomConnTest {
  ok: boolean
  stage: 'ssh' | 'channel' | 'room' | 'config' | 'ok'
  message: string
  hint?: string | null
  host?: string
  room?: string
  queue?: string
}

// runspec:room_connection — a room's live connection flipped (connect / drop).
export interface RoomConnection {
  room: string
  host: string
  connected: boolean
  queue?: string
}

// runspec:room_message — a sent message (server-stamped id/ts/sender). A
// visitor's catalog submission arrives as kind="request" with the machine
// payload in `data`; plain chat is kind="chat" (older servers send neither).
export interface RoomMessage {
  room: string
  sender: string
  text: string
  id: string
  ts: number
  kind?: string
  data?: { tool: string; args: Record<string, unknown> }
}

// runspec:room_conversation — a helpdesk thread's lifecycle state changed
// (open on first visitor message, assigned on claim, resolved/closed/reopened).
export interface RoomConversation {
  room: string
  state: string
  assignee: string | null
}

// runspec:room_presence — a participant's live presence in a room.
export interface RoomPresence {
  room: string
  user: string
  status: 'available' | 'away'
}

// runspec:room_draft — an agent-proposed reply pre-filling the composer (the
// "speak" checkpoint: the human edits and sends; the agent never sends).
export interface RoomDraft {
  id: string
  room: string
  text: string
}


export interface BridgeApi {
  get_hosts: () => Promise<Host[]>
  get_runnables: (host: string) => Promise<Runnable[]>
  get_history: (host: string, runnable?: string) => Promise<HistoryRecord[]>
  // History across every host (local + connected remotes) — the History tab uses
  // this so a run shows up regardless of which host it ran on.
  get_history_all: () => Promise<HistoryRecord[]>
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
  // Named source-side stream filters (Settings → Filters), selectable by name in
  // the Relay and Download dialogs.
  get_filters: () => Promise<Filter[]>
  save_filters: (filters: Filter[]) => Promise<void>
  import_filters: (tomlContent: string) => Promise<Filter[]>
  // Named credentials (Settings → Credentials). Metadata is returned by
  // list_credentials; the secret value rides only on save (write-only) and is
  // stored in the OS keyring, never read back.
  list_credentials: () => Promise<Credential[]>
  // In-scope credentials for a runnable (id + label) — for the Forms run dialog's
  // optional credential override. In-scope credentials auto-apply on run anyway.
  credentials_for_runnable: (host: string, runnable: string) => Promise<{ id: string; label: string; has_secret: boolean }[]>
  save_credential: (entry: Credential) => Promise<{ id?: string; error?: string }>
  delete_credential: (id: string) => Promise<void>
  credentials_backend: () => Promise<CredentialBackend>
  test_host: (name: string) => Promise<TestResult>
  // `credentialId` (optional) applies a saved credential by id, overriding any
  // in-scope credential that would auto-apply. In-scope credentials fill their
  // bound args automatically even without it.
  invoke_runnable: (host: string, runnable: string, args: Record<string, unknown>, commandPath?: string[], group?: string, credentialId?: string) => Promise<string>
  // List a directory on a host for the Forms path-picker. With asServiceAccount +
  // a runnable, the remote listing runs as that runnable's run_as (sudo/become).
  list_dir: (host: string, path?: string, group?: string, dirsOnly?: boolean, runnable?: string, asServiceAccount?: boolean) => Promise<DirListing>
  cancel_invocation: (invId: string) => Promise<void>
  // Copy files between the local machine and a remote host over SFTP (local↔remote
  // only). Returns an id, then streams runspec:transfer_progress / transfer_end.
  // filterName/filterArg select a saved source-side filter; they apply only to a
  // download (pull) — the remote file is filtered at the source, output written local.
  start_transfer: (sourceHost: string, sourcePaths: string[], destHost: string, destDir: string, filterName?: string, filterArg?: string) => Promise<{ id?: string; error?: string }>
  // Fan-out: upload the same local source file(s) to dest_dir on many remote hosts.
  // filterName/filterArg apply a saved filter locally (once) before fanning out.
  start_fanout: (sourcePaths: string[], destHosts: string[], destDir: string, filterName?: string, filterArg?: string) => Promise<{ id?: string; error?: string }>
  // Relay: stream file(s) from one host to another through the console. Source is a
  // configured host, or an ad-hoc host reached via a configured jump (key first,
  // then an optional password — used once, never stored/logged).
  // filterName/filterArg select a saved source-side filter run on the source host,
  // so only matching output is relayed (handy for pulling a few lines from a huge log).
  start_relay: (source: RelaySource, sourcePaths: string[], destHost: string, destDir: string, filterName?: string, filterArg?: string) => Promise<{ id?: string; error?: string }>
  // Browse a directory on an ad-hoc relay source (reached via a jump). Password
  // is used only for this listing connection, never stored.
  list_dir_adhoc: (jump: string, host: string, path?: string, user?: string, password?: string, port?: number, dirsOnly?: boolean, credentialId?: string) => Promise<DirListing>
  // Browse a directory on a host that isn't saved yet (Settings → Jump Hosts add/
  // edit form) so the runspec-path picker can pick where runspec lives. Connects
  // straight to the host from the form's connection details (no jump).
  list_dir_unsaved: (hostname: string, path?: string, user?: string, port?: number, identityFile?: string, dirsOnly?: boolean) => Promise<DirListing>
  // Close cached ad-hoc browse connections (call when the relay dialog closes).
  close_adhoc: () => Promise<void>
  // Pre-flight: does `path` exist as a directory on each host? (fan-out dest check)
  check_dir_exists: (hosts: string[], path: string) => Promise<Record<string, { exists: boolean; error?: string | null }>>
  cancel_transfer: (id: string) => Promise<void>
  send_chat: (message: string, invocationId?: string) => Promise<string>
  cancel_chat: (chatId: string) => Promise<void>
  clear_chat: () => Promise<void>
  resolve_tool_confirmation: (requestId: string, approved: boolean) => Promise<void>
  // ── event triggers ─────────────────────────────────────────────────────────
  list_triggers: () => Promise<TriggerListResult>
  set_trigger_enabled: (name: string, enabled: boolean) => Promise<{ ok: boolean }>
  save_rota: (rota: Rota) => Promise<{ ok: boolean }>
  get_trigger_board: () => Promise<TriggerCard[]>
  get_trigger_events: () => Promise<TriggerEvent[]>
  // Cold-start snapshot for the work inbox: autonomy-gate confirms parked before
  // the view mounted (the live path only emits runspec:tool_confirm for later ones).
  get_pending_confirms: () => Promise<PendingConfirm[]>
  // ─────────────────────────────────────────────────────────────────────────
  // Send a draft an agent-mode trigger saved (the card's Send button).
  send_trigger_draft: (entryId: string) => Promise<{ ok: boolean; to?: string; subject?: string; error?: string }>
  // ── chat room (Rooms view) ─────────────────────────────────────────────────
  get_room_state: () => Promise<RoomsState>
  // Diagnose a room/queue's connection on demand (the real paramiko path), so
  // the operator sees the actual blocker instead of a buried traceback.
  test_room_connection: (host: string) => Promise<RoomConnTest>
  // The composer's one outbound path. The agent never reaches this — a proposed
  // reply only pre-fills the composer (runspec:room_draft); the human sends.
  // For a helpdesk thread, pass the conversation room (helpdesk/<queue>/<visitor>);
  // the bridge routes it via the agent connection staffing that queue.
  send_room_message: (room: string, text: string) => Promise<{ ok: boolean; error?: string }>
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
