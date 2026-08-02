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

// A user-invocable, parameterized runbook (the `$` runbooks menu + Runbooks settings
// tab). Its `args` are a real runspec `[runbook.args]` table — parsed server-side to
// the same ArgDef shape a runnable form uses, so the run modal reuses ArgInput.
// One step of a runbook: free prose the agent follows (with {{name}} placeholders).
// Only text + optional title — a step never carries a machine condition/goto field
// (that would cross the "no workflow engine" cliff); skip/branch logic lives in the
// prose and is the agent's judgement.
export interface RunbookStep {
  title?: string
  text: string
}

export interface Runbook {
  id: string
  title: string
  symptom: string          // "when to use" — shown in the menu + editor
  steps: RunbookStep[]     // ordered steps; rendered to numbered prose at run time
  tags: string[]
  updated?: string
  args: ArgDef[]           // parsed param defs — the run modal renders these
  argsRaw?: Record<string, unknown>  // stored [runbook.args] table (faithful editor round-trip)
  argsError?: string       // set when a malformed args table failed to parse
}

// A saved run — a deterministic preset (runnable + target + fixed args + open
// params) stored in synced runspec_saved_runs.toml. The Forms tab lists them,
// the run modal renders only the open params, and schedules can fire them.
export interface SavedRun {
  id: string
  title: string
  description: string
  runnable: string
  command: string[]        // optional subcommand path
  // Exactly one of host / group / hosts / selected_hosts is set.
  host: string
  group: string
  hosts: string[]          // failover candidates — first connected wins
  selected_hosts: string[] // explicit fan-out list
  args: Record<string, unknown>       // fixed args, baked in
  credential_ids: string[]
  target?: string          // human summary of the targeting, computed bridge-side
  params: ArgDef[]         // parsed open-param defs — the run modal renders these
  paramsRaw?: Record<string, unknown> // stored params table (faithful editor round-trip)
  argsError?: string       // set when the entry failed to validate/parse
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

// An ad-hoc transfer endpoint: an SSH host not in the fleet, reached via a
// configured jump — the same shape a RelaySource ad-hoc source uses. A transfer
// side is 'local', a configured host name, or this descriptor.
export interface AdHocEndpoint {
  kind: 'adhoc'
  host: string
  jump: string
  user?: string
  port?: number
  password?: string
  credential_id?: string
}
export type TransferEndpoint = string | AdHocEndpoint

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
  kind: 'userpass' | 'password' | 'token' | 'ssh-key'
  username?: string
  username_arg?: string
  secret_arg?: string
  private_key?: string   // ssh-key: path to the private key file
  public_key?: string    // ssh-key: path to the public key file (optional)
  scope_hosts?: string[]
  scope_runnables?: string[]
  has_secret?: boolean   // ssh-key: a key passphrase is stored
  secret?: string   // write-only; never returned by list_credentials
}

export interface CredentialBackend {
  available: boolean
  backend: string
}

// Settings → MCP. An external MCP server the chat agent connects to. `credential`
// is a Credentials-tab id (keychain-backed; only the id is stored). `proxy` is a
// per-server HTTP CONNECT proxy (servers can live on different networks).
export interface McpServer {
  name: string
  transport: 'stdio' | 'http' | 'sse'
  command?: string
  args?: string[]
  env?: Record<string, string>
  cwd?: string
  url?: string
  headers?: Record<string, string>
  enabled?: boolean
  autonomy?: '' | 'autonomous' | 'confirm' | 'manual'
  credential?: string
  credential_header?: string
  proxy?: string
}
export interface McpConfig {
  enabled: boolean
  call_timeout: number
  connect_timeout: number
  servers: McpServer[]
}
export interface McpServerStatus {
  name: string
  transport: string
  connected: boolean
  tool_count: number
  error: string
  credential: string
  proxy: string
}
export interface McpStatus {
  available: boolean
  reason?: string
  started?: boolean
  tool_count: number
  servers: McpServerStatus[]
}
export interface McpCredentialOption {
  id: string
  label: string
  kind: string
}

// Settings → Config Sync. Where to pull the console's config files from (a git
// repo, at a tag/ref), which credential authenticates, and the last successful
// pull. The secret is never here — it lives in the keyring under credential_id.
// `engine` is the active git engine (null = the `git-sync` extra isn't installed);
// `files_default` is the file set the UI offers when none are chosen. `preserve`
// is the user's keep-local paths ("<file>:<dotted.path>"); `preserve_default` is
// the built-in set always applied on top (mailbox.folder, *.room_user).
export interface SyncConfig {
  url: string
  ref: string
  credential_id: string
  subpath: string
  layers: string[]
  ca_bundle: string
  tls_verify: boolean
  files: string[]
  preserve: string[]
  backups_keep: number
  last_ref: string
  last_commit: string
  last_synced: string
  engine: string | null
  files_default: string[]
  preserve_default: string[]
}

export interface SyncPullResult {
  ok: boolean
  ref?: string
  commit?: string
  written?: string[]
  skipped?: string[]
  backups?: string[]
  pruned?: string[]
  error?: string
}

// Settings → Config Sync → Config seed. Read-only status of the baseline config
// bundled inside the deployment package. `stamp`/`applied` map distribution name
// → version; `pending` is true when what's bundled differs from what's applied
// (a re-assert will run on next launch, or via the Re-apply button now).
// One seeded leaf key whose current *local* value differs from the seed's — what
// a re-assert (or the next package upgrade) would silently revert. `path` is the
// dotted key (mapping files) or `<id>.<field>` (collection files); `preserved` is
// true when a `preserve` rule pins it local, so a re-assert would NOT override it.
export interface SeedOverride {
  file: string
  path: string
  seed: string
  local: string
  preserved: boolean
}

export interface SeedStatus {
  available: boolean
  stamp: Record<string, string>
  applied: Record<string, string>
  last_applied: string
  pending: boolean
  overrides?: SeedOverride[]
}

// Validated white-label theme (`[console.theme]`). Every field optional; an
// empty object means no theme (the default runspec look). `accent_color` is a
// hex colour → Ant `colorPrimary`; `watermark_logo` is an http(s)/data:image URL.
export interface BrandTheme {
  accent_color?: string
  brand_name?: string
  watermark_text?: string
  watermark_logo?: string
  watermark_style?: 'tiled' | 'banner' | 'center'  // default 'tiled'
}

export interface SeedApplyResult {
  ok: boolean
  applied?: boolean
  reason?: string
  stamp?: Record<string, string>
  written?: string[]
  skipped?: string[]
  backups?: string[]
  pruned?: string[]
  error?: string
}

// Settings → Config Sync → Runbook docs. Where the deterministic runbook wiki is
// written (blank = the `default`, app_dir()/docs).
export interface DocsConfig {
  dir: string
  default: string
}

export interface DocsGenResult {
  ok: boolean
  dir?: string
  written?: string[]
  error?: string
}

// The single [mailbox] the Outlook source monitors (Settings → Triggers).
// `address` blank = the default account; `folder` blank = the Inbox; `label` is
// an optional display name. null = no mailbox configured (monitoring off).
// See docs/design/teams-and-email-monitoring.md.
export interface Mailbox {
  address: string
  folder: string
  label: string
  // Mailbox-wide default for suppressing replies (once per thread). A trigger
  // inherits this unless it sets its own override. Default false.
  once_per_thread?: boolean
}

// An additional watch-only mailbox/folder the Outlook source monitors, on top of
// the primary [mailbox] — e.g. the operator's own Inbox alongside a shared
// helpdesk mailbox. Same shape minus once_per_thread: these feed new_mail
// triggers only and carry no room/verify-from role, so reply suppression is left
// to each trigger's own once_per_thread. See read_watches() in config.py.
export interface Watch {
  address: string
  folder: string
  label: string
}

// A customer team (department) a visitor picks at sign-up. Lives in the helpdesk
// room DB, managed from the console (Settings → Teams). `slug` is the stable id.
export interface Team {
  slug: string
  name: string
}

// runspec:teams event detail — the room's reply to list/add/remove_team.
export interface TeamsEvent {
  teams: Team[]
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
  profiles?: string[]             // host-profile (working-set) tags this host belongs to
}

// An entry in the canonical host-group registry (runspec_groups.toml). The
// registry pins the legal group NAMES (shared via Config Sync); membership lives
// on each host's `group` field.
export interface HostGroup {
  name: string
  description?: string
}

// An entry in the canonical host-profile registry (runspec_profiles.toml). A
// profile is a named host working-set (which remotes are in scope / connected).
// The registry pins the legal profile NAMES (shared via Config Sync); membership
// lives on each host's `profiles` array; the *active* profile is machine-local.
// See docs/design/host-profiles.md.
export interface HostProfile {
  name: string
  description?: string
}

// A runnable designated as self-service (Settings → Self-Service,
// runspec_self_service.toml). The registry names which discovered runnables are
// offered to visitors in the helpdesk room — replacing the runspec.toml
// `discoverable = ["self-service"]` marking. Identity key is `name` (synced).
export interface SelfServiceEntry {
  name: string
  description?: string
}

// A git repo the agent may read and locally edit (Settings → Repos,
// runspec_repos.toml). The registry is the agent's allowlist — only repos listed
// here are reachable by the repo_* tools. `checked_out`/`commit` are status fields
// returned by get_repos (the local working-copy state); omit them on save.
export interface Repo {
  name: string
  url: string
  ref: string
  credential_id?: string
  description?: string
  path?: string          // machine-local custom checkout dir ("" / absent = default AppData location)
  checkout_dir?: string  // effective absolute checkout dir (read-only; from get_repos)
  checked_out?: boolean
  commit?: string
}

export interface RepoSyncResult {
  ok: boolean
  action?: string   // "cloned" | "updated"
  commit?: string   // short HEAD sha after the sync
  error?: string
}

export interface JumpHost {
  name: string        // identifier — displayed and used as SSH host alias
  hostname: string
  runspec_paths?: string[]  // paths to runspec binaries (one per venv) on the remote host
  user?: string       // falls back to SSH default in settings
  port?: number       // falls back to SSH default (22)
  identityFile?: string
  group?: string      // sidebar display group label (e.g. "Production", "Staging")
  jump?: string       // reach this host via system-ssh ProxyJump (-J) so ~/.ssh/config legacy algorithms apply (CentOS 6 ssh-rsa/ssh-dss)
  profiles?: string[] // host-profile (working-set) tags — which profiles load this host
  room?: string       // chat room hosted on this host (runspec-room) — opt-in
  room_port?: number  // the room server's localhost port on this host
  room_user?: string  // display name in the room (defaults to the SSH login)
  room_via?: string   // "exec" to join over an SSH exec channel (no port-forward)
  room_exec_command?: string  // exec-mode relay command on the host (e.g. socat / console-room connect)
  room_queue?: string  // helpdesk queue this console staffs on the host's room server (agent mode)
  room_catalog?: string[]  // optional per-queue catalog allowlist (bare self-service runnable names)
}

// The local machine's venv auto-discovery config (Settings → Local venvs). The
// `globs` convention lives in config.toml so it rides Config Sync / the config
// seed (one versioned glob works fleet-wide); `resolved` is what those globs
// actually match on THIS machine (machine-local), with the console's own venv
// (`own_venv`) always first.
export interface LocalVenvConfig {
  globs: string[]
  own_venv: string
  resolved: { path: string; venv_name: string }[]
}

export interface HistoryLogLine {
  ts: string
  level: string
  message: string
}

export interface HistoryRecord {
  id: string
  runId?: string     // raw run_id (id is a hash of run_id+runnable) — drives the "View full log" drill
  runnable: string
  group: string      // venv the run's logs live in — needed to resolve logs_view; "" when unknown (some remote)
  host: string
  operator: string   // who triggered the run (console user or "Scheduled Task")
  runAs: string      // OS user the runnable executed as on the host (from run_as in spec)
  exitCode: number
  durationMs: number
  ts: string
  args: Record<string, unknown>      // arguments used for this invocation (from run_summary)
  argSources?: Record<string, string> // provenance of each arg: "cli" | "env" | "runspec_env" | "spec_default" | "not_set"
  commandPath?: string[]             // subcommand path ([] for a plain runnable) — drives rerun reconstruction
  logLines: HistoryLogLine[]         // log records belonging to this invocation
  initiatedBy?: 'llm' | 'user'      // 'llm' when triggered by the agentic chat loop
  trigger?: string                   // event source that caused the run ("schedule"/…), "" = interactive/manual
  runbook?: string                     // saved runbook that drove the run (RUNSPEC_RUNBOOK), "" = not a runbook run
}

// A schedule (Schedules tab). When it fires it either emits a matchable event
// (action 'event') that source="schedule" triggers pick up like mail, runs a
// runnable directly (action 'runnable'), runs an agent turn on a fixed prompt
// (action 'prompt'), or runs a saved runbook as an agent turn (action 'runbook').
// Timing lives in `when` (cron or every).
export interface ScheduleWhen {
  cron?: string
  every?: string
  timezone?: string
}
export interface Schedule {
  id: string
  enabled?: boolean
  action: 'runnable' | 'event' | 'prompt' | 'runbook' | 'saved_run'
  when: ScheduleWhen
  // action === 'runnable' — exactly one of host / group / hosts / selected_hosts.
  // group fans the run out across every member of the host group; hosts is a
  // candidate list and the run goes to the first connected one (failover);
  // selected_hosts is an explicit list and the run fans out to every host in it.
  host?: string
  group?: string
  hosts?: string[]
  selected_hosts?: string[]
  runnable?: string
  args?: Record<string, unknown>
  // action === 'runnable' — the subcommand path to descend into (empty/absent =
  // the root command). Mirrors a saved run's `command`.
  command?: string[]
  // action === 'runnable' — ids of console credentials to apply at fire time.
  // Only ids are stored (never secrets); the secret is injected from the keychain
  // when the schedule fires, exactly like the Forms tab's explicit selection.
  credential_ids?: string[]
  // action === 'event'
  event?: string
  fields?: Record<string, string>
  // action === 'prompt' — the instruction the agent turn runs on.
  prompt?: string
  // action === 'runbook' — the saved runbook id + values for its {{placeholders}}.
  runbook?: string
  // Shared with action === 'saved_run' (there it fills the preset's open params).
  values?: Record<string, unknown>
  // action === 'saved_run' — the saved-run (preset) id; targeting lives on the preset.
  saved_run?: string
  nextRun?: string
}

export interface ConversationMeta {
  id: string
  title: string
  created: string
  updated: string
  turn_count: number
  preview: string
}

export interface ConversationTurn {
  // Absent ⇒ a chat exchange; 'operator_run' is an operator-initiated manual
  // runnable run, rendered for the operator but never replayed to the agent.
  kind?: 'chat' | 'operator_run'
  user: string
  assistant: string
  tools: {
    name: string
    input: Record<string, unknown>
    output: string
    // Operator's confirm-gate decision (when the call was gated) + the agent's
    // original args if the operator edited them. Operator-only annotations.
    operator_decision?: 'approved' | 'edited' | 'denied'
    proposed_input?: Record<string, unknown>
  }[]
  usage?: Record<string, number> | null
  model?: string
  ts?: string
  // operator_run fields
  runnable?: string
  host?: string
  args?: Record<string, unknown>
  output?: string
  exit_code?: number
  duration_ms?: number
  operator?: string
}

export interface ChatConversation {
  id: string
  title: string
  created?: string
  updated?: string
  turn_count?: number
  provider?: string
  model?: string
  turns: ConversationTurn[]
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
  trigger?: string                   // event source that caused the run ("schedule"/…), "" = interactive/manual
  kind?: string                      // "builtin_tool" for a built-in agent tool (repo_*, outlook_*, …); absent for a runnable run
  tool?: string                      // the raw built-in tool name (when kind === "builtin_tool")
  chatId?: string                    // the chat turn this built-in tool belongs to
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
  runbook: string                     // saved runbook that drove the run, "" = not a runbook run
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
  runbooks: string[]
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

// runspec:trigger_card event detail — one run card (an event instance moving
// through its run inbox). Lifecycle statuses: triggered | triage | awaiting_human
// | running | done | failed.
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

// A run card in the shared run inbox (RunInbox). Schedule runs reuse the exact
// TriggerCard shape, so one type serves both the Triggers and Schedules inboxes.
export type RunCard = TriggerCard

// A parked autonomy-gate confirm — the runspec:tool_confirm event's shape, also
// returned by get_pending_confirms for cold-start. `id` is the chat/card id; a
// confirm whose id matches a live trigger card belongs to that card (the work
// inbox folds it there, the same id-match the Triggers tab badge uses).
export interface PendingConfirm {
  id: string
  request_id: string
  tool_name: string                     // raw tool name — matches the tool segment's entry.name
  tool_input: Record<string, unknown>
  // Human-readable view of what's being approved (unwrapped runnable + args for
  // the search-mode `run_runnable` wrapper; equal to tool_name/_input otherwise).
  display_name?: string
  display_input?: Record<string, unknown>
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
  lock_suspend?: boolean          // pause reactive triggers while the screen is locked (default on)
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
  break_until?: string | null             // active "Take a break" return time (ISO), or null
  screen_state?: LockState                // current screen-lock state (live presence indicator)
}

// Lock auto-suspend presence state. 'unknown' = the probe couldn't tell (treated
// as locked / fail-closed by the gate when lock_suspend is on).
export type LockState = 'locked' | 'unlocked' | 'unknown'


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
  connected: boolean   // currently joined (per-host accurate; only the active host)
  active: boolean      // the single host this console is currently selected to staff
  queue: string        // bare queue name in agent mode; '' for group chat
  threads?: Record<string, ThreadState>
}

// All configured rooms (bridge.get_room_state). Empty when none are configured.
export interface RoomsState {
  rooms: RoomInfo[]
}

// A room-configured host offered in the single-room selector (bridge.get_room_hosts).
export interface RoomHost {
  host: string
  room: string
  queue: string
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


// Denylist kill switch (Settings → Denylist). `targets` empty ⇒ all hosts;
// `runnable`/`user`/`host` are globs used by action 'add'; `setting` is for
// action 'mode'. Returns the aggregated per-host/per-venv summary string.
export interface DenylistPayload {
  action: 'list' | 'add' | 'all' | 'clear' | 'mode'
  targets?: string[]
  runnable?: string[]
  user?: string[]
  host?: string[]
  reason?: string
  setting?: 'enforce' | 'warn' | 'off'
}

export interface DenylistRule {
  runnables: string[]
  users: string[]
  hosts: string[]
  reason: string
}

// One row per venv on a host, from denylist_status(). When `ok` is false the
// host/venv couldn't be read (see `error`); otherwise the denylist fields apply.
export interface DenylistHostStatus {
  host: string
  venv: string
  ok: boolean
  error?: string
  path?: string
  present?: boolean
  mode?: 'enforce' | 'warn' | 'off'
  disabled?: boolean
  reason?: string
  rules?: DenylistRule[]
}

// Status of the console-managed local Arize Phoenix server (Phoenix tab).
// `available` false (with `reason`) means the `phoenix` extra isn't installed;
// `running`/`url`/`port` describe a live (or adopted) local server.
export interface PhoenixStatus {
  available: boolean
  reason: string
  running: boolean
  url: string
  port: number
}

export interface BridgeApi {
  get_hosts: () => Promise<Host[]>
  // Disable / re-enable runnables across fleet hosts (a kill switch). Edits each
  // host's venv denylist via the core `runspec deny` over SSH.
  manage_denylist: (payload: DenylistPayload) => Promise<string>
  // Read-only structured denylist state per host/venv (Settings → Denylist table).
  denylist_status: (targets?: string[]) => Promise<DenylistHostStatus[]>
  get_runnables: (host: string) => Promise<Runnable[]>
  // Fleet-discovery readiness for the "discovering N/M hosts…" indicator. `ready`
  // flips true after the first full refresh cycle; live updates arrive on the
  // `runspec:discovery_state` window event (read this on mount in case it predates
  // the listener).
  discovery_state: () => Promise<{ ready: boolean; done: number; total: number }>
  get_history: (host: string, runnable?: string) => Promise<HistoryRecord[]>
  // History across every host (local + connected remotes) — the History tab uses
  // this so a run shows up regardless of which host it ran on.
  get_history_all: () => Promise<HistoryRecord[]>
  get_schedules: () => Promise<Schedule[]>
  get_config: () => Promise<Record<string, unknown>>
  config_dir: () => Promise<string>
  // The single monitored mailbox (Settings → Triggers), or null when unset.
  // Saving restarts the trigger engine so the Outlook source re-resolves the
  // watched folder; pass null to stop monitoring.
  get_mailbox: () => Promise<Mailbox | null>
  save_mailbox: (mailbox: Mailbox | null) => Promise<void>
  // Additional watch-only mailboxes monitored on top of the primary [mailbox]
  // (Settings → Mailbox). Saving restarts the trigger engine; pass [] to clear.
  get_watches: () => Promise<Watch[]>
  save_watches: (watches: Watch[]) => Promise<void>
  // ── version / docs ─────────────────────────────────────────────────────────
  app_title: () => Promise<string>
  // Validated [console.theme] for white-label rendering (read-only; set by a
  // config seed or by hand). Empty object = the default look.
  brand_theme: () => Promise<BrandTheme>
  app_version: () => Promise<string>
  check_for_update: () => Promise<{ ok: boolean; current: string; latest: string | null; update_available: boolean }>
  docs_url: () => Promise<string>
  open_docs: () => Promise<void>
  list_providers: () => Promise<{ value: string; label: string }[]>
  save_config: (data: Record<string, unknown>) => Promise<void>
  // Fire a test toast + taskbar flash + confirm sound; reports which channels are
  // available (Settings → Sounds & Notifications). window_found surfaces the
  // flash/focus title-match prerequisite.
  test_notification: () => Promise<{ toast_available: boolean; toast_reason: string; sound_available: boolean; window_found: boolean }>
  // Preview one sound category (Settings row ▶). token plays the row's unsaved
  // selection ("synth:…" / "system:…" / "file:…"); omitted → the saved cue.
  test_sound: (category: string, token?: string | null) => Promise<boolean>
  // Coverage report for a sound-pack folder: which categories it has files for.
  sound_pack_info: (path?: string) => Promise<{ dir: string; exists: boolean; covered: Record<string, string>; categories: string[] }>
  create_schedule: (data: Record<string, unknown>) => Promise<void>
  // Replace a schedule in place (edit dialog, inline enable/disable). Keeps the id.
  update_schedule: (id: string, data: Record<string, unknown>) => Promise<void>
  delete_schedule: (id: string) => Promise<void>
  // Fire a schedule immediately — runs the runnable (returns inv_id) or emits the
  // event (returns event), regardless of the schedule's enabled flag.
  run_schedule_now: (id: string) => Promise<{ action: 'runnable' | 'event' | 'prompt' | 'runbook' | 'saved_run'; inv_id?: string; event?: string; host?: string; runnable?: string }>
  get_jump_hosts: () => Promise<JumpHost[]>
  save_jump_hosts: (hosts: JumpHost[]) => Promise<void>
  import_jump_hosts: (tomlContent: string) => Promise<JumpHost[]>
  // Local machine venv auto-discovery ([local] venv_globs in config.toml). get
  // returns the saved globs + a live preview of what they resolve to here; save
  // validates + persists them (raises on bad input), then rediscovers.
  get_local_venv_config: () => Promise<LocalVenvConfig>
  save_local_venv_config: (globs: string[]) => Promise<LocalVenvConfig>
  // Canonical host-group registry (runspec_groups.toml) — the controlled
  // vocabulary the host editor's group dropdown is populated from (synced).
  get_groups: () => Promise<HostGroup[]>
  save_groups: (groups: HostGroup[]) => Promise<void>
  rename_group: (oldName: string, newName: string) => Promise<void>
  // Host-profile registry + the machine-local active profile (working sets).
  get_profiles: () => Promise<HostProfile[]>
  save_profiles: (profiles: HostProfile[]) => Promise<void>
  rename_profile: (oldName: string, newName: string) => Promise<void>
  get_active_profile: () => Promise<string>
  set_active_profile: (name: string) => Promise<{ active: string }>
  // Self-service catalog registry (runspec_self_service.toml) — which discovered
  // runnables are offered to visitors in the helpdesk room (Settings →
  // Self-Service). Replaces the runspec.toml `discoverable = ["self-service"]`
  // marking; saving republishes every connected queue's catalog.
  get_self_service: () => Promise<SelfServiceEntry[]>
  save_self_service: (entries: SelfServiceEntry[]) => Promise<void>
  // Machine-local single-room selector (Settings → Self-Service): which
  // room-configured host this console connects to. get_room_hosts lists the
  // candidates; get_active_room returns the effectively-connected host ("" =
  // none); set_active_room picks one ("" = no room) and live re-joins.
  get_room_hosts: () => Promise<RoomHost[]>
  get_active_room: () => Promise<string>
  set_active_room: (host: string) => Promise<{ active: string }>
  // Git-repo registry (runspec_repos.toml) — the agent's read/edit allowlist
  // (Settings → Repos). sync_repo clones/fetches a repo on operator demand.
  get_repos: () => Promise<Repo[]>
  save_repos: (repos: Repo[]) => Promise<void>
  // Remove a repo from the registry and delete its AppData checkout from disk; a
  // custom dev-folder checkout (Repo.path) is never deleted.
  delete_repo: (name: string) => Promise<{ ok: boolean; removed_checkout: boolean }>
  sync_repo: (name: string) => Promise<RepoSyncResult>
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
  credentials_for_runnable: (host: string, runnable: string) => Promise<{ id: string; label: string; has_secret: boolean; secret_arg: string; username_arg: string; username: string }[]>
  save_credential: (entry: Credential) => Promise<{ id?: string; error?: string }>
  delete_credential: (id: string) => Promise<void>
  credentials_backend: () => Promise<CredentialBackend>
  // Settings → MCP (external MCP servers the chat agent connects to). get/save the
  // [mcp] section; save validates, writes config.toml, reconnects, and returns the
  // fresh status. mcp_status/mcp_reload report/refresh live connection state;
  // mcp_credential_options feeds the per-server credential dropdown (no secrets).
  get_mcp_config: () => Promise<McpConfig>
  save_mcp_config: (payload: McpConfig) => Promise<McpStatus>
  mcp_status: () => Promise<McpStatus>
  mcp_reload: () => Promise<McpStatus>
  mcp_credential_options: () => Promise<McpCredentialOption[]>
  // Config Sync (Settings → Config Sync): pull config files from a git repo by
  // tag. get/save the settings, list the repo's tags for the ref dropdown, and
  // run a pull (overwrites local config, backing up first; hot-reloads after).
  get_sync_config: () => Promise<SyncConfig>
  save_sync_config: (data: Partial<SyncConfig>) => Promise<void>
  list_sync_tags: () => Promise<{ tags?: string[]; error?: string }>
  sync_pull: () => Promise<SyncPullResult>
  // Package config seed (Settings → Config Sync): baseline config bundled inside
  // the deployment package, applied on launch when its version changed. Read-only
  // status, and a force re-apply button.
  seed_status: () => Promise<SeedStatus>
  seed_apply: (force?: boolean) => Promise<SeedApplyResult>
  // Runbook docs: a deterministic markdown wiki generated from the local runbooks
  // + fleet, for humans to follow during an outage. get/save the output dir and
  // (re)generate the files.
  get_docs_config: () => Promise<DocsConfig>
  save_docs_config: (dest_dir: string) => Promise<void>
  generate_docs: (dest_dir?: string) => Promise<DocsGenResult>
  test_host: (name: string) => Promise<TestResult>
  // `credentialIds` (optional) are saved credentials the operator chose to pass to
  // this run as name-derived plain env vars (e.g. JIRA_USERNAME/JIRA_PASSWORD).
  // Credentials are never copied into args on this human/form path.
  invoke_runnable: (host: string, runnable: string, args: Record<string, unknown>, commandPath?: string[], group?: string, credentialIds?: string[]) => Promise<string>
  // List a directory on a host for the Forms path-picker. With asServiceAccount +
  // a runnable, the remote listing runs as that runnable's run_as (sudo/become).
  list_dir: (host: string, path?: string, group?: string, dirsOnly?: boolean, runnable?: string, asServiceAccount?: boolean) => Promise<DirListing>
  cancel_invocation: (invId: string) => Promise<void>
  // Copy files between the local machine and a remote host over SFTP (exactly one
  // side is 'local'). The remote side is a configured host name or an AdHocEndpoint
  // (an SSH host not in the fleet, reached via a jump). Returns an id, then streams
  // runspec:transfer_progress / transfer_end. filterName/filterArg select a saved
  // source-side filter (download: filtered at the source; upload: filtered locally).
  // destName optionally renames the (single) file on the destination (bare filename).
  start_transfer: (sourceHost: TransferEndpoint, sourcePaths: string[], destHost: TransferEndpoint, destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
  // Fan-out: upload the same local source file(s) to dest_dir on many remote hosts.
  // filterName/filterArg apply a saved filter locally (once) before fanning out.
  // destName optionally renames the (single) file on every target (bare filename).
  start_fanout: (sourcePaths: string[], destHosts: string[], destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
  // Relay: stream file(s) from one host to another through the console. Source is a
  // configured host, or an ad-hoc host reached via a configured jump (key first,
  // then an optional password — used once, never stored/logged).
  // filterName/filterArg select a saved source-side filter run on the source host,
  // so only matching output is relayed (handy for pulling a few lines from a huge log).
  // destName optionally renames the (single) file on the destination (bare filename).
  start_relay: (source: RelaySource, sourcePaths: string[], destHost: string, destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
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
  // Absolute path of the current chat session's agent workspace, or '' if none.
  // The Transfer dialog defaults a local destination to it so downloads land
  // where the agent works. Creates the directory if missing.
  current_workspace_path: () => Promise<string>
  send_chat: (message: string, invocationId?: string) => Promise<string>
  cancel_chat: (chatId: string) => Promise<void>
  clear_chat: () => Promise<void>
  // Condense the live chat history in place (drop the older prefix, fold a note,
  // keep the recent tail) — frees context without losing the thread. Returns the
  // number of messages dropped. See docs/design/console-context-indicator.md.
  compact_chat: () => Promise<{ ok: boolean; compacted: number }>
  // Toggle whether a completed chat turn stays in the agent's context (the
  // Console card "in context" checkbox). Reversible and non-destructive — the
  // backend keeps the messages and only filters, so re-including restores the
  // exchange. Returns the resulting in/out state.
  set_turn_included: (chatId: string, included: boolean) => Promise<{ ok: boolean; included: boolean }>
  // ── saved conversations (chat history) ────────────────────────────────────────
  // Metadata for every saved chat, newest first (for the Conversations browser).
  list_conversations: () => Promise<ConversationMeta[]>
  // A saved conversation's full transcript, for rendering it read-only.
  load_conversation: (id: string) => Promise<ChatConversation>
  // Make a saved conversation the active rolling chat so new sends continue it
  // (history rebuilt from the transcript as plain text; exact=false). On a long
  // chat the middle is condensed on resume (compacted=true; summarized=true when
  // an LLM summary was used) — the saved transcript itself is never changed.
  resume_conversation: (id: string) => Promise<{ ok: boolean; turns?: number; kept?: number; compacted?: boolean; summarized?: boolean; exact?: boolean; error?: string }>
  delete_conversation: (id: string) => Promise<void>
  rename_conversation: (id: string, title: string) => Promise<void>
  // ── runbooks (user-invocable, parameterized runbooks) ─────────────────────────
  // All runbooks with their [runbook.args] parsed to ArgDef[] — feeds the `$` menu,
  // the run modal, and the Settings editor.
  get_runbooks: () => Promise<Runbook[]>
  // Persist the whole collection from the Settings editor (validates each args
  // table; rejects with {ok:false,error} if one is malformed).
  save_runbooks: (runbooks: Runbook[]) => Promise<{ ok: boolean; count?: number; error?: string }>
  // Render a runbook's body with the operator's form values (validated against its
  // args) + optional extra instructions, then start a chat turn tagged with the
  // runbook. Returns {chat_id, message} (the rendered text the UI shows) or {error}.
  run_runbook: (
    runbookId: string,
    values?: Record<string, unknown>,
    extraInstructions?: string,
  ) => Promise<{ chat_id?: string; message?: string; error?: string }>
  // ── saved runs (deterministic presets) ─────────────────────────────────────
  // All saved runs with their params parsed to ArgDef[] — feeds the Forms lane,
  // the run modal, the Schedules picker, and the Settings editor.
  get_saved_runs: () => Promise<SavedRun[]>
  // Persist the whole collection from the Settings editor (each entry validated;
  // rejected with {ok:false,error} if one is invalid).
  save_saved_runs: (savedRuns: SavedRun[]) => Promise<{ ok: boolean; count?: number; error?: string }>
  // Fire a saved run now: validates `values` against its open params, merges
  // them over the fixed args, resolves the stored targeting bridge-side, and
  // invokes — one {host, inv_id} per host actually run (the UI renders one
  // Console run block each, streaming via runspec:output / runspec:run_end).
  run_saved_run: (
    id: string,
    values?: Record<string, unknown>,
  ) => Promise<{ id?: string; runnable?: string; runs?: { host: string; inv_id: string }[]; error?: string }>
  // Resolve a parked autonomy-gate confirm. `editedInput` (optional) replaces the
  // agent's proposed tool_input when approving — the operator can tweak the args
  // before the call runs (editable confirmations). Ignored on a deny.
  resolve_tool_confirmation: (
    requestId: string,
    approved: boolean,
    editedInput?: Record<string, unknown>,
  ) => Promise<void>
  // ── event triggers ─────────────────────────────────────────────────────────
  list_triggers: () => Promise<TriggerListResult>
  // The built-in action tools an agent-mode trigger may be allowed to use, for the
  // Triggers editor's per-trigger allowlist ({name, label}; Outlook entries appear
  // only when Outlook COM is reachable on the host).
  list_action_tools: () => Promise<{ name: string; label: string }[]>
  set_trigger_enabled: (name: string, enabled: boolean) => Promise<{ ok: boolean }>
  // Add/update one [triggers.<name>] table (upsert by name) or remove it; both
  // restart the trigger engine. Re-read list_triggers after to refresh the view.
  save_trigger: (trigger: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>
  delete_trigger: (name: string) => Promise<{ ok: boolean }>
  save_rota: (rota: Rota) => Promise<{ ok: boolean }>
  // Rota "Take a break": suspend automation for N minutes (auto-resumes; held
  // while the screen is locked) + a best-effort Outlook reminder at the return time.
  start_break: (minutes: number) => Promise<{ break_until: string | null; reminder_set: boolean }>
  cancel_break: () => Promise<{ ok: boolean }>
  // Lock auto-suspend live indicator: current screen-lock state (cheap probe).
  screen_state: () => Promise<LockState>
  // Local Arize Phoenix server (Phoenix tab): launch / stop / poll a private,
  // on-machine OTLP collector + trace-UI. phoenix_start also live-routes the
  // console's own agent traces to it; phoenix_stop restores the saved route.
  phoenix_status: () => Promise<PhoenixStatus>
  phoenix_start: () => Promise<PhoenixStatus>
  phoenix_stop: () => Promise<PhoenixStatus>
  // Open the running Phoenix UI in the OS browser (fallback when the iframe can't
  // frame it). Opens the server's own known URL, http(s) only.
  phoenix_open: () => Promise<{ ok: boolean; url: string }>
  // Wake/sleep Windows Voice access listening (injects Alt+Shift+B host-side —
  // same chord both ways; the UI tracks state). Speak button wakes after focusing
  // the box; sending sleeps. {ok:false} off Windows / on failure (operator can
  // still drive Voice access from its own bar).
  start_voice_dictation: () => Promise<{ ok: boolean }>
  stop_voice_dictation: () => Promise<{ ok: boolean }>
  get_trigger_board: () => Promise<TriggerCard[]>
  get_trigger_events: () => Promise<TriggerEvent[]>
  // Fire a trigger now with a synthetic event (the Triggers table's "Trigger now"
  // button) — test a trigger without waiting for a real event. `body` is the
  // pasted message (lands under the `body` payload key); `fields` are key=value
  // payload overrides. Bypasses match/rota/dedup for the named trigger.
  test_trigger: (name: string, body: string, fields: Record<string, string>) => Promise<{ ok: boolean; error?: string }>
  // Run a `source = "paste"` trigger now with `text` as its input — a REAL run
  // through the trigger's match/rota/dedup gates (unlike test_trigger). `fields`
  // are extra key=value payload. `skipped` explains a gated no-run.
  submit_paste_input: (name: string, text: string, fields: Record<string, string>) => Promise<{ ok: boolean; skipped?: string; error?: string }>
  // Schedules mirror the trigger run inbox + event log with their own feeds.
  get_schedule_board: () => Promise<RunCard[]>
  get_schedule_events: () => Promise<TriggerEvent[]>
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
  // Customer teams (Settings → Teams) — managed over the live helpdesk room
  // connection. Fire-and-forget: the room's reply arrives as a runspec:teams event
  // (TeamsEvent). ok=false (with a reason) when no helpdesk room is connected.
  list_teams: () => Promise<{ ok: boolean; error?: string }>
  add_team: (name: string) => Promise<{ ok: boolean; error?: string }>
  remove_team: (slug: string) => Promise<{ ok: boolean; error?: string }>
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
  copy_ssh_id_all: (password: string, targets?: string[]) => Promise<{ ok: boolean; per_host: { host: string; ok: boolean; connected: boolean; error: string }[]; message: string }>
  puttygen_path: () => Promise<string>
  launch_puttygen: () => Promise<void>
  open_putty_url: (url: string) => Promise<void>
  browse_ssh_binary: () => Promise<string>
  browse_key_file: () => Promise<string>
  browse_folder: () => Promise<string>
  browse_sound_file: () => Promise<string>
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
  // Record a frontend error in the Python log file (so it isn't trapped in the
  // web view console). Best-effort; the proxy forwards uncaught bridge errors here.
  log_client_error: (context: string, message: string, detail?: unknown) => Promise<void>
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

// Forward a bridge error into the Python log file (best-effort). Guarded so the
// very failure we're surfacing — an older Python that doesn't expose
// `log_client_error` — can't recurse or throw; if it's missing we just skip, and
// the descriptive error below still shows in the Dev tab.
let _forwardingClientError = false
function forwardClientError(name: string, err: unknown): void {
  if (_forwardingClientError || name === 'log_client_error') return
  const fn = (window.pywebview?.api as Partial<BridgeApi> | undefined)?.log_client_error
  if (typeof fn !== 'function') return
  _forwardingClientError = true
  try {
    void fn('bridge-call', `${name}: ${err instanceof Error ? err.message : String(err)}`)
  } catch { /* logging must never break the UI */ }
  finally { _forwardingClientError = false }
}

// Resolve a bridge method, tolerating pywebview's async startup.
//
// pywebview 6.x injects `window.pywebview.api` and then attaches its methods
// **asynchronously** — so a mount-time call can land *before* a given method
// exists on the api object yet (the failures cluster at startup and vary in
// count run-to-run, the signature of a race, not a missing method). We read the
// **live** api each time (the object may be replaced/extended after the first
// ready-probe) and, on the real pywebview path, briefly retry while the method
// is still being attached. A method that's still absent after the api has had
// time to settle is a genuine "not exposed" (a real version mismatch).
const _BRIDGE_READY_RETRIES = 40   // × 75ms ≈ 3s max wait, only when racing/absent
const _BRIDGE_READY_DELAY_MS = 75

async function resolveMethod(
  name: string,
  resolved: BridgeApi,
): Promise<(...a: unknown[]) => unknown> {
  const fromLive = (): ((...a: unknown[]) => unknown) | undefined => {
    // Plain browser (mock): no window.pywebview — use the resolved mock, which
    // always has every method. Real desktop: read the live pywebview api.
    const api =
      (window.pywebview?.api as Record<string, unknown> | undefined) ??
      (resolved as unknown as Record<string, unknown>)
    const fn = api[name]
    return typeof fn === 'function' ? (fn as (...a: unknown[]) => unknown) : undefined
  }
  let fn = fromLive()
  // Only retry on the real pywebview path (the mock has every method already).
  for (let i = 0; !fn && window.pywebview?.api && i < _BRIDGE_READY_RETRIES; i++) {
    await new Promise(r => setTimeout(r, _BRIDGE_READY_DELAY_MS))
    fn = fromLive()
  }
  if (!fn) {
    throw new Error(
      `Bridge method "${name}" is not exposed by the Python API (still missing after ` +
      `waiting for the bridge to settle) — likely a version mismatch between the ` +
      `installed runspec-console and this UI bundle.`,
    )
  }
  return fn
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
        const resolved = await resolveBridge()
        const fn = await resolveMethod(name, resolved)
        const result = await fn(...args)
        recordCallEnd(entryId, result, performance.now() - t0)
        return result
      } catch (err) {
        recordCallError(entryId, err, performance.now() - t0)
        forwardClientError(name, err)
        throw err
      }
    }
  },
})
